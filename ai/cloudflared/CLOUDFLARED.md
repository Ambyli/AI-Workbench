# cloudflared — Cloudflare Tunnel connector

Publishes this host's services to the public internet over an outbound-only
Cloudflare Zero Trust tunnel. No inbound firewall rule, no public IP.

Compose file: [`docker-compose.cloudflared.yml`](docker-compose.cloudflared.yml).
Config: `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.

`api.zeoenergy.com` is split-horizon: off the LAN it is this tunnel's public
hostname, and on the office LAN a local DNS override sends the same name to
the LAN edge, whose Let's Encrypt certificate uses Cloudflare's DNS and API
(not the tunnel) with its own token, `CLOUDFLARE_DNS_API_TOKEN`: see
[DNS-01 certificate for api.zeoenergy.com on the LAN edge](#dns-01-certificate-for-apizeoenergycom-on-the-lan-edge-split-horizon).
**Never add an `A` record for `api`** in the zone.

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
| `api.zeoenergy.com` | `http://litellm:4000` | **Bypasses oauth2-proxy** — LiteLLM's own key auth is the only gate. Off-LAN path only: on the office LAN the same name resolves to the box and reaches the LAN edge instead ([split-horizon](#dns-01-certificate-for-apizeoenergycom-on-the-lan-edge-split-horizon)). Leave its DNS record alone; never add an `A` record for `api` |
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

## DNS-01 certificate for api.zeoenergy.com on the LAN edge (split-horizon)

**This section is about Cloudflare's DNS and its API, not about the tunnel's
connector**, even though the name is the tunnel's own public hostname.

`api.zeoenergy.com` is **one name with two paths**. In public DNS it is the
tunnel's public hostname ([Current ingress rules](#current-ingress-rules)):
Cloudflare answers, terminates TLS, and the tunnel carries the request to
`litellm:4000`. On the office LAN, a local DNS override makes the same name
resolve to the box, `192.168.5.240`, so clients reach the LAN edge
([`ai/edge`](../edge/EDGE.md)) directly on port 443. The edge serves LiteLLM
there with a Let's Encrypt certificate for the same name. A client uses
`https://api.zeoenergy.com` everywhere; where it is decides the path.

| | Off the LAN | On the office LAN |
|---|---|---|
| `api.zeoenergy.com` resolves to | Cloudflare's addresses (the proxied tunnel record) | `192.168.5.240` (the router's local override) |
| TLS certificate | Cloudflare's edge certificate | Let's Encrypt, kept by `edge-certs` |
| Path | Cloudflare → tunnel → `litellm:4000` | edge `:443` → `litellm:4000`, no Cloudflare |
| Response timeout | Cloudflare's ~100 s origin response timeout (a `524` past it) | the edge's 900 s |
| Edge allow-list and per-key limit | no: the tunnel calls `litellm:4000` directly | yes ([EDGE.md](../edge/EDGE.md#lan-allow-list)) |
| Response headers | `server: cloudflare` and `cf-ray` | neither |

**Which path did a client take?** Look at the response headers: the tunnel
path always adds `server: cloudflare` and a `cf-ray` header; the edge path has
neither.

The edge's `edge-certs` container gets the certificate with a **DNS-01**
challenge. It creates the TXT record `_acme-challenge.api.zeoenergy.com`
through the Cloudflare API, Let's Encrypt reads it from Cloudflare's
nameservers, and the record is deleted again. `_acme-challenge.api` is a
different name from `api`, so the TXT record sits next to the tunnel's record
without touching it. Issuance needs no `A` record, and Let's Encrypt never
reaches the box. The full checklist (`.env`, port 443, `make up edge`,
moving clients) is in [EDGE.md § TLS](../edge/EDGE.md#tls); the Cloudflare
and DNS steps are here.

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

### Never add an `A` record for `api`

The tunnel already owns `api.zeoenergy.com` in the zone: a proxied (orange
cloud) record pointing at the tunnel, created by its public hostname in the
Zero Trust dashboard. **Leave that record and the public hostname exactly as
they are.**

- **Do not add an `A` or `AAAA` record named `api`**, grey cloud or orange. It
  collides with the tunnel's record. Cloudflare refuses a second record next
  to a `CNAME`, and replacing the tunnel's record to make room would send
  every off-LAN client to a private address it cannot reach: public access to
  LiteLLM breaks.
- **Do not create `_acme-challenge.api` by hand.** `edge-certs` creates and
  deletes it on every issuance and renewal.
- Public DNS never contains the box's private address. The LAN override below
  is the only place `192.168.5.240` appears.

### The LAN override

Make `api.zeoenergy.com` resolve to `192.168.5.240` **on the office network
only**. Override this one name, never the whole zone: `chat.zeoenergy.com` and
everything else must keep resolving publicly.

1. **Preferred: the office router's local DNS.** Depending on the router it is
   called a host override, static DNS entry, local DNS record or DNS rewrite;
   on Pi-hole it is **Local DNS → DNS Records**. Examples: Unbound on pfSense
   or OPNsense, a host override with host `api`, domain `zeoenergy.com`, IP
   `192.168.5.240`; dnsmasq, `host-record=api.zeoenergy.com,192.168.5.240`.
   It applies only to clients using the office resolver, so a laptop that
   leaves the office falls back to public DNS and the tunnel path, with
   nothing to change on the laptop.
   - The client must actually use the router as its DNS server. A machine with
     a hard-coded resolver (`8.8.8.8`, `1.1.1.1`) or DNS-over-HTTPS turned on
     in the OS or the browser bypasses the override and takes the tunnel path.
2. **Fallback: a hosts-file line on one machine**, in
   `C:\Windows\System32\drivers\etc\hosts` (edit as Administrator) or
   `/etc/hosts`:
   ```
   192.168.5.240  api.zeoenergy.com
   ```
   **The line travels with the machine.** Outside the office the laptop still
   sends `api.zeoenergy.com` to `192.168.5.240`, a private address it cannot
   reach there, so every call fails instead of going through the tunnel.
   Remove the line before leaving, or use the router override instead.
3. **On the box itself,** `curl` and the containers use the box's resolver,
   so they resolve the public tunnel name unless the box is pointed at the
   router's override. That is fine: services on the box call `litellm:4000`
   by Docker DNS and never need the name. To test the edge from the box, pin
   the name with `curl --resolve` (below); don't edit the box's `/etc/hosts`.

### Verify

From a **LAN laptop** (on Windows use `curl.exe` and `-o NUL`):

```bash
nslookup api.zeoenergy.com           # expect 192.168.5.240
curl -sS -D - -o /dev/null https://api.zeoenergy.com/health/liveliness
```

Expect `200`, no certificate error, and **no** `cf-ray` or
`server: cloudflare` header. (`curl -sSI` shows the same headers with a HEAD
request, which a GET-only LiteLLM route may answer with `405`; the headers
tell the path either way.)

From the **box**, bypassing whatever the box resolves:

```bash
curl -sS --resolve api.zeoenergy.com:443:127.0.0.1 https://api.zeoenergy.com/health/liveliness
```

From **off the LAN** (a phone hotspot), the same URL must still work, now
with `cf-ray` in the headers: the tunnel path is untouched.

### Troubleshooting DNS

| Symptom | Cause and fix |
|---|---|
| On the LAN, responses carry `cf-ray` and `server: cloudflare`. Requests work, but slowly; long ones fail with a `524` after about 100 s, and the LAN per-key limit never applies | The override is missing for this client: `nslookup api.zeoenergy.com` returns Cloudflare addresses. Check the router entry, check that the client uses the router as its DNS server (no hard-coded resolver, no DNS-over-HTTPS), then flush the client's cache (`ipconfig /flushdns`, `sudo resolvectl flush-caches`). |
| Outside the office, `api.zeoenergy.com` times out (`Failed to connect to api.zeoenergy.com port 443`) | A stale hosts-file line still pins the name to `192.168.5.240`. Remove it and flush the DNS cache. |
| On the LAN, `Failed to connect … port 443: Connection refused` | The edge does not publish 443: `PORT_LITELLM_TLS` is not `443` in the box's `.env`, or the edge was not recreated since. `make up edge`. |
| On the LAN, a certificate error | The edge still serves the staging certificate, the placeholder, or a certificate for an old name. See [EDGE.md § Troubleshooting TLS](../edge/EDGE.md#troubleshooting-tls). |
| Off the LAN, `api.zeoenergy.com` stopped working after a DNS change | Someone added or changed a record named `api`. Delete the `A` record and restore the tunnel's public hostname in the Zero Trust dashboard ([above](#never-add-an-a-record-for-api)). |

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
