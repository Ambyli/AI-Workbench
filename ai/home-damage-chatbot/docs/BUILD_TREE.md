# BUILD_TREE.md — Feature Decomposition (Build Tree Framework, Phase 2)

## Goal Statement (reproduced from GOAL.md — the lens for every node)

**Zeo Energy Local-LLM Service Chatbot.** A conversational replacement for a
static service-request form. **Deterministic code owns the flow and every
security/authority decision; the LLM is a narrow, stateless field-extractor with
no authority.** Success = Safety (deterministic 911, always-on) > Security /
injection-resistance > Trustworthy staff handoff (flag unconfirmed identity,
high-confidence match) > Forgiving homeowner UX > Runs with no creds/model. See
[GOAL.md](GOAL.md) for full statement, invariants, and tensions.

## Status legend
⬜ NOT_STARTED · 🧩 STUBBED · 🔨 IN_PROGRESS · ✅ IMPLEMENTED (no run-evidence yet)
· ✔️ VERIFIED (evidence) · 🚫 BLOCKED · 🔒 NO_TOUCH · 🖼️ = renders a visual artifact

Statuses below reflect the **as-is** codebase (existing-code recon). ✔️ = covered
by a suite I re-ran on 2026-07-06; ✅ = code complete but not directly asserted.

```
Zeo Energy Local-LLM Service Chatbot
│
├── T  TRUNK — app skeleton & shared contracts
│   ├── T.1  FastAPI app + static frontend mount ...................... ✅ main.py:27
│   ├── T.2  Shared Pydantic schemas (Mode/IssueType/ChatReq/Resp/…) .. 🔒 NT3 ✅ schemas.py
│   ├── T.3  Central config/secrets (env + .env fallback) ............. ✅ config.py:83
│   └── T.4  Startup: LLM-status log + seed-if-empty .................. ✅ main.py:69
│
├── B1 BACKEND
│   ├── B1.1  Config & Secrets
│   │   ├── B1.1.1  Typed settings accessors (CORS/limits/CRM/email) .. ✅ config.py:83
│   │   └── B1.1.2  Minimal .env loader fallback ..................... ✅ config.py:26
│   ├── B1.2  Safety (deterministic 911) ............................. 🔒 NT2
│   │   ├── B1.2.1  is_safety_concern keyword/phrase classifier ...... ✔️ safety.py:43
│   │   └── B1.2.2  Always-on interception (every message) ........... ✔️ main.py / smoke
│   ├── B1.3  LLM boundary (stateless extractor)
│   │   ├── B1.3.1  sanitize_user_input (XML strip + truncate) ....... ✔️ llm.py:69
│   │   ├── B1.3.2  extract() one-slot-at-a-time (mock path) ......... ✔️ llm.py:88/211
│   │   ├── B1.3.3  extract() Ollama JSON-schema path ................ ✅ llm.py:271
│   │   ├── B1.3.4  is_responsive() advisory gate (mock) ............. ✅ llm.py:136
│   │   ├── B1.3.5  is_responsive() Ollama path ..................... ✅ llm.py:156
│   │   └── B1.3.6  using_mock() Ollama probe / model config ......... ✅ llm.py:45  ⚠A2
│   ├── B1.4  Validation gate (don't advance on wrong answer)
│   │   ├── B1.4.1  Typed-field verdict (yesno/int/email/enum) ....... ✅ validation.py:44
│   │   ├── B1.4.2  Free-text heuristics (name/address/contact) ...... ✅ validation.py:91
│   │   ├── B1.4.3  Verbatim min-substance check .................... ✅ validation.py
│   │   ├── B1.4.4  Advisory LLM responsiveness (thresholded) ........ ✅ validation.py
│   │   └── B1.4.5  Unconfirmed-identity flagging ................... ✔️ pipeline_test
│   ├── B1.5  State machine / conversation flow ...................... 🔒 NT1
│   │   ├── B1.5.1  Session model + start()/greeting ................ ✔️ state_machine.py:238
│   │   ├── B1.5.2  Common steps (name/account/contact/issue/urgency) ✔️ smoke
│   │   ├── B1.5.3  Branch: ROOF steps ............................. ✔️ smoke
│   │   ├── B1.5.4  Branch: ELECTRICAL steps ....................... ✔️ smoke
│   │   ├── B1.5.5  Branch: SOLAR steps ............................ ✅ (gap G3)
│   │   ├── B1.5.6  Branch: MISC steps ............................. ✅ (gap G3)
│   │   ├── B1.5.7  collect→validate→advance loop + retries ......... ✔️ smoke
│   │   ├── B1.5.8  Display-only account lookup (Lookup mode) ....... ✔️ smoke
│   │   ├── B1.5.9  Read-back / confirm (+ ambiguous-confirm guard) . ✔️ smoke
│   │   ├── B1.5.10 Correction phase ............................... ✅
│   │   ├── B1.5.11 Representative / human-handoff routing .......... ✔️ security_test
│   │   ├── B1.5.12 Session hygiene (TTL, cap, LRU evict) .......... ✅ (gap: untested)
│   │   └── B1.5.13 Turn/retry limits (MAX_TURNS/MAX_RETRIES) ....... ✅
│   ├── B1.6  Lookup (least-privilege matching) ..................... 🔒 NT4
│   │   ├── B1.6.1  score_match weighting (anti-enumeration) ........ ✔️ pipeline_test
│   │   ├── B1.6.2  Deterministic name/address/email matchers ....... ✅ lookup.py:49-72
│   │   ├── B1.6.3  lookup() 2nd-factor requirement ................ ✔️ smoke
│   │   └── B1.6.4  list_customers (phone withheld) / demo .......... ✔️ smoke
│   ├── B1.7  CRM adapter (match + case) ........................... 🔒 NT1
│   │   ├── B1.7.1  StubCRMClient (threshold match + case-id) ....... ✔️ pipeline_test
│   │   ├── B1.7.2  MCPCRMClient seam .............................. 🧩 crm.py:94
│   │   ├── B1.7.3  HTTPCRMClient seam ............................. 🧩 crm.py:125
│   │   └── B1.7.4  get_client() backend selection ................. ✅ crm.py:154
│   ├── B1.8  Mailer adapter
│   │   ├── B1.8.1  InboxStoreMailer (default, no network) ......... ✔️ pipeline_test
│   │   ├── B1.8.2  GoogleWorkspaceMailer send seam ............... 🧩 mailer.py:58
│   │   └── B1.8.3  route_for (solar/matched/unverified mailbox) ... ✔️ pipeline_test
│   ├── B1.9  Pipeline disposition (match→case→route→render→send) .. ✔️ pipeline.py:44 / smoke
│   ├── B1.10 Email render (Jinja2 autoescaped) ................. 🖼️
│   │   ├── B1.10.1 render_and_store + template .................. ✔️🖼️ smoke
│   │   ├── B1.10.2 _tier urgency→label/color (electrical floor) . ✅ (gap G1)
│   │   ├── B1.10.3 _facts per-issue extraction .................. ✅
│   │   └── B1.10.4 _summary grounded one-liner ................. ✅
│   ├── B1.11 Upload (secure image pipeline)
│   │   ├── B1.11.1 MIME + magic-byte validation ................ ✔️ security_test
│   │   ├── B1.11.2 Size cap (5MB) ............................. ✔️ security_test
│   │   ├── B1.11.3 UUID secure filename (path-traversal safe) .. ✔️ security_test
│   │   └── B1.11.4 EXIF/GPS strip ............................. ✅ upload.py ⚠A1 (gap G4)
│   ├── B1.12 Rate limiter (per-IP fixed window, 429) .......... ✔️ pipeline_test
│   ├── B1.13 Security middleware
│   │   ├── B1.13.1 Locked CORS allow-list .................... ✅ main.py:32
│   │   ├── B1.13.2 Security headers (CSP/XFO/XCTO/Referrer/COOP) ✔️ pipeline_test
│   │   └── B1.13.3 Generic error handler (no stack leak) ..... ✅ main.py:59
│   ├── B1.14 Seed inbox (4 samples on first run) ............. ✔️ smoke
│   └── B1.15 API routes (8) .................................. 🔒 NT3
│       ├── B1.15.1 POST /api/chat ........................... ✔️ smoke
│       ├── B1.15.2 GET /api/emails[/{id}] ................... ✔️ smoke
│       ├── B1.15.3 GET /api/customers ...................... ✔️ smoke
│       ├── B1.15.4 POST /api/lookup ....................... ✔️ smoke
│       ├── B1.15.5 GET /api/health ....................... ✔️ pipeline_test
│       └── B1.15.6 POST /api/upload ...................... ✔️ security_test
│
├── B2 FRONTEND (vanilla JS, no build) 🖼️
│   ├── B2.1  App shell & routing
│   │   ├── B2.1.1  App bar (logo, mode toggle, 3 tabs) ...... ✅🖼️ index.html:13
│   │   ├── B2.1.2  Tab switching (chat/emails/database) ..... ✅🖼️ app.js setTab
│   │   └── B2.1.3  Mode toggle (Standard↔Lookup, restarts) . ✅🖼️ app.js toggleMode
│   ├── B2.2  Chat screen 🖼️
│   │   ├── B2.2.1  Safety banner + contact card ............ ✅🖼️ index.html:35
│   │   ├── B2.2.2  Message log + bubble roles .............. ✅🖼️ app.js addBubble
│   │   ├── B2.2.3  Quick-reply chips ...................... ✅🖼️ app.js renderQuickReplies
│   │   ├── B2.2.4  Input form + send / busy state ......... ✅🖼️ app.js sendMessage
│   │   └── B2.2.5  Upload affordance + attachment chips ... ✅🖼️ app.js renderAttachments
│   ├── B2.3  Emails screen (Gmail-style) 🖼️
│   │   ├── B2.3.1  Inbox list (newest-first, count badge) . ✅🖼️ app.js renderInbox
│   │   ├── B2.3.2  Message detail view + prev/next nav .... ✅🖼️ app.js openMessage
│   │   └── B2.3.3  escapeHtml on subject/to .............. ✅ app.js messageHtml
│   ├── B2.4  Database screen 🖼️
│   │   ├── B2.4.1  Customer table (phone withheld) ....... ✅🖼️ app.js loadCustomers
│   │   └── B2.4.2  "Try a lookup" form + result alert ... ✅🖼️ app.js runLookup
│   ├── B2.5  Design system (styles.css, 1170 lines) 🖼️
│   │   ├── B2.5.1  Tokens (color/type/spacing/radius/shadow) ✅ styles.css
│   │   ├── B2.5.2  Responsive breakpoints (tablet/mobile) . ✅ (gap: unverified visually)
│   │   └── B2.5.3  Bubble/alert/toggle component styles ... ✅🖼️
│   ├── B2.6  State & data-fetching (fetch wrappers) ...... ✅ app.js
│   └── B2.7  Frontend cross-cutting quality
│       ├── B2.7.1  Per-view states (loading/empty/error) . ⬜ (gap — needs audit)
│       ├── B2.7.2  Accessibility (WCAG AA: focus/aria/kbd) ⬜ (gap — needs audit)
│       └── B2.7.3  Error/network-failure handling ........ ⬜ (gap — needs audit)
│
├── B3 QUALITY / TESTS
│   ├── B3.1  smoke_test.py (e2e roof+electrical, safety, db) ✔️
│   ├── B3.2  security_test.py (sanitize/limits/upload/route) ✔️
│   ├── B3.3  pipeline_test.py (headers/ratelimit/crm/flag) . ✔️
│   ├── B3.4  Characterization gaps G1–G5 ................... ⬜ (Stage 1)
│   └── B3.5  Lint/type/format tooling ..................... ⬜ (none configured)
│
└── B4 DOCS & CONFIG
    ├── B4.1  README accuracy (paths/model/numbering) ...... ✅ ⚠A3/A4
    ├── B4.2  .env.example ↔ config parity ................ ✅
    └── B4.3  requirements.txt (py version note) .......... ✅ ⚠A3
```

## Full-stack coverage note
Both spines are present and decomposed to leaf granularity: **B1 BACKEND** (16
modules → ~45 leaves) and **B2 FRONTEND** (3 screens + design system + cross-cutting
→ ~20 leaves). The seam between them is the API contract (NT3, B1.15 ↔ B2.6).
The visible imbalance to resolve during the run: the **frontend cross-cutting
quality** branch (B2.7 — states, a11y, error handling) is the least-verified area
and the most likely source of EXTEND-stage work.
