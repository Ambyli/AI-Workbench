# Edge and zero-downtime deploys

`make up` used to run `docker compose up -d`, which stops a container before
its replacement starts. Anyone using the service saw errors for the whole
restart, which is minutes for LiteLLM. Now `make up` runs
[`scripts/deploy.py`](../../scripts/deploy.py). For services that can run
twice, it starts the new container **next to** the old one, waits until the new
one is healthy, and only then retires the old one.

A host port can be bound by only one container, so a service with two copies
cannot publish its own port. **`ai/edge`** is a small HAProxy that publishes
those ports instead and forwards to whichever copies are healthy. It also
fronts three recreate-class APIs so their restarts answer `503` instead of
refusing connections, filters LAN clients by source address, limits
concurrent requests per API key on LiteLLM's LAN port, gives the semantic
router a LiteLLM address that never disappears during a swap, and exports
Prometheus metrics and a read-only status page.

Compose file: [`docker-compose.edge.yml`](docker-compose.edge.yml).
Config: [`haproxy.cfg`](haproxy.cfg). Image tag: `EDGE_HAPROXY_VERSION` in `.env`.

- [How `make up` deploys](#how-make-up-deploys)
- [Which services swap](#which-services-swap)
- [The edge](#the-edge)
  - [LAN allow-list](#lan-allow-list)
  - [Per-key concurrency limit](#per-key-concurrency-limit)
  - [When no copy is healthy: 503](#when-no-copy-is-healthy-503)
  - [The semantic router's internal listener](#the-semantic-routers-internal-listener)
  - [Metrics](#metrics)
  - [Status page](#status-page)
  - [What is deliberately not behind the edge](#what-is-deliberately-not-behind-the-edge)
- [One-time cutover](#one-time-cutover)
- [Operating it](#operating-it)
- [Limits](#limits)
- [Adding a service](#adding-a-service)
- [Verifying on the box](#verifying-on-the-box)

---

## How `make up` deploys

`make up <stack> [service...]` runs:

```bash
python3 scripts/deploy.py -f ai/<stack>/docker-compose.<stack>.yml --env-file .env -p ai-<stack> [service...]
```

It takes the requested services plus everything they `depends_on`, in
dependency order, which keeps the old `up -d svc` semantics. With no stack,
it loops over every stack as before, with `edge` last. It prints one status
line per service and exits non-zero at the first failure.

| Class | Applies to | What happens | Status line |
|---|---|---|---|
| **swap** | labelled `com.zeo.deploy: swap` | Side-by-side swap, below | `swap -> <name>-<hash> (replacing …)` or `up to date (<name>-<hash>)` |
| **oneshot** | `restart` is `no` or unset (`vllm-sr-models-init`, `lake-s3-init`, `trino-auth-init`) | `up -d --no-deps`, then `docker wait`. A non-zero exit stops the deploy and prints the logs. | `oneshot` |
| **recreate** | everything else, by default | `up -d --no-deps <svc>`, exactly as before: compose recreates it only if its config or image changed | `recreate (downtime): <reason>`, or `recreate-class, unchanged …` when compose will leave it alone |

A swap-labelled service still falls back to recreate, with the reason printed,
in any of these cases:

- it has a `container_name`, published `ports:`, a GPU reservation, or no healthcheck;
- `network_mode` is `host`, `container:` or `service:`;
- `scale`/`replicas` is above 1;
- `pull_policy` is `always` or `build`.

Because the script runs compose with `--no-deps`, it waits for `depends_on`
conditions itself before each service: `service_healthy`, `service_started`
and `service_completed_successfully`.

### The swap

For service `S`:

1. **Name.** The container name is `<prefix>-<hash>`. `<prefix>` is the service key, or the
   `com.zeo.deploy.name` label when set (`supabase-auth`, `ai-cloudflared`).
   `<hash>` is the first 12 hex characters of
   `sha256(docker compose config --hash=S + image ID + bind-mount content)`:
   - **Image ID.** It catches a `make build` that rebuilds under the same tag.
   - **Bind-mount content.** It covers every bind-mounted file inside the
     repo. Bind-mounted directories are covered by their sorted relative paths
     and file contents. A directory over 2,000 files or 50 MB is skipped with a
     printed note. `/var/run/docker.sock` and anything outside the repo are
     never hashed. Compose's own config hash ignores mount content, so without
     this, editing `ai/litellm/litellm_config.yaml` or `overflow.py` would never
     deploy.
   - **Missing image.** If the image is not on the box yet, the script runs
     `docker compose pull` or `build` first, so the hash is stable.
2. **Reconcile.**
   - Non-running copies of `S` are removed. Otherwise compose would restart
     them in place of a scale-up.
   - If exactly one copy is running and it is already `<prefix>-<hash>` and
     healthy, the status is `up to date` and nothing changes. A second `make up`
     is therefore a no-op.
   - A copy that has the target name but is not healthy is renamed
     `<name>-old-<id>` and retired with the others. It is not removed first, so
     it can keep serving while the new copy starts.
3. **Start the new copy.** The script runs
   `docker compose up -d --no-deps --no-recreate --scale S=<running+1> S`.
   With `--no-recreate`, compose leaves the existing copies alone, even though
   their config has changed. It creates exactly one new container from the
   current config, which the script renames. Compose tracks containers by
   label, not by name, so the rename is safe.
4. **Wait for healthy.** The timeout is the healthcheck's
   `start_period + interval × retries + 60 s`. Override it with the
   `com.zeo.deploy.timeout` label, e.g. `"15m"`. If the new copy turns
   unhealthy, exits, or times out:
   - its last 100 log lines are printed;
   - it is removed, and the old copy keeps serving;
   - the script exits 1 and deploys nothing after it.
5. **Wait for the edge.** A service is *fronted* when `haproxy.cfg` has a
   `listen` section named after `S`; services the edge does not front skip this
   step. For a fronted service the script polls HAProxy's stats until the new
   copy's address is `UP`, for up to 60 s. It aborts as in step 4, rather than
   retiring the old copy blind, when:
   - the edge never marks the new copy `UP`;
   - the stats can't be read;
   - the running edge has no backend `S`, which means it is running an older
     `haproxy.cfg`. Run `make up edge` to reload it, see
     [Changing haproxy.cfg](#changing-haproxycfg).

   Whether the edge is running is checked **before** the new copy starts:

   | Edge not running, and… | What `make up` does |
   |---|---|
   | the old copy still publishes the host port itself (the [one-time cutover](#one-time-cutover)) | swaps, with a warning |
   | nothing is running yet (a fresh box; a full `make up` brings the edge up last) | swaps, with a warning |
   | the old copy has no host port (the edge went missing after the cutover) | **refuses**, and touches nothing. Run `make up edge` first, or `FORCE=1` to swap anyway. |

   Whenever a fronted service was swapped while the edge was down, the run
   ends with a `WARNING` naming the LAN ports that have no listener until
   `make up edge`.

   Both edge checks belong to the **swap** path only. The three recreate-class
   services the edge fronts (`classifier`, `sandbox-runner`, `detector`) are
   recreated exactly as before; `make up` never asks the edge about them.
6. **Retire the old copy.** `docker stop` honours the service's
   `stop_grace_period`, so in-flight requests finish (900 s for `litellm`,
   `oauth2-proxy` and `vllm-sr-router`). Then `docker rm`.

**Why callers see no errors during the swap:**

- **Inside Docker,** Docker DNS returns both IPs for the service name while both
  copies run. When the old copy closes its listener on SIGTERM, new connections
  to it are refused immediately. Python (`socket.create_connection`, httpx/anyio,
  aiohttp) and Go clients then try the next address. Open WebUI → LiteLLM and
  cloudflared → oauth2-proxy work this way.
- **From the LAN,** traffic goes through the edge. The old copy fails its
  HAProxy health check, and `retry-on conn-failure` plus `option redispatch 1`
  resend a refused connection to the new copy.
- **From the semantic router's Envoy,** traffic to LiteLLM goes through the
  edge's internal listener too, see
  [The semantic router's internal listener](#the-semantic-routers-internal-listener).

Only a failed *connection* is retried, never a request that reached a server.
Nothing runs twice.

### Switches

| Make | Script | Effect |
|---|---|---|
| `RECREATE=1` | — | The old behaviour: plain `docker compose up -d` |
| `DRY_RUN=1` | `--dry-run` | Prints the plan (order, class, reason, target name, what would be removed or retired) and changes nothing. If an image is not on the box yet, the printed hash uses a placeholder for it. |
| `FORCE=1` | `--force` | Swaps swap-class services even when they are up to date, e.g. to pick up a change in a skipped directory. Also lets a fronted service swap while the edge is down after the cutover. |

Only the literal value `1` switches these on. A `DRY_RUN=true` left in the
environment, such as roofix's `.env` setting if it was exported, does nothing.

---

## Which services swap

| Stack | Swap (no downtime) | Recreate, and why |
|---|---|---|
| litellm | `litellm` | `db`: Postgres data directory. `prometheus`: the TSDB is locked by one process. |
| oauth2-proxy | `oauth2-proxy`, `oauth2-assets` | — |
| cloudflared | `cloudflared` (`ai-cloudflared-<hash>`) | — |
| searxng | `searxng` | — |
| kokoro | `kokoro-api` | `kokoro-app`: GPU |
| madlad | `madlad-api` | `madlad-app`: GPU |
| sandbox | `sandbox-proxy`, `sandbox-egress` | `sandbox-db`: Postgres. `sandbox-runner`: an in-process slot semaphore, and its reaper would reap the other copy's sandboxes. Its port is published by the edge, which answers `503` while it restarts. |
| trino | `trino-mcp`, `superset` | `*-db`: Postgres. `lake-s3`: data directory. `hive-metastore`: migrates on start, and Trino caches its thrift connection. `trino-coordinator`: a cluster has one coordinator. The two `*-init` services are oneshot. |
| semantic-router | `vllm-sr-router`, `vllm-sr-envoy` | `vllm-sr-dashboard`: host port. `vllm-sr-models-init` is oneshot. |
| supabase | `supabase-api`, plus `auth`, `rest`, `storage`, `imgproxy`, `meta`, `studio`, `functions` (all as `supabase-<svc>-<hash>`) | `supabase-db`: Postgres. `realtime`: a logical replication slot. `supavisor`: migrates on start, and its TCP pooler ports. |
| vllm, llama, unsloth | — | GPU or RAM, and host ports |
| detector | — | `detector`: GPU. Its port is published by the edge, which answers `503` while it restarts or loads its model. |
| classifier | — | `classifier-db`: Postgres. `classifier`: on startup, `recover()` requeues the running copy's jobs (`ai/classifier/jobs/queue.py`). Its port is published by the edge, which answers `503` while it restarts. |
| openwebui | — | SQLite `webui.db`, boot migrations, in-process websockets |
| n8n | — | `n8n-db`: Postgres. `n8n`: its scheduler would fire twice. |
| roofix | — | `roofix-db`: Postgres. `roofix`: APScheduler would double-tick, and the processed-email store upserts rather than claims. |
| interceptor | — | Chrome profile directories and the debug-port pool |
| open-terminal | — | Per-user accounts and PTY sessions |
| edge | — | It owns the host ports |

Every recreate-class service has a YAML comment in its compose file saying why.
A recreate-class container keeps its fixed `container_name`, so
`docker exec <name>` still works for it. For a swap-class container, use
`make exec <stack> <service> CMD='…'` and `make logs <stack> <service>`.
Both find the container by its compose labels.

---

## The edge

One `edge` container on `ai_shared`, running `haproxy:<EDGE_HAPROXY_VERSION>-alpine`
(3.4 LTS, pinned to a patch tag). It publishes the ports below with the same
`PORT_*` variable and the same bind address (all interfaces) the services used
before. It does not mount `docker.sock`, so `sandbox-runner` stays the only
container that does.

**Published (LAN) listeners** — each one the first `bind … name lan` of a
`listen <service-key>` section:

| Host port (var, default) | Edge listener | Forwards to | Health check | Class | Timeout |
|---|---|---|---|---|---|
| `PORT_LITELLM` 4001 | 4001 | `litellm:4000` | `GET /health/liveliness` | swap | 900 s |
| `PORT_OAUTH2_PROXY` 4180 | 4180 | `oauth2-proxy:4180` | `GET /ping` | swap | 900 s |
| `PORT_KOKORO_API` 8004 | 8004 | `kokoro-api:8000` | `GET /health` | swap | 300 s |
| `PORT_MADLAD_API` 8008 | 8008 | `madlad-api:8000` | `GET /health` | swap | 300 s |
| `PORT_SEARXNG` 8009 | 8009 | `searxng:8080` | `GET /healthz` | swap | 300 s |
| `PORT_SANDBOX_PROXY` 8011 | 8011 | `sandbox-proxy:80` | `GET /health` | swap | 300 s |
| `PORT_SUPERSET` 8016 | 8016 | `superset:8088` | `GET /health` | swap | 300 s |
| `PORT_SUPABASE_API` 8022 | 8022 | `supabase-api:8000` | TCP connect | swap | 300 s |
| `PORT_SEMANTIC_ROUTER` 8025 | 8025 | `vllm-sr-envoy:8899` | TCP connect | swap | 900 s |
| `PORT_CLASSIFIER` 8005 | 8005 | `classifier:8000` | `GET /health` | **recreate** | 600 s |
| `PORT_SANDBOX_RUNNER` 8012 | 8012 | `sandbox-runner:8000` | `GET /health` | **recreate** | 300 s |
| `PORT_DETECTOR` 8021 | 8021 | `detector:8000` | `GET /health` (503 until the model is loaded) | **recreate** | 120 s |

**Internal listeners** — never published; reachable inside the container, or
as `edge:<port>` from `ai_shared`:

| Listener | Section | Used by | Filtered? |
|---|---|---|---|
| `127.0.0.1:8404` `/stats` | `frontend stats` | `deploy.py` (stats CSV through `docker exec`), the compose healthcheck | — |
| `:14000` | second bind of `listen litellm` (`name internal`) | `vllm-sr-envoy`'s four chain clusters | no allow-list, no per-key limit |
| `:8405` `/metrics` | `frontend metrics` | the litellm stack's `prometheus` | — |
| `:8407` `/edge/` | `frontend status` | oauth2-proxy → `chat.zeoenergy.com/edge/` | — |

- **Listener ports.** Inside the container, the listener ports are fixed at the
  default host numbers. If you change a `PORT_*` value in `.env`, only the left
  side of the mapping moves; `haproxy.cfg` does not change.
- **The first bind is the LAN bind.** `deploy.py`'s `parse_edge_listens` reads
  only the first `bind` of each `listen` section; litellm's `:14000` is a second
  bind and never counts as a LAN port. Internal-only listeners are `frontend`
  sections, so `deploy.py` never takes them for services.
- **Health checks.** They copy each service's own compose healthcheck. The two
  Envoy gateways get a TCP check because every route needs a key.
- **Address resolution.** Each section resolves `<service>` through Docker DNS
  (`127.0.0.11`) every second. `server-template app 3` holds up to three
  copies; `init-addr none` lets the edge start before the services exist.
- **Timeouts.** The default `timeout server` and `timeout client` are 300 s.
  `litellm`, `oauth2-proxy` and `vllm-sr-envoy` get 900 s, because an `auto`
  turn can be silent for 870 s. The recreate-class APIs are sized from their
  longest request: a classifier submit fetches every `type: "url"` document one
  after another, each bounded by its 120 s fetch timeout (600 s); a
  sandbox-runner spawn waits up to 30 s for readiness and `exec` is capped at
  120 s (300 s); a detector call is an image fetch (≤ 30 s) plus one inference
  behind a lock (120 s). `timeout tunnel 1h` covers WebSockets: Supabase
  Realtime and Studio, Open WebUI via oauth2-proxy, and Streamlit/Vite in
  sandboxes.

**Who goes through the edge.** LAN and `localhost` clients on the published
ports, such as Claude Code at `localhost:4001`, curl, and DBeaver for Superset.
Container-to-container traffic does not: Open WebUI → `litellm:4000`,
cloudflared → `oauth2-proxy:4180`, oauth2-proxy → `superset:8088`, the
LiteLLM pass-throughs → `classifier:8000` / `detector:8000`, and so on all go
straight to the service by DNS. Public chat traffic (cloudflared →
oauth2-proxy) never touches the edge. Two internal callers do, on purpose:
the semantic router's Envoy reaches LiteLLM through `edge:14000`, and
oauth2-proxy reaches the status page at `edge:8407`.

**What backends see.** For edge traffic, the TCP peer is now the edge's
`ai_shared` address. The LAN client's address is appended to
`X-Forwarded-For` (`option forwardfor`). LiteLLM has no IP allow-list
configured, so nothing depends on the peer address today. If you add one,
make it read `X-Forwarded-For`.

**Superset's header-auth trust boundary is tighter, not looser.** Superset runs
`AUTH_REMOTE_USER` and trusts `X-Auth-Request-Email` as the logged-in user with
no password. That is only safe when oauth2-proxy set the header. Before the
edge, `PORT_SUPERSET` published Superset directly on all interfaces, so anyone
on the LAN could send that header and log in as anyone (`TRINO.md` § Header-auth
trust boundary). oauth2-proxy reaches `superset:8088` directly, never through
the edge, so a header arriving on the edge's 8016 is always a spoof. The edge
deletes `X-Auth-Request-Email`, `-User` and `-Preferred-Username`, plus
`X-Forwarded-Email` and `-User`, on that listener. The LAN port can no longer
be used to impersonate anyone. Sign in through `chat.zeoenergy.com/superset/`
as before. If you set `SUPERSET_REMOTE_USER_HEADER` to another header name,
add a `del-header` for it in `haproxy.cfg`.

### LAN allow-list

Every LAN bind refuses a client whose source address is not in
`EDGE_ALLOWED_CIDRS`, with `403` and a one-line body naming the address
(`edge: 203.0.113.7 is not in EDGE_ALLOWED_CIDRS`). It is the first
`http-request` rule of every `listen` section, so a refused client reaches
nothing else.

- **Format: space-separated CIDRs.** HAProxy has no comma-splitting; the config
  uses its multi-word expansion, `src "${EDGE_ALLOWED_CIDRS[*]}"`, which turns
  each space-separated word into one ACL pattern (HAProxy 3.4 manual § 2.3).
  `.env.example` quotes the value:
  `EDGE_ALLOWED_CIDRS="127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"`.
  IPv6 networks work; `0.0.0.0/0 ::/0` allows everyone.
- **A wrong value fails loudly, never open.** A comma-separated list is not a
  valid address and an empty one is refused by an `.alert` at the top of
  `haproxy.cfg`; either way `haproxy -c` fails, so `make up edge` stops with
  HAProxy's error and a running edge keeps its old config.
- **Keep `172.16.0.0/12`.** A request from the box's **own** `localhost`
  (`curl localhost:4001`, Claude Code on the box) is not routed: Docker's
  userland proxy (`docker-proxy`) accepts it on the host and opens a new
  connection to the edge from the Docker bridge gateway, a `172.x` address. So
  that is the source HAProxy sees, not `127.0.0.1`. A LAN client that reaches
  the box's LAN address is DNAT-ed by iptables instead and keeps its real
  source address, which is what the `10/8` and `192.168/16` entries are for.
  The 403 body prints the address HAProxy saw, which makes this easy to check.
- **Internal listeners are never filtered.** litellm's `:14000` (the semantic
  router comes from an `ai_shared` address), `:8404`, `:8405` and `:8407` have no
  allow-list. On litellm's section, where both binds share rules, the reject is
  limited to the LAN bind by `so_name` (the `bind … name lan` label), not by
  `dst_port`: HAProxy reports the pre-DNAT destination port for a connection
  that came through iptables, which would be the operator's `PORT_LITELLM`,
  not 4001.
- **Changing it** changes the edge's compose environment, so `make up edge`
  recreates the edge (a second or two of downtime), not a hitless reload.

### Per-key concurrency limit

On LiteLLM's **LAN** port only, each API key may have
`EDGE_LITELLM_MAX_CONCURRENT_PER_KEY` requests in flight (default 8, `0` turns
it off). The next one gets `429`, `Retry-After: 10` and an OpenAI-style JSON
error.

- **Key.** A stick-table entry per SHA-256 of the `Authorization` header; when
  there is none, of `x-api-key` (Anthropic-style clients send that); when there
  is neither, the client address. The table stores the hex digest, never the
  raw token, so `show table litellm` on the runtime API cannot leak a key.
- **What counts.** `http-request track-sc0` tracks the entry for the life of
  the stream: the request **and its whole response**, streamed or not. A long
  chat holds its slot until its last byte, or until the client disconnects;
  then the count drops. `sc0_trackers` counts the current request too, so
  `gt N` refuses request N+1. (Verified with the 3.4.6 binary: two streaming
  requests on one key, a third gets 429, a different key still gets 200, and
  the slots come back when the streams end or the clients hang up.)
- **Why 8.** Local chat capacity is `qwen3.8-solo` `--max-num-seqs 3` plus the 2
  of `muse-glimmer`'s 8 sequences the classifier leaves free (it takes 6).
  Beyond 5 concurrent generations, requests queue in vLLM and the overflow
  hook spills chains to Claude. 8 lets one client use every local slot with
  headroom for quick calls (model lists, token counts, health checks), while
  stopping one runaway script from flooding the chains, and the Claude
  overflow behind them, on its own. Raise it if a fan-out client (Claude Code
  subagents) hits 429s.
- **LAN clients only.** Open WebUI, n8n, roofix and the semantic router call
  LiteLLM without the LAN port (`litellm:4000`, or the edge's internal
  `:14000`), so they are never limited here. Per-user and per-key budgets for
  them belong in LiteLLM's virtual keys.
- **Changing it** recreates the edge, like the allow-list.

### When no copy is healthy: 503

When every server of a section is down — a recreate-class service
restarting, a detector still loading its model, a swap that failed, a service
not running at all — the edge answers `503` with `Retry-After: 10` and a short
message naming the service, instead of HAProxy's generic page. The default
(in `defaults`, for browser-facing sections) is plain text built from
`%[be_name]`, e.g. `searxng has no healthy copy right now: it is restarting or
being deployed. Retry in a few seconds.` API sections override it with JSON in
the shape their callers expect: OpenAI-style `{"error": {…}}` for `litellm`
and `vllm-sr-envoy`, FastAPI-style `{"detail": "…"}` for `classifier`,
`sandbox-runner`, `detector`, `kokoro-api` and `madlad-api`, `{"message": "…"}`
for `supabase-api`.

That is why `classifier` (`8005`), `sandbox-runner` (`8012`) and `detector`
(`8021`) are now behind the edge although they stay recreate-class: during a
`make up` that recreates one, a client gets a `503` it can retry rather than
"connection refused". Same host port, same URL; their compose files no longer
have `ports:` and keep `container_name`.

### The semantic router's internal listener

`vllm-sr-envoy` forwards each routed `auto` request to LiteLLM. Its
`envoy.yaml` is generated by vllm-sr from `ai/semantic-router/config.yaml`, and
the generator turns each `base_url` host into a `LOGICAL_DNS` cluster, which
uses **one** resolved address at a time and re-resolves about every 5 s. With
`base_url: http://litellm:4000/v1`, that one address could be the LiteLLM copy
a swap was retiring, so a routed request could fail for up to one DNS refresh.

The four `backend_refs` now say `base_url: http://edge:14000/v1`. Envoy pins
the **edge's** address, which never changes during a LiteLLM swap, and the edge
health-checks every LiteLLM copy and retries a refused connection on another.
`:14000` is a second `bind` of `listen litellm`, so it shares the backend, the
health checks and the 900 s timeouts (above Envoy's 840 s listener timeout;
`unit-tests/litellm/test_timeouts.py` enforces the nesting). It is never
published, and it is exempt from the allow-list and the per-key limit: every
routed turn carries the same `SEMANTIC_ROUTER_LITELLM_KEY` from an `ai_shared`
address. The re-rendered `envoy.yaml` and the reason are in
[SEMANTIC_ROUTER.md § Request path](../semantic-router/SEMANTIC_ROUTER.md#request-path).

The price is that routed `auto` turns now need the edge. A `make up edge`
**reload** is hitless; an edge **recreate** (a change to its compose file or
an `EDGE_*` variable) makes Envoy's upstream disappear for a second or two.
LiteLLM's `auto` alias treats that like a router outage and answers from its
`fallbacks: [local-private]`
([SEMANTIC_ROUTER.md § The privacy guarantee](../semantic-router/SEMANTIC_ROUTER.md#the-privacy-guarantee--and-the-router-down-path)).

### Metrics

`frontend metrics` serves HAProxy's built-in Prometheus exporter on
`edge:8405/metrics`, internal only. The official image is built with
`USE_PROMEX=1` (it is in the `make` flags of
[docker-library/haproxy 3.4/alpine/Dockerfile](https://github.com/docker-library/haproxy/blob/0b6d0e8c96b3e06b98929b40260f0fe8b7da6e8d/3.4/alpine/Dockerfile)
for 3.4.6, and `haproxy -vv` of that image lists
`Available services : prometheus-exporter`). The litellm stack's
`prometheus` scrapes it as job `edge` (`ai/litellm/prometheus.yml`): per
backend server state, sessions, response codes (the 403s, 429s and 503s
above), queue and response times.

`prometheus` itself now sits on `ai_shared`. It used to have no `networks:`
block, which left it on its stack's default network only, where none of its
targets (`litellm`, `classifier`, `detector`, `vllm-sr-router`) live. It stays
recreate-class: its TSDB is locked by one process. (Its `litellm` job can
reach `litellm:4000` now, but LiteLLM serves `/metrics` only with the
`prometheus` success callback enabled, which this repo does not do — expect
that one target DOWN with a 404 until someone turns it on.)

### Status page

`frontend status` serves HAProxy's stats page at `edge:8407/edge/`, internal
only, read-only: no `stats admin`, so there are no enable / disable / drain
buttons. `OAUTH2_PROXY_UPSTREAMS` includes `http://edge:8407/edge/`, so it is
at **`https://chat.zeoenergy.com/edge/`** after Google sign-in, the same
pattern as `/n8n/` and `/superset/` (oauth2-proxy forwards the path
unchanged). `/edge/;csv` is the same data as CSV. Anything on `ai_shared` can
read it too; it shows backend addresses and health, no secrets.

### What is deliberately not behind the edge

| Port(s) | Service | Why it stays a direct publish |
|---|---|---|
| `8002` `8003` `8006` `8018` `8019`, `8010` `8017` `8020`, `8000` | vLLM, llama.cpp, unsloth models | GPU / RAM-bound, never swapped. A restart is minutes of model load either way; a 503 instead of a refused connect buys nothing, and LiteLLM (the real client) reaches them directly. |
| `5432`–`5439`, `8023` `8024` | Postgres instances, Supavisor | TCP, not HTTP: the edge's 503 page, allow-list rules and health checks are HTTP-mode. Clients (psql, DBeaver, Trino through `host.docker.internal`) expect raw Postgres. |
| `8013` | trino-coordinator | HTTPS with its own password-file auth and self-signed cert. The edge would have to terminate or pass TLS through, and the cert's SANs name the box. |
| `8014` `8015` | lake-s3 | S3 API with SigV4 signatures over the Host header, and large streamed uploads; recreate-class (data directory). The console is behind oauth2-proxy at `/rustfs/` already. |
| `8007` | openwebui | Bound to `127.0.0.1` on purpose: its trusted-header SSO trusts whoever reaches it, and the loopback bind is part of that trust model (`openwebui/docker-compose.openwebui.yml`). Routing it through a container on `ai_shared` would widen who can reach it. |
| `9090` | prometheus | Recreate-class (TSDB lock), operator-only, and the scraper of the edge itself. |
| `8026` | vllm-sr-dashboard | `127.0.0.1` only, behind a compose profile. |

---

## One-time cutover

Until a stack's first new-style `make up`, its old container (e.g. `litellm`)
still holds its host port. So the edge can start only after **every** stack it
fronts has been deployed once. Do it in one sitting:

1. **Pull the repo on the box and check the tools.** You need
   `docker compose version` (`config --hash` and `up --scale` with
   `--no-recreate`) and `python3 --version` (3.10 or later).
2. **Mirror `.env.example` into `.env`:** `EDGE_ALLOWED_CIDRS` and
   `EDGE_LITELLM_MAX_CONCURRENT_PER_KEY` in the Edge section, and
   `,http://n8n:5678/n8n/,http://edge:8407/edge/` appended to
   `OAUTH2_PROXY_UPSTREAMS` (skip the n8n entry if it is already there).
3. **Dry run.** Run `make up <stack> DRY_RUN=1` for each stack below and read
   the plan. The first real run of every swap service is always a swap: the new
   label and the removed `container_name`/`ports` change its config hash.
4. **Deploy each affected stack:**
   The newly pinned `oauth2-proxy` (`-alpine`) and `cloudflared` images are
   pulled automatically, because `deploy.py` pulls a missing image before
   hashing it.
   ```bash
   make up litellm
   make up oauth2-proxy
   make up cloudflared
   make up searxng
   make up kokoro
   make up madlad
   make up classifier
   make up detector
   make up sandbox
   make up trino
   make up semantic-router
   make up supabase
   ```
   Each swap service comes up without a host port, and its old container frees
   the port when it retires. `classifier`, `detector` and `sandbox-runner` are
   recreated without their `ports:`, which frees theirs. Skip any stack that is
   not running on the box; its port is free already.
5. **Start the edge:**
   ```bash
   make up edge
   curl -s localhost:4001/health/liveliness
   ```

**Downtime during the cutover.**

- **LAN clients.** From the moment each old copy retires in step 4 until
  `make up edge` binds its port, LAN clients get "connection refused" on that
  port: seconds, if you run the steps back to back.
- **Routed `auto` turns.** From `make up semantic-router` until `make up edge`,
  Envoy's upstream (`edge:14000`) does not exist yet; LiteLLM's `auto` falls
  back to `local-private` for that window, as it does whenever the router is
  down.
- **Public chat.** No gap. cloudflared → oauth2-proxy → openwebui goes over
  Docker DNS.
- **The edge itself.** `make up edge` fails with "port is already allocated"
  if a fronted service still holds its port. Run `make up` for that stack and
  retry. `make up edge` also waits for the edge's healthcheck
  (`com.zeo.deploy.wait: healthy`) and fails loudly if it never passes, e.g.
  on a bad `haproxy.cfg`. The edge stack is last in the Makefile, so a full
  `make up` ends with that check.
- **The warnings are expected.** While the edge is not running, every fronted
  swap during step 4 prints a warning about the cutover, and each run ends with
  a `WARNING` listing the ports left without a listener. They stop once
  `make up edge` has run.

**Rolling back.** Run `make clean edge`, revert the compose changes, and
`make up <stack> RECREATE=1` for each stack. The old `ports:` come back.
Revert `ai/semantic-router/config.yaml` + `envoy.yaml` too, or routed `auto`
turns fall back to `local-private` with no edge to reach.

### Upgrading an edge that is already running

If the box was cut over before the allow-list, 503 page, internal listeners
and the three recreate-class APIs existed, apply them in this order — each
step frees what the next needs:

```bash
# 1. .env: add EDGE_ALLOWED_CIDRS + EDGE_LITELLM_MAX_CONCURRENT_PER_KEY,
#    append ,http://n8n:5678/n8n/,http://edge:8407/edge/ to OAUTH2_PROXY_UPSTREAMS
make up classifier             # recreate without ports: frees 8005
make up detector               # recreate without ports: frees 8021
make up sandbox                # sandbox-runner recreated without ports: frees 8012
make up edge                   # recreate: new ports, new environment, new haproxy.cfg
make up semantic-router FORCE=1  # Envoy loads the re-rendered envoy.yaml (edge:14000)
make up litellm                # prometheus recreated onto ai_shared, edge scrape job
make up oauth2-proxy           # picks up the new OAUTH2_PROXY_UPSTREAMS
```

Between the first three steps and `make up edge`, those three ports refuse
connections for a few seconds. `FORCE=1` on the semantic router is needed
because Envoy's config reaches it through a named volume, which `deploy.py`
does not hash: without it `vllm-sr-envoy` would report `up to date` and keep
the old `litellm:4000` clusters in memory
([SEMANTIC_ROUTER.md § Applying a config change](../semantic-router/SEMANTIC_ROUTER.md#applying-a-config-change)).

---

## Operating it

```bash
make up litellm                 # deploy: swap if anything changed, else "up to date"
make up litellm DRY_RUN=1       # show the plan
make up litellm FORCE=1         # swap even if unchanged
make up litellm RECREATE=1      # plain docker compose up -d
make logs litellm               # follow logs (compose finds hashed names by label)
make exec litellm litellm CMD='curl -s http://roofix:8080/status'
make ps litellm
```

**Edge status:** pxname, svname, status and addr, one line per copy:

```bash
docker exec edge wget -qO- 'http://127.0.0.1:8404/stats;csv' | cut -d, -f1,2,18,74
```

Or in a browser: `https://chat.zeoenergy.com/edge/`.

### Changing haproxy.cfg

Edit the file, then run `make up edge`. Compose does not notice a change to a
bind-mounted file, so when the edge's compose config is unchanged, `deploy.py`
reloads it in place instead of leaving it alone. Two labels on the edge
service drive this:

1. **Check** (`com.zeo.deploy.reload.check`): it runs
   `haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg` inside the running edge,
   which reads the same mounted file with the container's environment.
2. **Reload** (`com.zeo.deploy.reload: USR2`): only if the check passes, it
   sends `USR2`. The image's entrypoint runs HAProxy in master-worker mode
   (`-W`), so new workers load the new config while the old ones finish their
   open connections. Nothing is dropped.

A config that fails the check stops `make up` with HAProxy's error. No signal
is sent, so the running edge keeps serving its old config. The reload happens
on every `make up edge`, changed or not; it is hitless, so that costs nothing.
A full `make up` therefore also ends by reloading the edge.

A change to the edge's **compose file** or to an `EDGE_*` variable in `.env`
recreates it instead, with a second or two of LAN downtime (and routed `auto`
turns falling back to `local-private` for as long). That covers a new `ports:`
entry (adding a fronted service), the allow-list, the per-key limit and an
`EDGE_HAPROXY_VERSION` bump.

```bash
make up edge                    # validate + hitless reload (or recreate, if the compose config changed)
make up edge DRY_RUN=1          # say which it would do
```

The whole `ai/edge/` directory is mounted, not the single file, so an editor
or `git pull` that replaces `haproxy.cfg` with a new inode is still seen.

`haproxy -c` prints three `log format ignored for frontend …` warnings for the
internal frontends (they have `no log`); they are harmless and do not fail the
check.

---

## Limits

- **Envoy does not drain.**
  - `vllm-sr-envoy` and `supabase-api` are Envoy, and Envoy exits on SIGTERM
    without draining. A request in flight on the *retiring* copy at that moment
    fails; new requests already go to the new copy.
  - `vllm-sr-router` is in the same position if it does not handle SIGTERM
    gracefully. Every `auto` request holds an ext_proc stream to it.
- **Envoy pins one upstream address — closed for LiteLLM, open for the router.**
  `envoy.yaml` is generated, and its clusters are `LOGICAL_DNS`: one resolved
  address at a time, re-resolved about every 5 s.
  - **LiteLLM: closed.** The chain clusters point at `edge:14000`, not
    `litellm:4000`, so a LiteLLM swap no longer fails routed `auto` requests
    ([The semantic router's internal listener](#the-semantic-routers-internal-listener)).
  - **`vllm-sr-router`: still open.** The ext_proc cluster is gRPC to
    `vllm-sr-router:50051` and is left direct. When the router swaps, an `auto`
    request can fail for up to one DNS refresh if Envoy's address was the
    retiring copy; LiteLLM's `auto` fallback then answers it from
    `local-private`.
- **The `auto` path depends on the edge.** An edge recreate is a second or two
  of routed turns falling back to `local-private`. Reloads are hitless.
- **The per-key limit only sees LAN clients.** Open WebUI, n8n, roofix and the
  semantic router never cross the LAN port. Budgets for them are LiteLLM
  virtual-key settings.
- **Two cloudflared connectors during a swap.** The tunnel briefly has two
  connectors. Both are this exact container, with the same config and the same
  reachable origins, so this is not the "two connectors, one can't reach the
  origin" failure in [CLOUDFLARED.md](../cloudflared/CLOUDFLARED.md). It lasts
  only until the old copy retires.
- **Long stops.** Retiring `litellm`, `oauth2-proxy` or `vllm-sr-router` can
  take up to 900 s while long chats finish. `make up` waits for it.
  - Verify that SIGTERM reaches the server: time a `docker stop` on an idle
    copy. A full grace-period wait on an idle container means the entrypoint
    does not forward signals; fix that with `exec` or `init: true`.
- **Recreate-class services ignore bind-mount content, as before.** Only swap
  services hash it. After editing, for example, `ai/vllm/qwen_fixed_chat_template.jinja`,
  use `docker compose … up -d --force-recreate <svc>`. Volume content is not
  hashed either: that is why a router config change needs
  `make up semantic-router FORCE=1`.
- **Directories that hash a lot.**
  - `searxng` mounts its whole folder, so any file there (even `SEARXNG.md`)
    triggers a swap.
  - `functions` mounts `ai/supabase/volumes/functions` read-write. If the
    runtime ever writes there, every `make up supabase` swaps it.

---

## Adding a service

- **New services are recreate by default.** Leave them unlabelled unless two
  copies can safely run at once: no singleton scheduler, no exclusive data
  directory or lock, no GPU, and no startup step that disturbs a running copy.
- **To make one swap-class:**
  - Add `labels: {com.zeo.deploy: swap}` and a healthcheck.
  - Remove `container_name`. Add `com.zeo.deploy.name: <prefix>` if the old
    name should stay recognisable.
  - Never give it `ports:`. Add a `listen <service-key>` section to
    `haproxy.cfg` plus a `ports:` entry in `docker-compose.edge.yml`, reusing
    the service's `PORT_*` variable, then `make up edge`. The new port changes
    the edge's compose config, so this one is a recreate, not a reload.
  - Set `stop_grace_period` to cover the service's longest request.
- **To front a recreate-class HTTP API** (for the 503 only): keep its
  `container_name`, remove its `ports:`, put it on `ai_shared`, and add the same
  `listen` section + edge `ports:` entry. Size `timeout server` from its longest
  request.
- **Every `listen` section** starts with `bind :<port> name lan`, then the two
  allow-list lines (`acl lan_allowed src "${EDGE_ALLOWED_CIDRS[*]}"` and the
  `http-request return status 403 … unless lan_allowed` rule) as its first
  `http-request` rule, an `http-error status 503 … hdr Retry-After 10` naming the
  service if its callers want JSON, and its health check. The repo-invariant
  tests in `unit-tests/deploy/test_deploy.py` check all of this.
- **Recreate-class services** get a one-line YAML comment saying why.

---

## Verifying on the box

1. **Locally:**
   ```bash
   uv run --no-sync pytest unit-tests/deploy unit-tests/litellm -q
   make -n up litellm
   ```
2. **Tools:** check `docker compose version` and `python3 --version`, then
   `make up litellm DRY_RUN=1`.
3. **Cutover:** follow [One-time cutover](#one-time-cutover), or
   [Upgrading an edge that is already running](#upgrading-an-edge-that-is-already-running).
4. **Zero downtime:**
   - Run `while sleep 0.2; do curl -fsS -o /dev/null localhost:4001/health/liveliness || echo FAIL; done`
     plus one long streamed `auto` chat in Open WebUI.
   - Edit `ai/litellm/litellm_config.yaml`, even just a comment, and run
     `make up litellm`. The mount content is part of the hash, so this now
     produces a new name and swaps.
   - Expect no `FAIL`, the stream completes, and `docker ps` shows
     `litellm-<hash>` with the old copy gone. The `auto` chat now also survives
     the swap: its Envoy hop goes through `edge:14000`.
   - A second `make up litellm` prints `up to date`.
5. **Failure path:** break the litellm config so its healthcheck fails, then
   `make up litellm`. Expect exit 1, the old copy still serving, and the new
   one removed with its logs printed.
6. **SIGTERM reaches the server:** `time docker stop <idle swap container>`
   should return in seconds, not the full grace period.
7. **A recreate service:** `make up classifier` prints its recreate reason and
   behaves as before; meanwhile `curl -si localhost:8005/health` answers `503`
   with `Retry-After: 10` and a JSON body naming the classifier, then `200`.
8. **Allow-list:** from the box, `curl -s localhost:4001/health/liveliness`
   works (source `172.x`, inside `172.16.0.0/12`). Temporarily set
   `EDGE_ALLOWED_CIDRS="10.0.0.0/8"`, `make up edge`, and the same curl gets
   `403 edge: 172.x.x.x is not in EDGE_ALLOWED_CIDRS`; an `auto` chat still
   works (internal bind). Put it back and `make up edge`.
9. **Per-key limit:** with a throwaway key, start 9 concurrent streamed
   requests on `localhost:4001`; the 9th gets `429` with `Retry-After`. The same
   test on `auto` through Open WebUI is never limited.
10. **Metrics and status:** `curl -s localhost:9090/api/v1/targets` shows the
    `edge` job UP, and `classifier`, `detector` and `semantic-router` UP when
    those services run. A `litellm` target that is DOWN with **HTTP 404** is not
    a network problem: LiteLLM mounts `/metrics` only when `prometheus` is in
    its `success_callback` list, which `litellm_config.yaml` does not set.
    `https://chat.zeoenergy.com/edge/` shows the stats page after Google
    sign-in, with no admin buttons.
