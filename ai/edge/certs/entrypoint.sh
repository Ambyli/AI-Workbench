#!/bin/sh
# edge-certs: the Let's Encrypt certificate for the edge's TLS listeners.
# Runs in certbot/dns-cloudflare (Alpine, busybox sh, Python 3.14, openssl);
# this directory is bind-mounted read-only at /edge-certs and the compose file
# starts it as `sh /edge-certs/entrypoint.sh` (no exec bit needed). Operator
# guide: ai/edge/EDGE.md § TLS.
#
#   1. Makes the runtime-API socket directory (the `edge_api` volume) writable
#      for HAProxy, which runs as uid/gid 99 and creates admin.sock in it. A
#      fresh named volume is root:root 0755.
#   2. If /certs/edge.pem (the `edge_certs` volume) does not exist yet, writes
#      a self-signed placeholder: HAProxy refuses to start when an `ssl crt`
#      file is missing, and the edge waits for this container's healthcheck
#      (`test -s /certs/edge.pem`) before it starts.
#   3. With EDGE_TLS_DOMAIN and CLOUDFLARE_DNS_API_TOKEN set: obtains the
#      certificate by DNS-01 through the Cloudflare API, then once an hour
#      makes sure the running edge serves the current certificate, and every
#      12 h runs `certbot renew`. A new or renewed certificate is written to
#      /certs/edge.pem and pushed into the running HAProxy through its
#      runtime API (edge_certs.py sync: certbot's --deploy-hook, and again
#      after every run). Without them: keeps the placeholder and idles.
#
# Secrets: CLOUDFLARE_DNS_API_TOKEN goes into a 0600 ini on a tmpfs
# (/run/edge-certs, never a volume, never the repo) and is then unset, so
# certbot and the hook never see it in their environment. Never add `set -x`.
set -eu

CERTS_DIR=/certs
API_DIR=/run/haproxy-api
SECRETS_DIR=/run/edge-certs
CREDENTIALS=$SECRETS_DIR/cloudflare.ini
HELPER=/edge-certs/edge_certs.py
LE_DIR=/etc/letsencrypt
# Group of the `haproxy` user in haproxy:*-alpine (/etc/group: haproxy:x:99:).
# Exported: edge_certs.py gives /certs/edge.pem to root:$HAPROXY_GID 0640.
HAPROXY_GID=99
export HAPROXY_GID
# Seconds certbot waits after creating the _acme-challenge TXT record before
# Let's Encrypt checks it (plugin default 10). Cloudflare's own nameservers
# usually answer within seconds; raise this if validation fails with
# "Incorrect TXT record" / NXDOMAIN, then `docker restart edge-certs`.
PROPAGATION_SECONDS=30
TICK_SECONDS=3600
RENEW_EVERY_TICKS=12

log() { printf '%s edge-certs: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

idle() {
    while :; do
        sleep 86400 &
        wait $! || :
    done
}

# 1. Runtime-API socket directory.
mkdir -p "$API_DIR" "$CERTS_DIR"
chown 0:"$HAPROXY_GID" "$API_DIR"
chmod 0770 "$API_DIR"

domain=${EDGE_TLS_DOMAIN:-}
enabled=1
if [ -z "$domain" ] || [ -z "${CLOUDFLARE_DNS_API_TOKEN:-}" ]; then
    enabled=0
fi
case $domain in
    *[!A-Za-z0-9.-]*)
        log "EDGE_TLS_DOMAIN must be one hostname (letters, digits, dots, dashes); not requesting a certificate"
        enabled=0
        ;;
esac

# 2. Placeholder, so HAProxy can start before (or without) a real certificate.
if [ ! -s "$CERTS_DIR/edge.pem" ]; then
    if [ "$enabled" = 1 ]; then
        python3 "$HELPER" placeholder "$domain"
    else
        python3 "$HELPER" placeholder localhost
    fi
fi

if [ "$enabled" = 0 ]; then
    unset CLOUDFLARE_DNS_API_TOKEN
    log "TLS certificates are off: EDGE_TLS_DOMAIN or CLOUDFLARE_DNS_API_TOKEN is empty, so the edge's TLS listeners serve the self-signed certificate already in $CERTS_DIR/edge.pem (ai/edge/EDGE.md § TLS)"
    idle
fi

# 3. Let's Encrypt.
umask 077
mkdir -p "$SECRETS_DIR"
printf 'dns_cloudflare_api_token = %s\n' "$CLOUDFLARE_DNS_API_TOKEN" > "$CREDENTIALS"
chmod 0600 "$CREDENTIALS"
unset CLOUDFLARE_DNS_API_TOKEN
umask 022

# Only the literal 0 asks for production certificates: anything else (unset,
# 1, a typo) stays on staging, which cannot burn production rate limits.
if [ "${EDGE_TLS_STAGING:-1}" = 0 ]; then
    want_staging=0
    ca="production"
else
    want_staging=1
    ca="STAGING (untrusted; set EDGE_TLS_STAGING=0 once it works)"
fi

conf=$LE_DIR/renewal/$domain.conf
lineage_is_staging() { grep -q '^server *= *.*staging' "$conf"; }

obtain() {
    # $@: extra certbot flags. The deploy hook is saved in the renewal
    # config, so `certbot renew` runs it too.
    if [ "$want_staging" = 1 ]; then
        set -- "$@" --staging
    fi
    if [ -n "${EDGE_TLS_EMAIL:-}" ]; then
        set -- "$@" -m "$EDGE_TLS_EMAIL" --no-eff-email
    else
        set -- "$@" --register-unsafely-without-email
    fi
    certbot certonly --non-interactive --agree-tos \
        --dns-cloudflare \
        --dns-cloudflare-credentials "$CREDENTIALS" \
        --dns-cloudflare-propagation-seconds "$PROPAGATION_SECONDS" \
        --cert-name "$domain" -d "$domain" \
        --deploy-hook "python3 $HELPER sync" \
        "$@"
}

renew() {
    # The credentials path and propagation wait are passed again so this
    # script stays their single source; the server, key type and deploy hook
    # come from the lineage's renewal config.
    certbot renew --non-interactive --cert-name "$domain" \
        --dns-cloudflare-credentials "$CREDENTIALS" \
        --dns-cloudflare-propagation-seconds "$PROPAGATION_SECONDS"
}

log "managing a $ca certificate for $domain (DNS-01 through the Cloudflare API)"
# Lineages for other names (an earlier EDGE_TLS_DOMAIN) stay in the volume:
# every certbot run below is scoped to --cert-name "$domain", and
# edge_certs.py sync reads only live/$EDGE_TLS_DOMAIN, so they are never
# renewed or served. Said once, so the operator knows they can go.
for other in "$LE_DIR"/renewal/*.conf; do
    if [ -f "$other" ] && [ "$other" != "$conf" ]; then
        other=${other##*/}
        other=${other%.conf}
        log "an old certificate lineage for $other (not EDGE_TLS_DOMAIN) is left alone: never renewed, never served. To remove it: docker exec edge-certs certbot delete --cert-name $other --non-interactive"
    fi
done
tick=0
while :; do
    if [ ! -f "$conf" ]; then
        log "requesting a $ca certificate for $domain; certbot waits ${PROPAGATION_SECONDS}s for the TXT record"
        obtain || log "certbot certonly failed (see above); the edge keeps serving $CERTS_DIR/edge.pem; retrying in 1 h"
    elif [ "$want_staging" = 0 ] && lineage_is_staging; then
        log "EDGE_TLS_STAGING=0 and $domain has a staging certificate: replacing it with a production one"
        obtain --force-renewal || log "certbot certonly failed (see above); the staging certificate stays; retrying in 1 h"
    elif [ $((tick % RENEW_EVERY_TICKS)) -eq 0 ]; then
        if [ "$want_staging" = 1 ] && ! lineage_is_staging; then
            log "EDGE_TLS_STAGING is not 0 but $domain already has a production certificate: keeping it and renewing from production (to start over: docker exec edge-certs certbot delete --cert-name $domain)"
        fi
        renew || log "certbot renew failed (see above); retrying in 12 h"
    fi
    python3 "$HELPER" sync || log "could not bring the edge up to date (see above); retrying in 1 h"
    tick=$((tick + 1))
    sleep "$TICK_SECONDS" &
    wait $! || :
done
