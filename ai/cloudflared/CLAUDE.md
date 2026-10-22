# ai/cloudflared

The Cloudflare Tunnel connector: publishes `chat.zeoenergy.com` (→ `oauth2-proxy:4180`), `api.zeoenergy.com` (→ `litellm:4000`) and `rdp.zeoenergy.com` over an outbound-only tunnel. Guide: [`CLOUDFLARED.md`](CLOUDFLARED.md). Compose: `docker-compose.cloudflared.yml`; the only config is `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.

## Rules

- **Ingress rules live in the Zero Trust dashboard, not the repo.** The token names the tunnel; the dashboard supplies the routing table. Before proposing a new public hostname, read `CLOUDFLARED.md § Current ingress rules` — `api.zeoenergy.com` is already LiteLLM's tunnel host. Prefer a path under `chat.zeoenergy.com/<path>/` (behind oauth2-proxy and its Google cookie) over a new hostname.
- **Exactly one connector per tunnel.** A second connector (e.g. a host `systemctl` cloudflared on the same tunnel id) makes ~half of requests 502, per request. Resetting the token does not evict it. The only allowed overlap is the seconds-long `make up cloudflared` swap (`ai-cloudflared-<hash>`, same container twice). `CLOUDFLARED.md § Exactly one connector per tunnel`.
- **Origins are container-relative.** Use `http://<service>:<container port>` on `ai_shared` (`http://litellm:4000`, never `localhost:4001`) and `tcp://host.docker.internal:<port>` for host services; `host.docker.internal` resolves only through the compose file's `extra_hosts: host-gateway`. `CLOUDFLARED.md § Origin addresses are container-relative`.
- **`api.zeoenergy.com` bypasses oauth2-proxy** — LiteLLM's key auth is its only gate. Off the LAN Cloudflare cuts a silent response at ~100 s (`524`); long non-streamed calls belong on the LAN edge.
- **Cloudflare edge-caches `chat.zeoenergy.com`** by file extension, authenticated or not, unless the dashboard bypass rule exists; check `cf-cache-status` first when assets look stale or leak. `CLOUDFLARED.md` § Cache rule — bypass for `chat.zeoenergy.com`.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **`api.zeoenergy.com` is split-horizon** — in public DNS it is this tunnel's proxied record → `litellm:4000`; on the office LAN the router's override sends it to the edge's TLS listener (`ai/edge`, `edge-certs`). **Never add an `A` / `AAAA` record for `api`**, never create `_acme-challenge.api` by hand (edge-certs manages it), and leave the tunnel's public hostname alone. `CLOUDFLARE_DNS_API_TOKEN` (edge-certs' DNS-01 token, "Edit zone DNS" on `zeoenergy.com` only, never in this container) is unrelated to `CLOUDFLARE_TUNNEL_TOKEN`. `CLOUDFLARED.md § DNS-01 certificate for api.zeoenergy.com on the LAN edge (split-horizon)`.
- **Paths under `chat.zeoenergy.com`** — the `chat` rule ends at `oauth2-proxy:4180`; which paths exist behind it is `OAUTH2_PROXY_UPSTREAMS` (`ai/oauth2-proxy/CLAUDE.md`). n8n has no hostname: it is not exposed (`ai/n8n/N8N.md § Access (not exposed)`).
- **Swap-class services publish through the edge** — `cloudflared` is swap-class (`com.zeo.deploy.name: ai-cloudflared`) and publishes no port; it reaches `oauth2-proxy:4180` and `litellm:4000` by Docker DNS, never through the edge. Root `CLAUDE.md` § Deploys (`make up`).

## Env vars

- `CLOUDFLARE_TUNNEL_TOKEN` (SECRET) — identifies the tunnel; the dashboard holds the routing. Rotating it does not create a new tunnel or evict a duplicate connector. Detail: `.env.example` (Cloudflare Tunnel block) and `CLOUDFLARED.md`.
