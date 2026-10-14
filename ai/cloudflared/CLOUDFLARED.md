# cloudflared — Cloudflare Tunnel connector

Publishes this host's services to the public internet over an outbound-only
Cloudflare Zero Trust tunnel. No inbound firewall rule, no public IP.

Compose file: [`docker-compose.cloudflared.yml`](docker-compose.cloudflared.yml).
Config: `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.

Separately, the LAN edge's TLS certificate uses Cloudflare's DNS and API (not
the tunnel) with its own token, `CLOUDFLARE_DNS_API_TOKEN`: see
[DNS-01 certificate for the LAN edge](#dns-01-certificate-for-the-lan-edge-aizeoenergycom).

Ingress rules (which hostname maps to which origin) live in the **Cloudflare
Zero Trust dashboard**, not in this repo. The token identifies the tunnel; the
dashboard supplies the routing table, which the connector pulls on connect and
logs as `Updated to new configuration config=… version=N`.

---

## Exactly one connector per tunnel

**A tunnel may only have one connector on this host. Two is a bug, and it
fails intermittently rather than cleanly.**

The one exception is deliberate and lasts seconds: `make up cloudflared`
swaps the container side by side (see [EDGE.md](../edge/EDGE.md)), so while
the new `ai-cloudflared-<hash>` copy comes up next to the old one, two
connectors are registered. Both are this exact container — same token, same
config, same reachable origins — so neither returns 502, and the old one
retires (draining for `--grace-period`) as soon as the new one reports ready.
`ps aux` below may briefly count two during a deploy.

Cloudflare load-balances requests across every registered connector for a
tunnel. If two connectors are registered and only one can reach the origin,
roughly half of all requests return **502 Bad Gateway** and half succeed —
per request, not per session. The site looks "randomly broken", browser
reloads appear to fix it, and any single test has a ~50% chance of passing.

This is easy to create by accident, because a connector can be started two
different ways and they don't know about each other:

| Form | Started by | Tunnel identified by |
|---|---|---|
| Docker (this compose file) | `make up cloudflared` | `CLOUDFLARE_TUNNEL_TOKEN` in `.env` |
| Host service | `systemctl enable --now cloudflared` | `/etc/cloudflared/config.yml` |

**Resetting a tunnel's token does not create a new tunnel.** It rotates the
secret on the existing one. A host service pointed at the same tunnel id will
keep registering to it after the reset, so rotating the token does not evict a
duplicate connector.

### Checking for duplicates

```bash
# Every cloudflared on this host — expect exactly one line.
ps aux | grep -c '[c]loudflared'

# Is a host service also running?
systemctl status cloudflared --no-pager

# Which tunnel does the host service serve?
grep '^tunnel:' /etc/cloudflared/config.yml

# Which tunnel does the container serve? (prints the id, never the secret)
python3 -c 'import base64,json,sys; t=sys.argv[1]; t+="="*(-len(t)%4); print(json.loads(base64.urlsafe_b64decode(t))["t"])' \
  "$(grep -m1 '^CLOUDFLARE_TUNNEL_TOKEN=' .env | cut -d= -f2- | tr -d '"'"'"' ')"
```

Matching tunnel ids from two connectors is the bug. Retire the host service:

```bash
sudo systemctl disable --now cloudflared
```

Before doing that, confirm every dashboard ingress origin is reachable from
inside the container — see the next section.

---

## Origin addresses are container-relative

Ingress origins are dialed **from inside this container**, so `localhost` means
the container itself. Origins authored for a host-based connector silently
break when the connector moves into Docker, and the failure surfaces as a 502
at the edge with the real reason only in the connector's log:

```
ERR Request failed error="Unable to reach the origin service…
    dial tcp [::1]:4001: connect: connection refused"
ERR Request failed error="Unable to reach the origin service…
    lookup oauth2-proxy on 127.0.0.53:53: server misbehaving"
```

The first is a host-port origin used from a container. The second is the
mirror-image mistake: a Docker service name used from a **host** connector,
which has no Docker DNS.

Translate origins by where the target actually listens:

| Target listens on | Correct origin | Wrong (host-era) origin |
|---|---|---|
| `ai_shared` network | `http://<service>:<container port>` | `http://localhost:<host port>` |
| The Docker host | `tcp://host.docker.internal:<port>` | `tcp://localhost:<port>` |

Use the **container** port, not the published host port. LiteLLM listens on
`4000` inside the container and is published to the host as `4001`, so the
origin is `http://litellm:4000` — `4001` does not exist on `ai_shared`.

`host.docker.internal` resolves only because this compose file declares
`extra_hosts: - "host.docker.internal:host-gateway"`. Remove that and every
host-targeted ingress rule breaks.

### Current ingress rules

| Hostname | Origin | Notes |
|---|---|---|
| `chat.zeoenergy.com` | `http://oauth2-proxy:4180` | SSO front door for Open WebUI, `/sandboxes/*`, `/n8n/*` |
| `api.zeoenergy.com` | `http://litellm:4000` | **Bypasses oauth2-proxy** — LiteLLM's own key auth is the only gate |
| `rdp.zeoenergy.com` | `tcp://host.docker.internal:3389` | RDP listens on the host, not in Docker |
| _(catch-all)_ | `http_status:404` | |

Adding a hostname served by a container needs **no** compose or `.env` change —
only a dashboard rule plus, if it should be behind SSO, an entry in
`OAUTH2_PROXY_UPSTREAMS`. Prefer putting a new service behind
`chat.zeoenergy.com/<path>/` over giving it its own hostname: it inherits the
existing Google sign-in cookie instead of being exposed unauthenticated.

---

## Cache rule — bypass for `chat.zeoenergy.com`

Cloudflare caches by file extension by default — `.png`, `.ico`, `.svg`, `.css`,
`.js`, fonts — for 4 h when the origin sends no `Cache-Control`, and it does so
**regardless of request cookies**. Everything on `chat.zeoenergy.com` sits behind
oauth2-proxy, and neither oauth2-proxy nor Open WebUI's static handler sets
`Cache-Control`, so the edge caches *authenticated* responses and then serves
them to anyone who asks: an anonymous
`curl -sI https://chat.zeoenergy.com/static/favicon.png` returns `200` with
`cf-cache-status: HIT` where oauth2-proxy would have returned `403`. It is also
why re-branding Open WebUI's `/static/*` assets appears to do nothing for hours
after a recreate — see
[ai/openwebui/OPENWEBUI.md § Cloudflare caches /static/*](../openwebui/OPENWEBUI.md#cloudflare-caches-static).

Like the ingress rules, this lives in the dashboard, not the repo:

1. Caching → Cache Rules → Create rule. Name: `chat – bypass (authenticated origin)`.
2. Match: **Hostname** equals `chat.zeoenergy.com`. Optionally AND **URI Path**
   does not start with `/assets/` — the unauthenticated logo sidecar is safe to
   cache.
3. Cache eligibility: **Bypass cache**. Deploy.
4. Caching → Configuration → **Purge Everything**, once, to evict what is
   already cached.

Verify with the `curl -sI` above: `cf-cache-status` should read `DYNAMIC` or
`BYPASS`, and the anonymous request should get oauth2-proxy's `403`.
`api.zeoenergy.com` needs no rule — LiteLLM answers JSON on extension-less
paths, which Cloudflare never caches by default.

---

## DNS-01 certificate for the LAN edge (ai.zeoenergy.com)

**This section is not about the tunnel.** It uses Cloudflare's **DNS** and its
**API**, and nothing in `cloudflared`.

The LAN edge ([`ai/edge`](../edge/EDGE.md)) serves LiteLLM over HTTPS on port
`4443` with a Let's Encrypt certificate for the name in `EDGE_TLS_DOMAIN`,
`ai.zeoenergy.com` here. Its `edge-certs` container gets that certificate with
a **DNS-01** challenge: it creates the TXT record
`_acme-challenge.ai.zeoenergy.com` through the Cloudflare API, Let's Encrypt
reads it from Cloudflare's nameservers, and the record is deleted again. LAN
clients connect straight to the box's LAN address: their traffic never passes
through Cloudflare or the tunnel. The full checklist (`.env`, `make up edge`,
staging → production, moving clients) is in
[EDGE.md § TLS](../edge/EDGE.md#tls); the two Cloudflare steps are here.

### The API token

1. Cloudflare dashboard → **My Profile** (top right) → **API Tokens** →
   **Create Token**.
2. Next to **Edit zone DNS**, click **Use template**. Its permission is
   already filled in: **Zone · DNS · Edit**. Leave it as the only one.
3. **Zone Resources**: **Include** → **Specific zone** → `zeoenergy.com`.
   Never "All zones".
4. Optional: **Client IP Address Filtering** to the office's public address,
   and a **TTL** end date (then put the rotation in a calendar).
5. **Continue to summary** → **Create Token**. Cloudflare shows the token
   **once**. Put it in the box's `.env` as `CLOUDFLARE_DNS_API_TOKEN`, and
   nowhere else: not chat, not a ticket, not the repo, not `.env.example`.
6. To check it on the box without leaving it in shell history:
   ```bash
   read -rs CF && curl -s https://api.cloudflare.com/client/v4/user/tokens/verify \
     -H "Authorization: Bearer $CF"; unset CF
   ```
   Expect `"status":"active"`.

This token has nothing to do with `CLOUDFLARE_TUNNEL_TOKEN` above: that one
identifies the tunnel, this one edits DNS records.

### The DNS record

Cloudflare dashboard → `zeoenergy.com` → **DNS** → **Records** → **Add
record**:

| Type | Name | IPv4 address | Proxy status | TTL |
|---|---|---|---|---|
| `A` | `ai` | `192.168.5.240` | **DNS only** (grey cloud) | Auto |

- **DNS only, never proxied.** An orange-cloud record would answer with
  Cloudflare's addresses, and Cloudflare's proxy would then try to reach
  `192.168.5.240` from the internet. It cannot, because that is a private
  address, so every client would get a Cloudflare error page. And LAN traffic
  would leave the building.
- **No tunnel public hostname for this name.** One would create a proxied
  CNAME for `ai.zeoenergy.com` that clashes with this record.
- The record is what clients resolve. Issuance does not need it: DNS-01 only
  uses the TXT record `edge-certs` creates and deletes. It does publish the
  box's private address to anyone who looks the name up, which says little
  but is not nothing.

### Check DNS-rebind protection

Many routers and LAN resolvers drop a public DNS answer that points at a
private address (DNS-rebinding protection). From a **LAN laptop**, not the
box:

```bash
nslookup ai.zeoenergy.com            # the LAN's resolver
nslookup ai.zeoenergy.com 1.1.1.1    # a public resolver, for comparison
```

(On Windows PowerShell, `Resolve-DnsName ai.zeoenergy.com` works too.) Both
must answer `192.168.5.240`. If the public resolver does and the LAN's does not
(no answer, `NXDOMAIN`, or `Server failed`), the LAN resolver is filtering it.
Fix it on the router or LAN DNS server, in order of preference:

1. **A local DNS override** (a host override / static DNS entry):
   `ai.zeoenergy.com` → `192.168.5.240`. LAN clients then do not depend on
   public DNS at all.
2. **Exempt the domain** from rebind protection: for example
   `rebind-domain-ok=/zeoenergy.com/` in dnsmasq, or `private-domain:
   "zeoenergy.com"` in Unbound (pfSense / OPNsense custom options).
3. For one machine only: `192.168.5.240 ai.zeoenergy.com` in its hosts file.

### What the token can and cannot do

**It can** list, create, change and delete **any DNS record in
`zeoenergy.com`**: `chat`, `api`, the MX, SPF, DKIM and DMARC records, all of
them. Whoever holds it could therefore redirect the company's web and mail
traffic, or prove control of any name in the zone and get a trusted
certificate for it, the same way `edge-certs` does. That is the blast radius:
the whole zone's DNS.

**It cannot** touch other zones, the account, members, billing or other
tokens, the tunnel or anything in Zero Trust (Access, `cloudflared`
configuration), cache and WAF rules, or Workers, and it cannot read anyone's
traffic.

Cloudflare cannot scope a token to a single record, so keep the rest tight:

- one zone only (never all zones, never the Global API Key);
- IP filtering if the office's address is stable;
- it lives only in the box's `.env`, and only the `edge-certs` container
  receives it (not the edge). `edge-certs` keeps it in a `0600` file on a
  tmpfs and never logs it ([EDGE.md § TLS](../edge/EDGE.md#how-it-works));
- changes made with it show up in Cloudflare's **Audit Log** (Manage Account
  → Audit Log).

### Rotating or revoking the token

- **Rotate:** My Profile → API Tokens → the token's **⋯** menu → **Roll**
  (a new secret with the same permissions; the old one stops working at once),
  or create a new token as above. Put the new value in `.env`, run
  `make up edge` (it recreates only `edge-certs`), then delete the old token if
  you created a new one. Check with `docker exec edge-certs certbot renew
  --dry-run`. More in [EDGE.md § Rotating the Cloudflare
  token](../edge/EDGE.md#rotating-the-cloudflare-token).
- **Revoke** (a leak, or someone leaving): **Delete** the token in the same
  menu. Nothing breaks right away: the token is only used when a certificate
  is issued or renewed. The current certificate keeps serving until it
  expires, and `edge-certs` logs `Invalid access token` (code 9109) at the next
  renewal attempt, so set a new token within a few weeks. After a leak, also
  look through the zone's DNS records and the Audit Log for changes you did not
  make.

---

## Triage

```bash
# Origin failures, grouped by which rule is failing
make -s logs cloudflared FOLLOW=0 TAIL=all 2>&1 | grep -oE 'ingressRule=[0-9]+ originService=[^ ]+' | sort | uniq -c | sort -rn

# The routing table this connector actually pulled
make -s logs cloudflared FOLLOW=0 TAIL=all 2>&1 | grep 'Updated to new configuration'

# Connections registered — expect four (one per Cloudflare edge colo) per
# container; logs since the last `make up cloudflared` swap cover only the new copy
make -s logs cloudflared FOLLOW=0 TAIL=all 2>&1 | grep -c 'Registered tunnel connection'
```

| Symptom | Cause |
|---|---|
| 502 on ~half of requests, reload "fixes" it | Two connectors on one tunnel; one can't reach the origin |
| 502 on every request to one hostname | That hostname's origin is unreachable from the container |
| `lookup <name> … server misbehaving` | Docker service name dialed from a non-Docker connector, or the service is down |
| `dial tcp [::1]:<port>: connection refused` | Host-port origin used from inside the container |
| `control stream encountered a failure while serving`, never registers | Stale credentials after a token reset |
| 403 with a "Sign in to Zeo AI Chat" page | Not an error — oauth2-proxy serves its sign-in page with a 403 status |

`Your version … is outdated` is advisory. Bump with
`make build cloudflared && make up cloudflared`; the tunnel reconnects in seconds.
