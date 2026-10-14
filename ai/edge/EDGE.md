# Edge and zero-downtime deploys

`make up` used to run `docker compose up -d`, which stops a container before
its replacement starts. Anyone using the service saw errors for the whole
restart, which is minutes for LiteLLM. Now `make up` runs
[`scripts/deploy.py`](../../scripts/deploy.py). For services that can run
twice, it starts the new container **next to** the old one, waits until the new
one is healthy, and only then retires the old one.

A host port can be bound by only one container, so a service with two copies
cannot publish its own port. **`ai/edge`** is a small HAProxy that publishes
those ports instead and forwards to whichever copies are healthy.

Compose file: [`docker-compose.edge.yml`](docker-compose.edge.yml).
Config: [`haproxy.cfg`](haproxy.cfg). Image tag: `EDGE_HAPROXY_VERSION` in `.env`.

- [How `make up` deploys](#how-make-up-deploys)
- [Which services swap](#which-services-swap)
- [The edge](#the-edge)
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
| sandbox | `sandbox-proxy`, `sandbox-egress` | `sandbox-db`: Postgres. `sandbox-runner`: an in-process slot semaphore, and its reaper would reap the other copy's sandboxes |
| trino | `trino-mcp`, `superset` | `*-db`: Postgres. `lake-s3`: data directory. `hive-metastore`: migrates on start, and Trino caches its thrift connection. `trino-coordinator`: a cluster has one coordinator. The two `*-init` services are oneshot. |
| semantic-router | `vllm-sr-router`, `vllm-sr-envoy` | `vllm-sr-dashboard`: host port. `vllm-sr-models-init` is oneshot. |
| supabase | `supabase-api`, plus `auth`, `rest`, `storage`, `imgproxy`, `meta`, `studio`, `functions` (all as `supabase-<svc>-<hash>`) | `supabase-db`: Postgres. `realtime`: a logical replication slot. `supavisor`: migrates on start, and its TCP pooler ports. |
| vllm, llama, unsloth, detector | — | GPU or RAM, and host ports |
| classifier | — | `classifier-db`: Postgres. `classifier`: on startup, `recover()` requeues the running copy's jobs (`ai/classifier/jobs/queue.py`). |
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

| Host port (var, default) | Edge listener | Forwards to | Health check |
|---|---|---|---|
| `PORT_LITELLM` 4001 | 4001 | `litellm:4000` | `GET /health/liveliness` |
| `PORT_OAUTH2_PROXY` 4180 | 4180 | `oauth2-proxy:4180` | `GET /ping` |
| `PORT_KOKORO_API` 8004 | 8004 | `kokoro-api:8000` | `GET /health` |
| `PORT_MADLAD_API` 8008 | 8008 | `madlad-api:8000` | `GET /health` |
| `PORT_SEARXNG` 8009 | 8009 | `searxng:8080` | `GET /healthz` |
| `PORT_SANDBOX_PROXY` 8011 | 8011 | `sandbox-proxy:80` | `GET /health` |
| `PORT_SUPERSET` 8016 | 8016 | `superset:8088` | `GET /health` |
| `PORT_SUPABASE_API` 8022 | 8022 | `supabase-api:8000` | TCP connect |
| `PORT_SEMANTIC_ROUTER` 8025 | 8025 | `vllm-sr-envoy:8899` | TCP connect |

- **Listener ports.** Inside the container, the listener ports are fixed at the
  default host numbers. If you change a `PORT_*` value in `.env`, only the left
  side of the mapping moves; `haproxy.cfg` does not change.
- **Health checks.** They copy each service's own compose healthcheck. The two
  Envoy gateways get a TCP check because every route needs a key.
- **Address resolution.** Each section resolves `<service>` through Docker DNS
  (`127.0.0.11`) every second. `server-template app 3` holds up to three
  copies; `init-addr none` lets the edge start before the services exist.
- **Timeouts.** The default `timeout server` and `timeout client` are 300 s.
  `litellm`, `oauth2-proxy` and `vllm-sr-envoy` get 900 s, because an `auto`
  turn can be silent for 870 s. `timeout tunnel 1h` covers WebSockets: Supabase
  Realtime and Studio, Open WebUI via oauth2-proxy, and Streamlit/Vite in
  sandboxes.
- **Stats.** HAProxy's stats page is on `127.0.0.1:8404` *inside* the
  container only, never published. `deploy.py` reads it through `docker exec`.

**Who goes through the edge.** Only LAN and `localhost` clients on the
published ports, such as Claude Code at `localhost:4001`, curl, and DBeaver
for Superset. Container-to-container traffic never does: Open WebUI →
`litellm:4000`, cloudflared → `oauth2-proxy:4180`, oauth2-proxy →
`superset:8088`, and so on all go straight to the service by DNS. Public chat
traffic (cloudflared → oauth2-proxy) never touches the edge.

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

---

## One-time cutover

Until a stack's first new-style `make up`, its old container (e.g. `litellm`)
still holds its host port. So the edge can start only after **every** stack it
fronts has been deployed once. Do it in one sitting:

1. **Pull the repo on the box and check the tools.** You need
   `docker compose version` (`config --hash` and `up --scale` with
   `--no-recreate`) and `python3 --version` (3.10 or later).
2. **Dry run.** Run `make up <stack> DRY_RUN=1` for each stack below and read
   the plan. The first real run of every swap service is always a swap: the new
   label and the removed `container_name`/`ports` change its config hash.
3. **Deploy each affected stack:**
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
   make up sandbox
   make up trino
   make up semantic-router
   make up supabase
   ```
   Each swap service comes up without a host port, and its old container frees
   the port when it retires. Skip any stack that is not running on the box; its
   port is free already.
4. **Start the edge:**
   ```bash
   make up edge
   curl -s localhost:4001/health/liveliness
   ```

**Downtime during the cutover.**

- **LAN clients.** From the moment each old copy retires in step 3 until
  `make up edge` binds its port, LAN clients get "connection refused" on that
  port: seconds, if you run the steps back to back.
- **Public chat.** No gap. cloudflared → oauth2-proxy → openwebui goes over
  Docker DNS.
- **The edge itself.** `make up edge` fails with "port is already allocated"
  if a fronted service still holds its port. Run `make up` for that stack and
  retry. `make up edge` also waits for the edge's healthcheck
  (`com.zeo.deploy.wait: healthy`) and fails loudly if it never passes, e.g.
  on a bad `haproxy.cfg`. The edge stack is last in the Makefile, so a full
  `make up` ends with that check.
- **The warnings are expected.** While the edge is not running, every fronted
  swap during step 3 prints a warning about the cutover, and each run ends with
  a `WARNING` listing the ports left without a listener. They stop once
  `make up edge` has run.

**Rolling back.** Run `make clean edge`, revert the compose changes, and
`make up <stack> RECREATE=1` for each stack. The old `ports:` come back.

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

### Changing haproxy.cfg

Edit the file, then run `make up edge`. Compose does not notice a change to a
bind-mounted file, so when the edge's compose config is unchanged, `deploy.py`
reloads it in place instead of leaving it alone. Two labels on the edge
service drive this:

1. **Check** (`com.zeo.deploy.reload.check`): it runs
   `haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg` inside the running edge,
   which reads the same mounted file.
2. **Reload** (`com.zeo.deploy.reload: USR2`): only if the check passes, it
   sends `USR2`. The image's entrypoint runs HAProxy in master-worker mode
   (`-W`), so new workers load the new config while the old ones finish their
   open connections. Nothing is dropped.

A config that fails the check stops `make up` with HAProxy's error. No signal
is sent, so the running edge keeps serving its old config. The reload happens
on every `make up edge`, changed or not; it is hitless, so that costs nothing.
A full `make up` therefore also ends by reloading the edge.

A change to the edge's **compose file** recreates it instead, with a second or
two of LAN downtime. That covers a new `ports:` entry (adding a fronted
service) and an `EDGE_HAPROXY_VERSION` bump.

```bash
make up edge                    # validate + hitless reload (or recreate, if the compose config changed)
make up edge DRY_RUN=1          # say which it would do
```

The whole `ai/edge/` directory is mounted, not the single file, so an editor
or `git pull` that replaces `haproxy.cfg` with a new inode is still seen.

---

## Limits

- **Envoy does not drain.**
  - `vllm-sr-envoy` and `supabase-api` are Envoy, and Envoy exits on SIGTERM
    without draining. A request in flight on the *retiring* copy at that moment
    fails; new requests already go to the new copy.
  - `vllm-sr-router` is in the same position if it does not handle SIGTERM
    gracefully. Every `auto` request holds an ext_proc stream to it.
- **Envoy pins one upstream address.** `vllm-sr-envoy` reaches `litellm` and
  `vllm-sr-router` through `LOGICAL_DNS` clusters, which use one resolved
  address at a time and re-resolve about every 5 s. `envoy.yaml` is generated,
  so this cannot be changed here. When `litellm` or `vllm-sr-router` swaps, a
  routed `auto` request can fail for up to one DNS refresh if Envoy's address
  was the retiring copy.
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
  use `docker compose … up -d --force-recreate <svc>`.
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
- **Recreate-class services** get a one-line YAML comment saying why.

---

## Verifying on the box

1. **Locally:**
   ```bash
   uv run --no-sync pytest unit-tests/deploy -q
   make -n up litellm
   ```
2. **Tools:** check `docker compose version` and `python3 --version`, then
   `make up litellm DRY_RUN=1`.
3. **Cutover:** follow [One-time cutover](#one-time-cutover).
4. **Zero downtime:**
   - Run `while sleep 0.2; do curl -fsS -o /dev/null localhost:4001/health/liveliness || echo FAIL; done`
     plus one long streamed `auto` chat in Open WebUI.
   - Edit `ai/litellm/litellm_config.yaml`, even just a comment, and run
     `make up litellm`. The mount content is part of the hash, so this now
     produces a new name and swaps.
   - Expect no `FAIL`, the stream completes, and `docker ps` shows
     `litellm-<hash>` with the old copy gone.
   - A second `make up litellm` prints `up to date`.
5. **Failure path:** break the litellm config so its healthcheck fails, then
   `make up litellm`. Expect exit 1, the old copy still serving, and the new
   one removed with its logs printed.
6. **SIGTERM reaches the server:** `time docker stop <idle swap container>`
   should return in seconds, not the full grace period.
7. **A recreate service:** `make up classifier` prints its recreate reason and
   behaves as before.
