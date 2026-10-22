# ai/open-terminal

Open Terminal: Open WebUI's code-execution backend (it replaces the Pyodide code interpreter) — one fat Linux container with a persistent per-user `/home`, exposed as native function-calling tools plus a file sidebar, upload target and xterm pane. It runs a **locally built** `open-terminal-zeo:<tag>` (upstream + `patch_entrypoint.py`). Guide: [`OPEN_TERMINAL.md`](OPEN_TERMINAL.md). Compose: `docker-compose.open-terminal.yml`. API: `open-terminal.postman_collection.json`.

## Rules

- **A root-capable shell a model drives.** Never put it on `ai_shared` (it could reach `roofix-db` / `sandbox-db` / `n8n-db` / `lake-s3` on dev-default credentials, `litellm`, n8n's credential store, and `sandbox-runner`, which holds `docker.sock`). Never mount `docker.sock`, although the upstream README documents it.
- **`terminal_net` is a plain bridge, not `internal: true`** — the fat image's point is runtime `pip` / `apt`; egress is narrowed inside the container by `OPEN_TERMINAL_ALLOWED_DOMAINS`, which needs `cap_add: [NET_ADMIN]`. Do **not** add `security_opt: [no-new-privileges:true]`: the unprivileged image user needs passwordless sudo for the firewall, package installs and account provisioning.
- **No host port, on purpose.** The Open WebUI *backend* proxies every request and attaches `Authorization: Bearer` + `X-User-Id` server-side, so the key never reaches a browser. Debug with `docker exec openwebui curl …` (the Postman collection documents a temporary loopback publish and `socat`); never bind `0.0.0.0`, and never configure it as a per-user Direct Connection (sends the key to the browser).
- **Multi-user mode is a workspace separation, not a security boundary** (one kernel, process list, network stack, `/tmp`); everyone with chat access must be trusted equally. Real isolation is Open WebUI Enterprise "Terminals" (`server_type: "orchestrator"` + `policy_id`). `OPEN_TERMINAL_MULTI_USER=false` also means mounting the volume at `/home/user`.
- **Access is `config.access_grants`, not `access_control`.** A missing or empty list is *private, admin-only*. Public read: `{"principal_type": "user", "principal_id": "*", "permission": "read"}`; a group swaps in `principal_type: "group"` + its id.
- **Terminal tools are native function calling only** — a model on `function_calling: legacy` gets none (the pill shows and does nothing). Selecting a terminal force-clears the Code Interpreter toggle.
- **Custom image = unmerged upstream fix PR #118** for issue #119 (`capsh` as the unprivileged user dies with `unable to raise CAP_SETPCAP for BSET changes` whenever the allowlist is set; `SETPCAP` in `cap_add` does not help). `patch_entrypoint.py` rewrites it to `sudo -E capsh … --user=user` and fails the build if the script changed shape or already has the fix. When #118 ships: delete the Dockerfile + script and point the compose back at `ghcr.io/open-webui/open-terminal` — don't keep patching a fixed base. Never drop `--user=user` or set `user: root`.
- **Recreate-class** (per-user accounts, PTY sessions). An allowlist change is `make up open-terminal` (the firewall is built at start). `down -v` drops `open_terminal_home` — every user's files, no backup.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Open Terminal ↔ Open WebUI** — `OPEN_TERMINAL_API_KEY` is verified here and interpolated into the inline `TERMINAL_SERVER_CONNECTIONS` JSON in `ai/openwebui/docker-compose.openwebui.yml`; `terminal_net` (`make network`) joins only `open-terminal` and `openwebui`, and adding it to `openwebui` is a `make up openwebui` recreate. `TERMINAL_SERVER_CONNECTIONS` (`terminal_server.connections`) and `ENABLE_CODE_INTERPRETER` are first-boot-only: on this install set them at Admin → Integrations → Open Terminal and Admin → Code Execution. (root `CLAUDE.md` § Cross-folder rules)
- **`docker.sock` only in `sandbox-runner`** — never here. (root `CLAUDE.md` § Cross-folder rules)

## Env vars

One line each; full detail in `.env.example` and `OPEN_TERMINAL.md § Configuration` (+ the sections named).

- `OPEN_TERMINAL_VERSION` (`0.13.0`) — upstream tag, **no leading `v`** (`v0.13.0` 404s), never `latest`, must be the default fat variant (`slim` / `alpine` lack multi-user and runtime installs); bump = `make build open-terminal && make up open-terminal` (`OPEN_TERMINAL.md § Custom image (upstream firewall fix)`).
- `OPEN_TERMINAL_API_KEY` (unset — required, `${…:?}`) — root-capable bearer token shared with `openwebui`; unset would self-generate a key and silently 401. Rotate: `.env`, `make up open-terminal && make up openwebui`, and edit it in the Admin connection.
- `OPEN_TERMINAL_MULTI_USER` (`true`) — per-user Linux accounts keyed off `X-User-Id`; see rules.
- `OPEN_TERMINAL_ALLOWED_DOMAINS` (pypi, pythonhosted, github, githubusercontent, npm, debian, huggingface, hf.co) — DNS allowlist; **three-way: unset = no firewall, `""` = block all**, list = allowlist; compose always passes it; bare IPs never work (`OPEN_TERMINAL.md § Egress allowlist`).
- `OPEN_TERMINAL_MAX_SESSIONS` (`16`) — concurrent PTY sessions, all users; 429 past it.
- `OPEN_TERMINAL_EXECUTE_TIMEOUT` (empty) — inline wait before `/execute` returns a process id; compose reads `${…:-0}` **on purpose** (an empty value crashes `float()` at import). Keep the `:-0`.
- `OPEN_TERMINAL_MEM_LIMIT` / `_CPUS` / `_PIDS_LIMIT` (`4g` / `2` / `512`) — v2 keys `mem_limit` / `cpus` / `pids_limit`, because compose ignores `deploy.resources.limits`; `pids_limit` is the fork-bomb guard.
- `OPEN_TERMINAL_PIP_PACKAGES` (empty) — installed on **every** start before the server binds; bake heavy deps into a derived image.
- `OPEN_TERMINAL_INFO` (operator string) — appended to the `GET /system` prompt Open WebUI fetches once at startup; also creates `GET /info` (absent when empty). Costs tokens every turn.
- `OPENWEBUI_ENABLE_CODE_INTERPRETER` — read by the openwebui compose; listed in `ai/openwebui/CLAUDE.md`.
