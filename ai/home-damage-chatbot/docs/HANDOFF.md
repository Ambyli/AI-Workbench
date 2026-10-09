# Handoff: Service Page + Home Damage Chatbot

**Start here.** State as of 2026-10-06. This page tells you what exists, how to run it, what's verified, and what's left. Details live in the linked docs.

## 1. What this is

The new zeoenergy.com **Service page** (`/service`) with an embedded **AI-assisted service-request chatbot**:
- **Header:** both department phone numbers, email and hours.
- **The chatbot:**
  - collects a damage report (account check, issue type, urgency, branch questions, a map pin of the damage, photos)
  - confirms the request with the customer
  - emails staff a structured handoff
- **Fallback:** the classic form, used automatically when the assistant is unavailable.

| Repo | Branch | Holds | Pushed? |
|---|---|---|---|
| This repo (Home Damage Chatbot) | `feature/service-page-mock` | Backend (FastAPI), account service, mock scripts, tests, docs | **No** (local commits only) |
| `New-Zeo-Website/` (SunergySolar/New-Zeo-Website, nested folder, git-ignored here) | `service-chatbot` | The Service page and chat UI (React) | **No.** Nothing goes to SunergySolar until Stage 5 sign-off; merging to `main` deploys production via Cloud Build |

## 2. Architecture (one paragraph per piece)

- **Chatbot API** (`backend/main.py`, `state_machine.py`)
  - A deterministic state machine owns the conversation. All bot wording is fixed in code.
  - The AI model (`qwen3.8-solo` via the LiteLLM gateway, `backend/llm.py`) only does two narrow jobs, statelessly, one message at a time: pull a value from an answer, and judge whether it really answered the question.
  - The model never decides flow, routing or wording, and never sees account data.
  - A code-level filter removes prompt-injection sentences before any model call.
  - 911 safety and handoff detection are pure code, and run first.
- **Account service** (`backend/account_service.py`), the separation layer
  - A separate process: the only thing with CRM/database access and email/chat delivery.
  - The chatbot asks it "found or not?" through `backend/account_gateway.py` and gets one bit plus an opaque reference.
  - At submit, the account service appends the account details to the end of the staff handoff and delivers it.
  - The chatbot refuses to start if it can see CRM or mail secrets (`REQUIRE_ACCOUNT_GAP=true`).
- **Matching** (`backend/account_match.py`, `address_parse.py`, `trgm.py`, `backend/sql/`)
  - Fuzzy address search, then name search.
  - Gates: address score, exact house number, street-name similarity, name score, and no two homes too close to call.
  - CRM backends: `stub` (synthetic accounts in `backend/fixtures/mock_projects.json`), `phoenix` (direct read-only SQL, not provisioned), `mcp` / `inhouse` (placeholders).
- **Website** (`New-Zeo-Website/src/app/components/service-chat/`)
  - The chat UI follows API contract v2.1 (`docs/API_CONTRACT.md`).
  - Desktop shows an embedded panel; phones get a full-screen overlay.
  - Also: a status pill (online, checking, unavailable), the waiting room, and the classic-form fallback.
- **Escalation** (`docs/ESCALATION.md`)
  - The right phone number per issue (349-4057 for solar; 382-0075 otherwise, and when no issue has been chosen yet).
  - Frustration acknowledged once.
  - "[Incomplete]" alerts to the department when a customer asks for a person, stops responding for 20 minutes, or hits the turn limit.

## 3. Run it

```powershell
powershell -ExecutionPolicy Bypass -File scripts\mock-start.ps1          # http://localhost:8080
powershell -ExecutionPolicy Bypass -File scripts\mock-start.ps1 -Rebuild # after website changes
powershell -ExecutionPolicy Bypass -File scripts\mock-inbox.ps1          # what staff received
powershell -ExecutionPolicy Bypass -File scripts\mock-stop.ps1
```

- **What it starts (no Docker):** the account service (:8100), the chatbot API (:8000), and a site server (:8080) standing in for nginx.
- **Mock-safe overrides:** no email or chat is ever sent, the staff API is off, synthetic accounts only, 3 active chats.
- **Requires:**
  - `.env` with `VLLM_API_KEY` and `VLLM_MODEL=qwen3.8-solo` (see `.env.example`)
  - optionally `.env.account` (see `.env.account.example`)
  - the website dependencies installed
- **Accounts to type:** see [`MOCK_ACCOUNTS.md`](MOCK_ACCOUNTS.md).

## 4. Tests

| Command | What | Last result (2026-10-02) |
|---|---|---|
| `USE_MOCK_LLM=1 python tests/run_all_tests.py` | 16 offline suites (separation gap, SQL injection, injection filter, escalation, degraded gateway, ...) | 16/16 |
| `python tests/security_test.py` | Input and handoff security | Pass |
| `python tests/model_eval.py` | Real model, 67 cases | All gates pass, 3 runs |
| `python tests/e2e/real_flows.py` (mock running) | 17 flows + 18 account cases on the real model | 17/17, 18/18 |
| `python tests/e2e/security_retest.py` (mock running) | Live security re-test | 22/22 |
| `python tests/e2e/load_queue.py` (mock running) | 15 concurrent customers | Pass, p95 3.9 s |

Static checks:
- `bandit -r backend mock --severity-level medium`: 0 findings
- `pip-audit -r requirements.txt`: clean
- Website `npx tsc --noEmit --ignoreDeprecations 6.0`: 23 pre-existing errors, 0 in feature files. The repo's documented `tsc` command checks nothing because of TS5101.

UI and accessibility were checked with headless Chrome scripts (desktop, 375px and 390px, axe-core). Those scripts lived in the session scratchpad, so **porting them to `tests/e2e/` with Playwright** is a recommended next task.

## 5. What's left before release (not code)

Full lists: [`DELIVERY_PLAN.md`](DELIVERY_PLAN.md) (findings, evidence log), [`RELEASE_TEST_PLAN.md`](RELEASE_TEST_PLAN.md) (go/no-go checklist).

1. **CRM access.** Preferred: use the read-only **Albatross API** (`POST /v1/match/projects`) through a new account-service backend, with a key scoped to matching only. We still need a sample match response, or its docs, to map candidates into our gates. Fallback: a read-only phoenix database role (`CRM_BACKEND=phoenix`). After access, calibrate thresholds on real projects with **0 wrong matches** (A-02/A-03).
2. **Legal:** approve [`LEGAL_COPY_DRAFT.md`](LEGAL_COPY_DRAFT.md) (cookie banner, privacy page, Tawk.to removal), then apply it on the website branch.
3. **Stakeholder re-sign-off** on changed copy (C-07): account name asked first; solar sent to 349-4057 instead of 375-9375; department numbers in handoff, submitted, queue and turn-limit messages.
4. **Business decisions** ([`ESCALATION.md`](ESCALATION.md) §"What the business still needs"):
   - who works "[Incomplete]" alerts, and how fast
   - real department inboxes (`MAILBOX_*`)
   - after-hours coverage
   - the follow-up promise ("one business day" on the page vs "1–2 business days" in the chatbot)
   - Cloudflare Turnstile (API6)
   - the production geocoder (S-M6)
   - accepting found/not-found disclosure (A-01)
5. **Rotate the model key** (C-05): it was pasted in chat.
6. **Production packaging (5.10):**
   - Docker plus a real nginx config: the 4-route allow-list, `X-Forwarded-For` overwrite, body cap, proxy timeouts ≥ 35 s, security headers passed through, HSTS on TLS
   - deploy the chatbot and the account service as separate processes with separate env files
7. **Real phones:** iPhone Safari and Android Chrome spot checks.
8. **Out of lane, report to the site owners:** no security headers on the site's nginx (W-07); "Get a Quote" contrast (W-08); 23 pre-existing TypeScript errors; `react-router` advisories (W-01).

## 6. Gotchas

- `.env` loads unless the process environment already sets a key, and **inline comments are not stripped** (put comments on their own line).
- The model id depends on the gateway key's team: `GET /v1/models` lists what the key can see, but only a real completion proves access (`python mock/check_model.py`).
- Only 3 chats can be active. The page heartbeats every 45 s and idle slots free after 150 s. A test script that opens chats and quits holds slots until then.
- `scripts/mock-start.ps1` hangs if you pipe its output in some shells (child processes keep the handle open). Run it plainly.
- Never put real customer data in fixtures, docs or tests. The Postman export (`*.postman_collection*.json`) is git-ignored because its example contains real data.
- The website repo has no git identity configured; commits there used `-c user.name=... -c user.email=...`.
- The mock never emails. `EMAIL_SEND_ENABLED` and `CHAT_SEND_ENABLED` are forced false, and the classic form posts to `/mock-form`.

## 7. Suggested next steps for whoever picks this up

1. Get an Albatross key (match scope) and one sample `/v1/match/projects` response. Add `CRM_BACKEND=albatross` to the account service, then run `account_match_test`, `mock_accounts_test` and `real_flows`.
2. Port the UI scripts to Playwright under `tests/e2e/`. That also covers Firefox and WebKit.
3. Install Docker. Write `docker-compose.mock.yml` with three services plus nginx, then re-run `security_retest.py` against nginx.
4. Apply the legal copy once approved, and update the stakeholder sign-off document.
5. Optional libraries: `phonenumbers` (contact validation), `usaddress` (address parsing), `nicknames` (Bob → Robert matching), `hypothesis` (fuzz the parser and filter), `prometheus-client` (handoff, alert and outage metrics).
