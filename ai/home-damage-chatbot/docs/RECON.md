# RECON.md — Recon, Baseline & Scope (Build Tree Framework, Phase R)

> Read-only recon completed before any mutation. See [GOAL.md](GOAL.md) for purpose.

## 1. Baseline green state (the safety datum)

Established 2026-07-06 on a local macOS build host (Apple M4), branch `main`
@ `d192516`, venv Python **3.9.6**.

| Suite | Command | Result |
|-------|---------|--------|
| Smoke / end-to-end | `USE_MOCK_LLM=1 venv/bin/python smoke_test.py` | **PASS** — "ALL CHECKS PASSED" |
| Security | `USE_MOCK_LLM=1 venv/bin/python security_test.py` | **PASS** — "ALL SECURITY CHECKS PASSED" |
| Pipeline/hardening | `USE_MOCK_LLM=1 venv/bin/python pipeline_test.py` | **PASS** — "ALL PIPELINE/HARDENING CHECKS PASSED" |

Tests are **plain scripts** (not pytest) using FastAPI `TestClient` in-process —
no live server, no Ollama, no network. App imports clean (`12 routes`).

**No linter / type-checker / formatter is configured** (no ruff, mypy, black,
pyproject.toml). "Baseline green" here = the 3 suites pass + app imports. Any
tooling I add in Stage 1 becomes part of the baseline going forward.

### Baseline anomalies observed (candidate findings, not yet acted on)
- **A1 — EXIF strip silently falls back.** During `security_test.py` upload tests:
  `WARNING:chatbot.upload:PIL metadata strip failed ... falling back to direct
  write`. The privacy invariant (strip EXIF/GPS) may not actually execute for the
  test image; the "secure filename" path still holds. → Stage 1/2 investigate.
- **A2 — Model config drift.** `OLLAMA_MODEL` defaults to `qwen3:14b`; the machine
  actually has `qwen3.5:9b` / `qwen3.5:4b`. Real path would 404 the model. → Stage 1.
- **A3 — Python version drift.** Docs say 3.10+, venv is 3.9.6; runs fine → doc fix.
- **A4 — README staleness.** README still contains a stale Windows absolute path
  and a `file:///C:/Users/...` link, and a duplicated list numbering ("6." twice)
  in the Security section. Cosmetic/doc → Stage 1.

## 2. Scope (blast radius)

This is a **full Build Tree run over the whole app** (user opted into all three
passes), so the in-scope set is the entire `backend/` + `frontend/` + test
suite. This is one of the rare genuinely repo-wide efforts. Nothing in the
current tree is out of scope *by distance*; scope is instead bounded by the
NO_TOUCH set and the Sequencing Rule (stabilize → optimize → extend).

In-scope surfaces (mapped in the tree): all 16 backend modules, the 8 API
routes, the 3-tab vanilla-JS frontend, the Jinja2 email template, mock data, and
the 3 test scripts.

## 3. NO_TOUCH surfaces (🔒 — must not change without explicit approval)

| ID | Surface | Why off-limits |
|----|---------|----------------|
| NT1 | The **LLM-has-no-authority thesis** (deterministic flow/safety/auth boundary in `state_machine.py`, `safety.py`, `pipeline.py`, `crm.py`, `lookup.py`) | Load-bearing design thesis (GOAL.md). No change may give the model flow/auth/copy authority. |
| NT2 | `safety.is_safety_concern` **determinism + always-on** behavior | Safety invariant #1. May extend keyword coverage, never make it LLM-gated or skippable. |
| NT3 | API request/response **shapes** consumed by the frontend (`ChatRequest`/`ChatResponse`/`LookupResult`/`EmailRecord` field names) | The frontend↔backend seam. Additive-only; renames break `app.js`. |
| NT4 | The **least-privilege lookup contract** (name alone rejected; 2nd factor required; threshold 0.9; phone withheld from `/api/customers`) | Anti-enumeration / privacy invariant. |
| NT5 | The **scaffolded seams** (`crm` mcp/inhouse, Gmail send) raising `NotImplementedError` | Deliberate placeholders awaiting external inputs (CRM docs, GW creds). Track as 🧩 STUBBED, do not "finish" without those inputs. |
| NT6 | Green-without-network property (mock LLM + in-memory inbox + stub CRM defaults) | The demo must run with no creds/model. Don't introduce a hard dep on network/creds in a default path. |

## 4. Non-goals (this effort will NOT do)

- Will **not** wire live CRM `mcp`/`inhouse` backends or live Gmail send (blocked
  on external inputs — see NT5). Improve the seams' safety/testability only.
- Will **not** migrate to a persistent/encrypted DB, add server-issued auth
  tokens, or swap Ollama→vLLM (explicitly future-scope per README).
- Will **not** rewrite the vanilla-JS frontend into a framework (no build step is
  a deliberate property). Frontend work stays within vanilla JS/CSS/HTML.
- Will **not** change the LLM's role or the deterministic boundary (NT1).

## 5. Characterization-coverage gaps (write tests before modifying)

The 3 suites cover a lot, but these behaviors are exercised only indirectly or
not at all and should get characterization tests before Stage 1 touches them:

- **G1** — `email_render._tier()` urgency→(label,color) mapping incl. electrical
  floor (asserted only loosely). Pin the full mapping before any refactor.
- **G2** — `validation.validate_answer` branch matrix (name/address/contact/
  verbatim format checks, retry accounting). Only the unconfirmed-flag path is
  directly asserted.
- **G3** — `state_machine` branch-step coverage for **solar** and **misc** issue
  types end-to-end (smoke covers roof + electrical only).
- **G4** — `upload.validate_and_save_upload` **EXIF-strip success** path (A1 shows
  only the fallback path runs in tests → the strip itself is uncharacterized).
- **G5** — `safety.is_safety_concern` keyword matrix (which phrases do/don't fire)
  — pin before extending coverage (NT2 lets us extend, not weaken).

## 6. Environment notes for reproducibility

- Run tests with `USE_MOCK_LLM=1` and the repo venv: `venv/bin/python <file>.py`.
- `pydantic-settings` and `pytest` are **not** installed; `config.py` uses its
  built-in `.env` fallback. Adding pytest is a Stage-1 option (keep the plain
  scripts working regardless).
- `backend/data/emails.json` is a mutable seed/store; tests clear+reseed it.
  Treat as data, not code.
