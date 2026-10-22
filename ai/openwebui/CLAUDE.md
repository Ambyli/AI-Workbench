# ai/openwebui

Open WebUI, the chat UI at `chat.zeoenergy.com` (the catch-all upstream of oauth2-proxy; one Google sign-in via trusted-header SSO). It runs a **locally built, patched** image `openwebui-zeo:<OPENWEBUI_VERSION>`. Guide: [`OPENWEBUI.md`](OPENWEBUI.md). Compose: `docker-compose.openwebui.yml` (a pure `${…}` template — edit `.env`, not the YAML). Tests: `unit-tests/openwebui` (`uv run --no-sync pytest unit-tests/openwebui -q`).

## Rules

- **Recreate-class** (SQLite `webui.db`, boot migrations, in-process websockets): `make up openwebui` has downtime. Fixed name `openwebui`, so `docker exec openwebui …` works. A network-list change (`terminal_net`) needs that recreate, not a restart. No GPU; start LiteLLM first or the model list is empty.
- **Patches must apply, or the build fails — by design.** `Dockerfile.openwebui` dry-runs every `patches/*.patch`, applies it, `compile`s the patched modules and greps for each feature. A rejected hunk means: regenerate per `OPENWEBUI.md § Regenerating a patch for a new upstream version` and re-check the upstream facts listed there for that patch. Never force, fuzz or skip one. Pin a release tag, never `main`. Snapshot `openwebui_data` before a bump with migrations (`§ Updating the image`).
  - `0002` trusted-header SSO: verifies oauth2-proxy's asserted identity with Google using the forwarded access token (`WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER`), name + picture from Google.
  - `0003` a model's default OAuth MCP tools (Phoenix) stay in that user's `/api/models` `toolIds` only while the MCP server accepts the user's token (live `initialize` probe, cached 60 s) — no redirect on a new chat; plus single-flight token refresh in `OAuthClientManager.get_oauth_token`.
  - `0004` auto-continue: a native tool loop ended by an *announced* action gets a hidden nudge (`utils/zeo_auto_continue.py`).
  - `0005` `X-OpenWebUI-Message-Id` on chat completions to LiteLLM (`utils/headers.py`). Regenerate it with `diff -U4`, so the `ENABLE_FORWARD_USER_INFO_HEADERS` guard is inside the hunk.
  - `unit-tests/openwebui/` loads `0004`'s module and `0005`'s block **out of the patch files** — run it after any patch edit. A new patch: next number, Dockerfile header + `com.zeoenergy.openwebui.patches` label + compile list + a grep guard (`§ Adding another patch`).
- **Most env is first-boot-only.** Connections, web search, `AUDIO_TTS_*`, `TERMINAL_SERVER_CONNECTIONS`, `ENABLE_CODE_INTERPRETER`, context compaction and the Task Model are PersistentConfig rows in `webui.db` after the first boot of `openwebui_data`: set them in Admin Settings. Even plain-env keys can be shadowed (the 0.11 migration kept `ui.*` rows; an admin save writes `ui.enable_login_form`): run `select key from config order by key` before promising an env change applies (`§ Break-glass if Google itself is unreachable`). Read on every boot: `WEBUI_NAME`, `AIOHTTP_CLIENT_*`, `OAUTH_*`, `WEBUI_AUTH_TRUSTED_*`, `AUTO_CONTINUE_MAX_NUDGES`, `ENABLE_FORWARD_USER_INFO_HEADERS`.
- **MCP OAuth blobs have their own keys.** `OPENWEBUI_OAUTH_CLIENT_INFO_ENCRYPTION_KEY` / `_SESSION_TOKEN_ENCRYPTION_KEY` are set independently of `OPENWEBUI_SECRET_KEY` (compose falls back to it when unset). Changing a key that encrypted them makes MCP tools 404 (`InvalidToken`); **Save does not fix it** — re-register the client (`§ Encryption keys`). Rotating `OPENWEBUI_SECRET_KEY` alone only logs everyone out.
- **Cloudflare edge-caches `/static/*` for ~4 h regardless of auth** — check `cf-cache-status` before blaming a mount or the image (`§ Cloudflare caches /static/*`).
- **Forwarded user headers are not scoped to LiteLLM:** the same flag sends them to `kokoro-api`, every MCP / OpenAPI tool server (Phoenix over the internet, which also gets the chat / message ids) and the terminal. They are labels, not proof of identity. The LiteLLM-only alternative is per-connection header templates in the `openai.api_configs` row (`§ Forwarded user and session headers`).

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Open WebUI feeds routing** — `OPENWEBUI_ENABLE_FORWARD_USER_INFO_HEADERS=true` + patch `0005` give LiteLLM's route-policy hook the chat / message ids its turn pin keys on (off ⇒ no ids, no pin); `0004`'s hidden nudge arrives as an extra user message under the same message id; LiteLLM's footer and route-policy's `background_task` detect tasks by Open WebUI's default task prompts (`### Task:`, the emoji prompt, `Available Tools:`) — re-check after an upgrade (`§ Task model`). (root `CLAUDE.md` § Cross-folder rules)
- **`local-private` is where every failure ends** — set Admin → Interface → Task Model (local and external) to a non-`auto` model, `local-private` if chats may carry customer data (`§ Task model`). (root `CLAUDE.md` § Cross-folder rules)
- **Timeout ladder** — `OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT` / `_STREAM_IDLE_TIMEOUT` (900) bound every chat completion (and `search_web`, `fetch_url`, MCP calls) and must stay above the chains' per-read 240 / 300; `unit-tests/litellm/test_timeouts.py`. (root `CLAUDE.md` § Cross-folder rules)
- **Open Terminal ↔ Open WebUI** — `TERMINAL_SERVER_CONNECTIONS` is built inline in this compose file from `OPEN_TERMINAL_API_KEY`; `openwebui` is the only other member of `terminal_net`; terminal tools reach only models on **Native** function calling. Details: `ai/open-terminal/CLAUDE.md`. (root `CLAUDE.md` § Cross-folder rules)
- **Paths under `chat.zeoenergy.com`** — Open WebUI gets every path no other `OAUTH2_PROXY_UPSTREAMS` prefix claims. (root `CLAUDE.md` § Cross-folder rules)

## Env vars

One line each; full detail in `.env.example` and `OPENWEBUI.md`.

- `OPENWEBUI_VERSION` (`v0.12.3`) — upstream tag: the Dockerfile's `FROM` and the local image name; bump = `make build openwebui && make up openwebui` (`§ Updating the image`).
- `OPENWEBUI_AUTO_CONTINUE_MAX_NUDGES` (compose `2`; `.env.example` ships `0`, off until the heuristic fix is verified) — patch `0004` nudges per turn, `0` = off; each nudge is one extra model call and one `auto-continue:` INFO line (`§ Auto-continue announced tool calls`).
- `OPENWEBUI_ENABLE_FORWARD_USER_INFO_HEADERS` (`.env.example` `true`; compose fallback `false`) — only the literal `true`; see the rules above (`§ Forwarded user and session headers`).
- `OPENWEBUI_ENABLE_CODE_INTERPRETER` (`false`) — legacy Pyodide interpreter off (Open Terminal replaces it); first-boot-only (`code_interpreter.enable`), flip at Admin → Code Execution; leave `ENABLE_CODE_EXECUTION` (the code-block Run button) alone.
- `OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT` / `_STREAM_IDLE_TIMEOUT` (`900` / `900`) — see **Timeout ladder**; read on every boot.
- `OPENWEBUI_OAUTH_CLIENT_INFO_ENCRYPTION_KEY` / `OPENWEBUI_OAUTH_SESSION_TOKEN_ENCRYPTION_KEY` — set once, keep stable (rules above).
- `OPEN_TERMINAL_API_KEY` — consumed here too; listed in `ai/open-terminal/CLAUDE.md`.
