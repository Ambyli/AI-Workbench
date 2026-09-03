# cloudflared — Cloudflare Tunnel connector

Publishes this host's services to the public internet over an outbound-only
Cloudflare Zero Trust tunnel. No inbound firewall rule, no public IP.

Compose file: [`docker-compose.cloudflared.yml`](docker-compose.cloudflared.yml).
Config: `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.

Ingress rules (which hostname maps to which origin) live in the **Cloudflare
Zero Trust dashboard**, not in this repo. The token identifies the tunnel; the
dashboard supplies the routing table, which the connector pulls on connect and
logs as `Updated to new configuration config=… version=N`.

---

## Exactly one connector per tunnel

**A tunnel may only have one connector on this host. Two is a bug, and it
fails intermittently rather than cleanly.**

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

## Triage

```bash
# Origin failures, grouped by which rule is failing
docker logs ai-cloudflared 2>&1 | grep -oE 'ingressRule=[0-9]+ originService=[^ ]+' | sort | uniq -c | sort -rn

# The routing table this connector actually pulled
docker logs ai-cloudflared 2>&1 | grep 'Updated to new configuration'

# Connections registered — expect four (one per Cloudflare edge colo)
docker logs ai-cloudflared 2>&1 | grep -c 'Registered tunnel connection'
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
