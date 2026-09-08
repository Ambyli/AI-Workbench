#!/bin/sh
# Generate the Trino coordinator's TLS keystore + password file.
#
# Inputs (env, set in .env and passed through docker-compose.trino.yml):
#   TRINO_JDBC_USERS  user:password[,user2:password2]  (required)
#   TRINO_TLS_SANS    subjectAltName list for the self-signed cert, e.g.
#                     DNS:trino-coordinator,DNS:localhost,IP:127.0.0.1,IP:10.0.0.5
#
# Outputs (in the /auth volume, owned by the trino uid 1000):
#   /auth/tls/trino.pem   key + cert  — http-server.https.keystore.path
#   /auth/tls/trino.crt   cert only   — for client truststores
#   /auth/password.db     bcrypt lines — file.password-file
#
# Idempotency:
#   * TLS material is generated ONCE. Delete /auth/tls/trino.pem from the
#     volume (see TRINO.md § Rotating the TLS cert) to regenerate.
#   * password.db is rebuilt on EVERY run so editing TRINO_JDBC_USERS and
#     re-running `make up trino trino-auth-init` rotates credentials. The
#     coordinator re-reads the file every 5 s (file.refresh-period) — no
#     restart needed.
set -eu

AUTH_DIR=/auth
TLS_DIR="$AUTH_DIR/tls"
PEM="$TLS_DIR/trino.pem"
CRT="$TLS_DIR/trino.crt"
PASSWD="$AUTH_DIR/password.db"
TRINO_UID=1000

: "${TRINO_JDBC_USERS:?TRINO_JDBC_USERS must be set (user:password[,user2:password2])}"
TRINO_TLS_SANS="${TRINO_TLS_SANS:-DNS:trino-coordinator,DNS:localhost,IP:127.0.0.1}"

mkdir -p "$TLS_DIR"

# ── 1. TLS keystore (generate once) ─────────────────────────────────────
if [ ! -s "$PEM" ]; then
    echo "[auth-init] generating self-signed TLS cert (SANs: $TRINO_TLS_SANS)"
    openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
        -subj "/CN=trino-coordinator" \
        -addext "subjectAltName=${TRINO_TLS_SANS}" \
        -keyout "$TLS_DIR/key.pem" -out "$CRT" 2>/dev/null
    # Trino wants key + cert in one unencrypted PEM.
    cat "$TLS_DIR/key.pem" "$CRT" > "$PEM"
    rm -f "$TLS_DIR/key.pem"
else
    echo "[auth-init] reusing existing TLS cert at $PEM"
fi
echo "[auth-init] cert fingerprint: $(openssl x509 -in "$CRT" -noout -fingerprint -sha256 | cut -d= -f2)"

# ── 2. password.db (rebuild every run) ──────────────────────────────────
TMP="$PASSWD.tmp"
: > "$TMP"
count=0

OLD_IFS=$IFS
IFS=','
# shellcheck disable=SC2086  # word-splitting on ',' is the point
set -- $TRINO_JDBC_USERS
IFS=$OLD_IFS

for entry in "$@"; do
    # trim surrounding whitespace
    entry=$(printf '%s' "$entry" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [ -n "$entry" ] || continue
    case "$entry" in
        *:*) ;;
        *)
            echo "[auth-init] ERROR: entry '$entry' is not user:password" >&2
            exit 1
            ;;
    esac
    user=${entry%%:*}
    pass=${entry#*:}
    if [ -z "$user" ] || [ -z "$pass" ]; then
        echo "[auth-init] ERROR: empty user or password in '$entry'" >&2
        exit 1
    fi
    # -n print to stdout, -b password on cmdline, -B bcrypt, -C cost.
    # Trino requires bcrypt ($2y$) with cost >= 8.
    htpasswd -nbBC 10 "$user" "$pass" >> "$TMP"
    count=$((count + 1))
    echo "[auth-init] added user '$user'"
done

if [ "$count" -eq 0 ]; then
    echo "[auth-init] ERROR: TRINO_JDBC_USERS produced no users" >&2
    exit 1
fi

mv "$TMP" "$PASSWD"

# ── 3. Ownership — trinodb/trino runs as uid 1000 ───────────────────────
chown -R "$TRINO_UID:$TRINO_UID" "$AUTH_DIR"
chmod 755 "$AUTH_DIR" "$TLS_DIR"
chmod 600 "$PEM" "$PASSWD"
chmod 644 "$CRT"

echo "[auth-init] done — $count user(s) in $PASSWD"
