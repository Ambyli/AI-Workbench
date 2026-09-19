# Open Terminal

[Open Terminal](https://github.com/open-webui/open-terminal) is the code-execution backend for this Open WebUI deployment. It is a real Linux container — Python, Node 22, gcc, git, ffmpeg, pandoc, LaTeX, LibreOffice, and the usual data-science wheels — with a persistent per-user home directory, driven over a small REST API.

Open WebUI v0.11.3 integrates it natively: the terminal's OpenAPI operations become **native function-calling tools** on the model, and the chat UI grows a file-browser sidebar, file previews, drag-and-drop uploads into the terminal's filesystem, and an interactive xterm pane.

Compose file: [`docker-compose.open-terminal.yml`](docker-compose.open-terminal.yml). Operator config lives in the root `.env` (`OPEN_TERMINAL_*`, plus `OPENWEBUI_ENABLE_CODE_INTERPRETER`); see the config-var table in the root [`CLAUDE.md`](../../CLAUDE.md). HTTP surface: [`open-terminal.postman_collection.json`](open-terminal.postman_collection.json).

## Why it replaces Pyodide

The old Code Interpreter ran Python in the **browser** via Pyodide (`CODE_EXECUTION_ENGINE=pyodide`). That meant:

| Pyodide code interpreter | Open Terminal |
|---|---|
| WASM Python in the user's tab | Real CPython in a container on the box |
| Pure-Python wheels only, no C extensions that aren't prebuilt | `pip install` anything; apt packages too |
| No shell, no git, no ffmpeg, no compilers | Full Debian userland |
| Nothing persists between messages | Per-user `/home/<user>` survives chats, restarts, image bumps |
| Files uploaded to chat go to the RAG store, not to the code | `chat_uploads: "filesystem"` drops them straight into the user's home |
| One prompt-injected tool | Every OpenAPI operation is a native function-calling tool |

The two are **mutually exclusive in the composer**: `src/lib/components/chat/MessageInput.svelte` at v0.11.3 force-clears `codeInterpreterEnabled` the moment a terminal is selected. Leaving both enabled only produces a second, dead button — hence `OPENWEBUI_ENABLE_CODE_INTERPRETER=false`.

`ENABLE_CODE_EXECUTION` (`code_execution.enable`) is a **different** setting and is deliberately left alone: it is the little "Run" button on a code block in an assistant message, executed browser-side with no server reach.

## What runs

| Container | Purpose | Network | Host port |
|---|---|---|---|
| `open-terminal` | Shell + file API (`ghcr.io/open-webui/open-terminal:${OPEN_TERMINAL_VERSION}`, port 8000 in-container) | `terminal_net` **only** | _(none — by design)_ |

Plus one change to an existing container: `openwebui` joins `terminal_net` in addition to `ai_shared`.

State lives in the `open_terminal_home` named volume mounted at `/home`. A `docker compose … down -v` wipes every user's workspace.

### Image variant

The **default (fat, ~4 GB)** variant is required, not preferred. `slim` and `alpine` support neither `OPEN_TERMINAL_MULTI_USER` nor the runtime `OPEN_TERMINAL_PACKAGES` / `_PIP_PACKAGES` / `_NPM_PACKAGES` installs, and `openshift` additionally drops the egress firewall.

### Image tag has no `v`

The GitHub release is tagged `v0.13.0`; the **container tag is `0.13.0`**. Upstream's `.github/workflows/docker.yml` feeds `docker/metadata-action` a `type=raw,value=${version}${suffix}` where `version` comes from `pyproject.toml` — so the registry holds `0.13.0`, `0.13`, `latest` (and `-slim` / `-alpine` suffixed siblings), but never `v0.13.0`. Pulling `v0.13.0` fails with a manifest 404.

Do not pin `latest`. A cached `latest` on this box has been stale before.

## Network isolation

`open-terminal` is deliberately **not** on `ai_shared`. It runs code a model wrote, on behalf of whoever is chatting — the same trust posture as the sandbox subsystem. From `ai_shared` that shell could reach:

- `roofix-db`, `sandbox-db`, `n8n-db`, `hive-metastore-db`, `superset-db` — several on dev-default credentials
- `minio` on its dev-default root credentials, i.e. the whole Iceberg warehouse
- `litellm` — every model and every virtual key's surface
- `n8n` — the encrypted credential store for every automation
- `sandbox-runner` — which mounts `/var/run/docker.sock`, i.e. root on the host
- `phoenix-mcp`, `interceptor`, and their logged-in browser profiles

So it gets its own bridge, `terminal_net`, joined by exactly two containers:

```
Browser ─▶ cloudflared ─▶ oauth2-proxy ─▶ openwebui ─┬─ ai_shared ────▶ litellm, searxng, kokoro, …
                                                     └─ terminal_net ─▶ open-terminal:8000
```

Three properties fall out of that:

1. **No host port.** Every request is proxied by the openwebui *backend* (`backend/open_webui/routers/terminals.py`), which attaches `Authorization: Bearer <key>` and `X-User-Id: <open webui user id>` server-side. The browser never sees the URL or the key, and nothing on the LAN can reach the shell.
2. **`terminal_net` is a plain bridge, NOT `internal: true`** — unlike `sandbox_net`. The whole point of the fat image is runtime `pip install` / `apt-get install`, which needs a default gateway. Egress is narrowed by the in-container allowlist instead (below).
3. **Never mount `docker.sock`.** The upstream image ships the Docker CLI and README documents mounting the socket. Doing so here would hand a model root on the host and make every other control on this page decorative.

`terminal_net` is created by `make network` alongside `ai_shared`.

## Egress allowlist

`OPEN_TERMINAL_ALLOWED_DOMAINS` is enforced inside the container by the image's `entrypoint.sh`: a local `dnsmasq` NXDOMAINs everything not listed, each resolved IP is added to an `ipset`, and `iptables OUTPUT` drops anything not in that set. Loopback and `ESTABLISHED,RELATED` are accepted first, so the healthcheck and replies to openwebui keep working. `CAP_NET_ADMIN` is then permanently dropped with `capsh` before the server starts.

Default allowlist:

```
pypi.org, files.pythonhosted.org,
github.com, *.github.com, *.githubusercontent.com,
registry.npmjs.org,
deb.debian.org, security.debian.org,
huggingface.co, *.huggingface.co
```

A leading `*.` matches the parent domain and all subdomains (the entrypoint strips the wildcard and lets dnsmasq's suffix matching do the work).

The variable is **three-way, and empty is not "off"**:

| Value | Behaviour |
|---|---|
| unset / commented out | No firewall at all — full internet |
| `""` (empty string) | Block **all** outbound |
| `a,b,*.c` | Allowlist |

Compose always passes the variable, so blanking it in `.env` airgaps the terminal rather than disabling the firewall.

To add a package source: append the domain in `.env`, then `make up open-terminal` (a **recreate** — the rules are built at container start, not reloaded). Two limits worth knowing: because the filter is DNS-based, anything reached by bare IP is unreachable regardless of the list, and a CDN that hands out fresh IPs only works for names dnsmasq has actually resolved.

`cap_add: [NET_ADMIN]` in the compose file exists solely for this. Do **not** add `security_opt: [no-new-privileges:true]` — the image runs as the unprivileged `user` account and needs passwordless `sudo` for the firewall, for runtime package installs, and for multi-user account provisioning.

## Resource limits

Plain `docker compose` silently ignores `deploy.resources.limits` (a Swarm key), so the compose file uses the Compose-v2 service keys that actually apply:

| `.env` | Compose key | Default |
|---|---|---|
| `OPEN_TERMINAL_MEM_LIMIT` | `mem_limit` | `4g` |
| `OPEN_TERMINAL_CPUS` | `cpus` | `2` |
| `OPEN_TERMINAL_PIDS_LIMIT` | `pids_limit` | `512` |

This box also runs vLLM and llama.cpp; a runaway model-issued command with no ceiling starves them. `pids_limit` is the fork-bomb guard.

## First-time bring-up

Run these on the box, from the repo root.

1. **Create the network** (idempotent; also creates `ai_shared`):

   ```bash
   make network
   ```

2. **Set the API key.** `OPEN_TERMINAL_API_KEY` in `.env` is already populated; regenerate it if you want your own:

   ```bash
   openssl rand -hex 32
   ```

   The same variable feeds both containers — `open-terminal` verifies it, and `openwebui`'s `TERMINAL_SERVER_CONNECTIONS` interpolates it — so they cannot drift. Compose fails fast via `${OPEN_TERMINAL_API_KEY:?…}` if it is empty.

3. **Start the terminal.** First pull is ~4 GB:

   ```bash
   make up open-terminal
   docker logs -f open-terminal
   ```

   Wait for the firewall lines and the uvicorn bind:

   ```
   Egress: DNS whitelist — pypi.org,files.pythonhosted.org,…
     ✓ pypi.org (+ subdomains)
     …
   dnsmasq started (upstream: 127.0.0.11)
   Egress firewall active — dropping CAP_NET_ADMIN permanently
   INFO:     Uvicorn running on http://0.0.0.0:8000
   ```

4. **Recreate Open WebUI.** Its network list changed (it now joins `terminal_net`), so this must be a recreate, not a restart:

   ```bash
   make up openwebui
   ```

5. **Wire the connection in the Admin UI.** `TERMINAL_SERVER_CONNECTIONS` is a PersistentConfig value (`terminal_server.connections`) read only on the **first** boot of a fresh `openwebui_data` volume — same caveat as `ENABLE_WEB_SEARCH` and the `AUDIO_TTS_*` block. This install already has a volume, so do it by hand:

   **Admin Settings → Integrations → Open Terminal → Add Connection**

   | Field | Value |
   |---|---|
   | URL | `http://open-terminal:8000` |
   | API Key | the value of `OPEN_TERMINAL_API_KEY` |
   | Name | `Zeo Terminal` |
   | Path | `/openapi.json` (default) |
   | Auth | Bearer (default) |
   | Enabled | on |
   | Chat Uploads | **Filesystem** |
   | Access Control | see below |

   Hit **Verify** — Open WebUI probes the URL and should report a plain `terminal` (not `orchestrator`). Save.

6. **Turn off the Code Interpreter.** `ENABLE_CODE_INTERPRETER=false` is also first-boot-only, so flip it at **Admin Settings → Code Execution → Code Interpreter**.

7. **Check the models.** Terminal tools are injected as native function-calling tools. A model configured with **Function Calling: Legacy** in *Workspace → Models → (model) → Advanced Params* receives **none of them** — the terminal pill appears and does nothing. Set those models to **Native**. The per-model `terminal` capability flag defaults to on; it lives in the same model editor if you want to hide the terminal from a specific model.

### Access control

Open WebUI stores per-connection access on `config.access_grants` (a list), not the older `access_control` dict. `utils/access_control.py::has_connection_access` treats a **missing or empty** list as *private — admin only*, so an entry with no grants is invisible to regular users.

The compose default grants public read:

```json
"access_grants": [{"principal_type": "user", "principal_id": "*", "permission": "read"}]
```

`principal_id: "*"` with `principal_type: "user"` is the public-read form recognised by `has_access`. To restrict to a group instead, use the Access Control dialog in the Add/Edit Terminal Connection modal, which writes:

```json
"access_grants": [{"principal_type": "group", "principal_id": "<group id>", "permission": "read"}]
```

Admins always keep access when `BYPASS_ADMIN_ACCESS_CONTROL` is on (upstream default).

## Verifying the connection

`open-terminal` has no host port, so test from inside `openwebui` — the only container that can reach it:

```bash
# is it reachable and does the key work?
docker exec openwebui curl -fsS \
  -H "Authorization: Bearer $OPEN_TERMINAL_API_KEY" \
  http://open-terminal:8000/openapi.json | head -c 200

# unauthenticated liveness
docker exec openwebui curl -fsS http://open-terminal:8000/health
# {"status":"ok"}

# what the model is told about this environment
docker exec openwebui curl -fsS \
  -H "Authorization: Bearer $OPEN_TERMINAL_API_KEY" \
  http://open-terminal:8000/system
```

Confirm the firewall is on and that it actually blocks:

```bash
docker logs open-terminal 2>&1 | grep -E "Egress|dnsmasq"

docker exec open-terminal curl -sS -m 5 https://pypi.org/simple/ -o /dev/null -w '%{http_code}\n'   # 200
docker exec open-terminal curl -sS -m 5 https://example.com -o /dev/null -w '%{http_code}\n'        # DNS failure
```

Confirm the isolation holds — these must all fail:

```bash
docker exec open-terminal getent hosts litellm        # no output
docker exec open-terminal curl -sS -m 5 http://litellm:4000/health   # could not resolve
docker exec open-terminal ls /var/run/docker.sock     # No such file or directory
```

And that multi-user provisioning works — open a chat as two different people, run `whoami; pwd` in each, and you should see two different accounts under `/home`:

```bash
docker exec open-terminal ls -la /home
```

## Security model

Read this before widening access.

- **One shared container.** `OPEN_TERMINAL_MULTI_USER=true` gives each Open WebUI user their own Linux account and home directory, and commands run under `sudo -u`. Upstream is explicit that this is a **workspace separation, not a security boundary**: one kernel, one process list, one network stack, one `/tmp`. Users can watch each other's processes, reach ports each other open, and exhaust CPU / memory / disk for everyone. Account provisioning needs elevated privileges inside the container, so a user who sets out to reach root inside it will get there.
- **Everyone with chat access must be trusted at the same level.** Access to Open WebUI is already gated by oauth2-proxy on the `zeoai.access@zeoenergy.com` Google group; the terminal inherits that gate, plus whatever `access_grants` narrows it to. Treat terminal access as "shell on a shared dev box", because that is what it is.
- **Real per-user isolation needs Enterprise.** Open WebUI's [Terminals](https://docs.openwebui.com/features/open-terminal/terminals/) orchestrator provisions a separate container per user with its own files, processes, resource limits and network boundary. It requires an Open WebUI Enterprise licence for production. If we ever need to protect users from each other, that is the upgrade path — it plugs into the same connection UI, with `server_type: "orchestrator"` and a `policy_id`.
- **The API key is a root-capable credential.** Anyone holding it gets a shell. It never leaves the box today: the openwebui backend proxies every request. Do not publish a host port, and do not configure the terminal as a per-user *Direct Connection* (User Settings → Integrations), which sends the key to the browser.
- **Never mount `docker.sock`.** Ever. See [Network isolation](#network-isolation).
- **Egress is allowlisted, resource use is capped** — see the two sections above.

## Relationship to the sandbox subsystem

Both run untrusted model-authored code; they solve different problems and coexist.

| | [`ai/sandbox/`](../sandbox/SANDBOX.md) | `ai/open-terminal/` |
|---|---|---|
| Unit | One **ephemeral container per session**, spawned on demand | **One persistent container**, one home dir per user |
| Lifetime | Idle + hard TTL, reaped by `ai/sandbox/runner/reaper.py` | `restart: unless-stopped`; files persist indefinitely |
| Invoked via | LiteLLM **MCP tools** (`sandbox.run`, `create`, `write_files`, `preview`) | Open WebUI's **native terminal integration** — function-calling tools plus first-class UI |
| Output | A URL Open WebUI iframes — a running web app | Command output, files, previews, an interactive shell |
| UI | Iframe artifact panel | File-browser sidebar, file previews, chat-upload target, xterm pane |
| Egress control | `sandbox-egress` (tinyproxy, `FilterDefaultDeny`) on `sandbox_net` | dnsmasq + iptables **inside** the container |
| Networks | `sandbox_net` + `sandbox_state` (both `internal: true`) + `sandbox_egress_out` | `terminal_net` (plain bridge) |
| Privileged component | `sandbox-runner` holds `docker.sock` | none |

Rules of thumb: **sandbox** when the deliverable is a running web app to look at, or when each attempt must start from a clean slate. **Open Terminal** when the user wants a workspace — analyse this spreadsheet, keep the script around, `pip install` a library, come back tomorrow.

They are not interchangeable at the wiring level either: Open WebUI's terminal integration speaks Open Terminal's specific API surface (`/openapi.json`, `/system`, `/files/*`, `/execute`, `/api/terminals` + its WebSocket), so you cannot point `TERMINAL_SERVER_CONNECTIONS` at `sandbox-runner`.

## Upgrades

```bash
# 1. edit OPEN_TERMINAL_VERSION in .env (remember: no leading "v")
# 2. pull + recreate
make build open-terminal
make up open-terminal
```

The `open_terminal_home` volume is untouched by an image bump — user files survive. Check the [release notes](https://github.com/open-webui/open-terminal/releases) for new `OPEN_TERMINAL_*` variables; add anything you adopt to `.env`, `.env.example`, the compose file, and the `CLAUDE.md` table together.

Open WebUI itself is pinned separately by `OPENWEBUI_VERSION` and builds a patched local image — see [OPENWEBUI.md § Custom image](../openwebui/OPENWEBUI.md). Terminal support landed upstream before v0.11.3, so a terminal bump never requires an Open WebUI bump.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Container exits at boot with `unable to raise CAP_SETPCAP for BSET changes` | The entrypoint's `capsh --drop=cap_net_admin` needs `CAP_SETPCAP`, normally part of Docker's default capability set. A hardened daemon or seccomp profile trimmed it — add `SETPCAP` next to `NET_ADMIN` under `cap_add:`. As a last resort, unset `OPEN_TERMINAL_ALLOWED_DOMAINS` to skip the firewall entirely (and lose egress control). |
| `WARNING: iptables not found — skipping egress firewall` in the logs | You are on the `slim` / `alpine` / `openshift` variant. Use the default tag. |
| Container exits at import with `ValueError: could not convert string to float: ''` | Something passed `OPEN_TERMINAL_EXECUTE_TIMEOUT` as an empty string. `open_terminal/env.py` calls `float()` whenever the variable is *present*, so blank ≠ unset. The compose file guards this with `${OPEN_TERMINAL_EXECUTE_TIMEOUT:-0}` — don't remove the `:-0`. `0` is falsy at the only place it is read, so it behaves exactly like unset. |
| Container exits immediately with `variable OPEN_TERMINAL_API_KEY must be set` | `${OPEN_TERMINAL_API_KEY:?…}` fired. Populate it in `.env` — `openssl rand -hex 32`. |
| Admin UI **Verify** says "Failed to connect to the terminal server" | Usually the network. Confirm both containers are on `terminal_net` (`docker inspect -f '{{json .NetworkSettings.Networks}}' openwebui open-terminal`); if openwebui isn't, it was restarted rather than recreated — `make up openwebui`. Otherwise the key is wrong, or open-terminal is still installing `OPEN_TERMINAL_PIP_PACKAGES` and hasn't bound yet. |
| Chat shows **503 / "Terminal unavailable"** | Open WebUI fetches `<url>/openapi.json` once at startup (`utils/tools.py::set_terminal_servers`). If the terminal was down then, the tool cache is empty until openwebui is restarted or the connection is re-saved in the Admin UI. Re-save the connection — it re-runs the fetch. |
| Terminal pill appears but the model never uses it | The model is on **Function Calling: Legacy**. Terminal tools are native-only. Switch it to **Native** in Workspace → Models → Advanced Params. |
| Terminal doesn't appear in the composer at all for non-admins | `config.access_grants` is empty, which means *private, admin-only*. Add a public or group grant in the connection's Access Control dialog. |
| Terminal appears but the Code Interpreter button vanished | Expected — they are mutually exclusive; selecting a terminal clears the Code Interpreter toggle. |
| `pip install` fails with a DNS error for a host you expect to work | Not in `OPEN_TERMINAL_ALLOWED_DOMAINS`, or reached by bare IP. Add the domain and `make up open-terminal` (recreate — rules are built at container start). |
| Users see each other's files | Expected in shared-workspace terms only if `OPEN_TERMINAL_MULTI_USER` is false. If it is true and homes still collide, the volume is mounted at `/home/user` instead of `/home`. |
| Everyone's files disappeared | Someone ran `down -v` and dropped `open_terminal_home`. There is no backup — add the volume to the box's backup set if the workspaces matter. |
| Container OOM-killed mid-build | `OPEN_TERMINAL_MEM_LIMIT` (default `4g`) is a hard cap shared by every user at once. Raise it, or push heavy builds into the sandbox subsystem. |

## Rollback

Fully additive; to back out:

1. Set `OPENWEBUI_ENABLE_CODE_INTERPRETER=true` in `.env` (and flip it back in Admin Settings → Code Execution, since it's a DB-shadowed value).
2. Delete the connection in Admin Settings → Integrations → Open Terminal.
3. Remove `- terminal` from the `openwebui` service's `networks:` list and recreate: `make up openwebui`.
4. Tear the terminal down: `docker compose -f ai/open-terminal/docker-compose.open-terminal.yml --env-file .env -p ai-open-terminal down -v` (the `-v` drops every user's home directory; omit it to keep them for a re-enable).
