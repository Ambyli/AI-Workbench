# ai/interceptor

Generic HTTP + MCP front-end for `common.cdp_interceptor`: opens a URL in headless Chrome under a named `--user-data-dir` profile, optionally drives the page (`page_script`, `actions`, screenshots, `login_actions`), and returns the JSON XHR/fetch bodies matching the given URL patterns. Used by `ai/roofix` (proposal pages) and the Enphase FEOC lookup (`enphase` profile, `INTERCEPTOR.md § Enphase` and its worked example). Guide: [`INTERCEPTOR.md`](INTERCEPTOR.md). Source: `app.py`, `profiles.py`, `logins.py`. Compose: `docker-compose.interceptor.yml`. Tests: `unit-tests/interceptor/`.

## Rules

- **No `ports:`** — port 8080 is reachable only on `ai_shared` (`http://interceptor:8080`), from the host through LiteLLM's `/v1/interceptor` pass-through, or `docker exec`. Something answering on host `localhost:8080` is a different, bare-metal interceptor with separate storage (`INTERCEPTOR.md § Reaching the service`).
- **Recreate-class**: Chrome profile directories and the debug-port pool can't be shared by two copies.
- **Login files hold no secrets.** `logins/<profile>.json` (checked in, bind-mounted read-only at `INTERCEPTOR_LOGINS_DIR`) maps names to `${ENV:INTERCEPTOR_LOGIN_…}` references only — a literal is refused, other prefixes can't be read. `allowed_origins` is mandatory: a `fill` only types into a tab on one of them. The credentials live in `.env` as `INTERCEPTOR_LOGIN_<PROFILE>_USERNAME` / `_PASSWORD` **and** must be listed in the compose `environment:` block (`${VAR:-}`); a literal `$` is `$$`. New profile login: file + `.env` / `.env.example` + compose env, then `make up interceptor` (`INTERCEPTOR.md § Login actions`).
- **The logins dir stays outside `INTERCEPTOR_PROFILES_ROOT`**: a profile refresh `rmtree`s its directory and everything under the root is listed as a profile.
- **`${…}` resolves only in a `fill` value inside `login_actions`**; anywhere else in `login_actions` it is a 400, in `actions` / `page_script` it is plain text. `login_actions` allow no `evaluate` and no `screenshot`, run once per capture and are never retried (a bad password must not lock the account). Never log or return a resolved value.
- **Profiles are uploaded, not logged into here**: capture on a laptop (`uv run cdp-spy`), upload a `.tar.gz` (not `.zip`) to `POST /profiles/{name}/refresh`. Re-capture on `login_wall: true` unless `login_actions` cover it.
- **Concurrency**: different profiles run in parallel; a second capture on the same profile runs on a temporary clone whose refreshed cookies are discarded; the port pool (`INTERCEPTOR_MAX_CONCURRENT`) caps everything, `429` past it.
- Endpoint changes update `interceptor.postman_collection.json` and `INTERCEPTOR.md` together (root `CLAUDE.md` § Postman collections).

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **`cdp_interceptor` traps** — this service runs `common.cdp_interceptor` (`shared/common/src/common/cdp_interceptor/`): `interceptor.js` is injected verbatim (never reformat) and read once per process, so editing it needs `make build interceptor && make up interceptor` (the image bakes `shared/common`); the library's own constraints are in `shared/common/CLAUDE.md`. (root `CLAUDE.md` § Cross-folder rules)
- **LiteLLM pass-throughs carry the auth** — `/v1/interceptor` → `interceptor:8080` in `ai/litellm/litellm_config.yaml` has `auth: true`; this service checks no token. (root `CLAUDE.md` § Cross-folder rules)
- **Smaller couplings** — `ai/roofix` calls `/capture` with the `roofix` profile (`INTERCEPTOR_URL`, `ROOFIX_PROFILE_NAME` live in the roofix stack); bring this service up first. (root `CLAUDE.md` § Cross-folder rules)

## Env vars

One line each; full detail in `.env.example` and `INTERCEPTOR.md § Configuration`.

- `INTERCEPTOR_PROFILES_ROOT` (`/data/profiles`) — root of the named profiles (`interceptor_data` volume); temp clones under `<root>/.temp/`.
- `INTERCEPTOR_MAX_CONCURRENT` (`8`) — simultaneous `/capture` calls = port-pool size; each slot ≈ one Chrome (200–400 MB) + an optional profile clone (`INTERCEPTOR.md § Resource sizing`).
- `INTERCEPTOR_LOGINS_DIR` (`/config/logins`) — per-profile login files, re-read every request; adding the mount is `make build interceptor && make up interceptor`.
- `INTERCEPTOR_LOGIN_<PROFILE>_USERNAME` / `_PASSWORD` (empty) — the credentials the login files reference; also in the compose `environment:`; unset → that profile's `login_actions` are a 400.
