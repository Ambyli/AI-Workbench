# BUILD_MANIFEST.md — Single Source of Truth (Build Tree Framework, Phase 3)

## Goal Statement (reproduced from GOAL.md — the lens for every row)

**Zeo Energy Local-LLM Service Chatbot.** Deterministic code owns flow + all
security/authority decisions; the LLM is a stateless, no-authority field
extractor. Priority order: Safety > Security/injection-resistance > Trustworthy
handoff > Homeowner UX > Runs-with-no-creds. See [GOAL.md](GOAL.md).

## Status legend
⬜ NOT_STARTED · 🧩 STUBBED · 🔨 IN_PROGRESS · ✅ IMPLEMENTED (no run-evidence)
· ✔️ VERIFIED · 🚫 BLOCKED · 🔒 NO_TOUCH · 🖼️ renders visual (needs saved artifact)

## Progress summary (as-is baseline, 2026-07-06)
- **Existing & VERIFIED (re-ran suites):** ~30 leaves (safety, core flow, lookup,
  upload security, rate limit, headers, CRM stub, pipeline, seed, API routes).
- **Existing & IMPLEMENTED (code complete, not directly asserted):** ~25 leaves.
- **🧩 STUBBED (deliberate seams):** 3 (CRM mcp, CRM inhouse, Gmail send).
- **🔒 NO_TOUCH surfaces:** 6 (NT1–NT6, see RECON.md §3).
- **⬜ Actionable work (this run):** findings A1–A4, gaps G1–G5, frontend quality
  B2.7.1–B2.7.3.
- Baseline: 3/3 suites PASS · no lint/type tooling configured.

---

## Section 1 — 🔒 NO_TOUCH (protected; confirm unchanged at audit)

| ID | Surface | Status | Notes |
|----|---------|--------|-------|
| NT1 | LLM-no-authority thesis (flow/auth/copy stays deterministic) | 🔒 | GOAL.md thesis |
| NT2 | `safety.is_safety_concern` determinism + always-on | 🔒 | may extend keywords only |
| NT3 | Frontend-facing API field shapes | 🔒 | additive-only |
| NT4 | Least-privilege lookup (2nd factor, thr 0.9, phone withheld) | 🔒 | anti-enumeration |
| NT5 | Scaffolded seams raise NotImplementedError | 🔒 / 🧩 | await external inputs |
| NT6 | Green-without-network default path | 🔒 | no creds/model required |

## Section 2 — 🧩 STUBBED seams (tracked, revisit only with external inputs)

| ID | Tree Path | Feature | Contract / Acceptance | Depends On | Status | Evidence / Notes |
|----|-----------|---------|-----------------------|------------|--------|------------------|
| B1.7.2 | B1.7 CRM | MCPCRMClient | Raises NotImplementedError w/ expected tool surface (`crm_lookup_account`,`crm_open_case`) | In-house CRM MCP schemas | 🧩 | crm.py:94 — do not wire (NT5). Verify it fails safe, not silently. |
| B1.7.3 | B1.7 CRM | HTTPCRMClient | Raises NotImplementedError w/ expected REST surface | In-house CRM REST docs | 🧩 | crm.py:125 (NT5) |
| B1.8.2 | B1.8 Mailer | GoogleWorkspaceMailer.send | Gmail API send path; disabled unless `EMAIL_SEND_ENABLED=true` + creds | GW service-account creds | 🧩 | mailer.py:58 (NT5) |

## Section 3 — ⬜ Actionable work (this run, by stage)

### Stage 1 — STABILIZE & UPGRADE

| ID | Tree Path | Feature | Contract / Acceptance Criteria | Depends On | Status | Evidence / Notes |
|----|-----------|---------|--------------------------------|------------|--------|------------------|
| G4 | B1.11.4 / B3.4 | EXIF-strip characterization | A test that uploads a real JPEG **with** EXIF/GPS and asserts the saved file has metadata removed. Currently only the *fallback* path runs (A1). | — | ⬜ | Root-cause A1: PIL couldn't identify the synthetic test bytes → strip skipped. Must pin true behavior before any change. |
| A1 | B1.11.4 | EXIF strip actually executes | For a valid image, `validate_and_save_upload` strips EXIF/GPS (no fallback). Privacy invariant. Baseline green. | G4 | ⬜ | Fix only after G4 proves current behavior. |
| G1 | B1.10.2 / B3.4 | Tier-mapping characterization | Test pinning `_tier()` for representative urgencies + electrical floor. | — | ⬜ | Pin before touching render. |
| G2 | B1.4 / B3.4 | Validation matrix characterization | Tests for name/address/contact/verbatim accept+reject + retry accounting. | — | ⬜ | |
| G3 | B1.5.5-6 / B3.4 | Solar+Misc e2e characterization | Smoke-style e2e for solar and misc issue types through to email. | — | ⬜ | smoke covers roof+electrical only. |
| G5 | B1.2.1 / B3.4 | Safety keyword-matrix characterization | Test pinning which phrases do/don't fire `is_safety_concern`. | — | ⬜ | NT2: pin before extending. |
| A2 | B1.3.6 | Model config reconciliation | `OLLAMA_MODEL` default reflects an installed model, OR startup logs a clear "model not found" instead of a silent 404. Mock path unaffected. | — | ⬜ | Installed: qwen3.5:9b/4b, not qwen3:14b. |
| A3 | B4.1/B4.3 | Python-version doc fix | Docs/requirements state the real supported floor (runs on 3.9.6). | — | ⬜ | doc-only. |
| A4 | B4.1 | README accuracy | Remove stale `C:\Users\...` paths + `file:///` links; fix duplicated "6." numbering in Security list. | — | ⬜ | doc-only. |
| B3.5 | B3.5 | Lint/format tooling | Introduce a lightweight, non-breaking checker (e.g. `ruff`) as the type/lint gate, config committed; baseline stays green. | — | ⬜ | Optional-but-recommended; keeps future passes honest. |

### Stage 2 — OPTIMIZE (metric-gated; no delta ⇒ no change)

| ID | Tree Path | Feature | Contract / Acceptance Criteria | Depends On | Status | Evidence / Notes |
|----|-----------|---------|--------------------------------|------------|--------|------------------|
| O1 | B1.x | Candidate profiling | Measure real hot paths (chat turn latency in mock mode, email-store JSON write on every render, `score_match` over customer list) and record before-numbers. | Stage 1 green | ⬜ | Discover targets from measurement, not intuition. Safety/auth path is off-limits for speed trades (GOAL tension). |

### Stage 3 — EXTEND (goal-justified; go-signal required)

| ID | Tree Path | Feature | Contract / Acceptance Criteria | Depends On | Status | Evidence / Notes |
|----|-----------|---------|--------------------------------|------------|--------|------------------|
| B2.7.1 | B2.7 FE quality | Per-view UI states | Loading/empty/error states for chat send, email list, lookup, customer table. | Stage 1 | ⬜ | Candidate — needs go-signal + vision check. |
| B2.7.2 | B2.7 FE quality | Accessibility (WCAG AA) | Keyboard nav, visible focus, aria roles on tabs/chat/table, contrast. | Stage 1 | ⬜ | Candidate. |
| B2.7.3 | B2.7 FE quality | Network-failure handling | Graceful UI on fetch failure / offline (no silent dead buttons). | Stage 1 | ⬜ | Candidate. |
| E-tbd | — | Real-LLM smoke on qwen3.5 | Optional: verify the Ollama extract path against the actually-installed model (out of the no-network default). | A2 | ⬜ | Candidate — depends on A2. |

## Section 4 — Existing VERIFIED/IMPLEMENTED leaves (accounted for; no work unless a finding lands)

Compactly recorded (full tree in [BUILD_TREE.md](BUILD_TREE.md)); evidence = the
suite that exercises each. Not re-listed row-by-row to avoid noise — the tree is
the per-leaf ledger for these, and any that a finding touches gets promoted to a
full row above.

- **✔️ VERIFIED (suite-backed):** B1.2.1-2, B1.3.1-2, B1.4.5, B1.5.1-4,7-9,11,
  B1.6.1,3,4, B1.7.1, B1.8.1,3, B1.9, B1.10.1🖼️, B1.11.1-3, B1.12, B1.13.2,
  B1.14, B1.15.1-6.
- **✅ IMPLEMENTED (code complete, not directly asserted):** T.1-4, B1.1.1-2,
  B1.3.3-6, B1.4.1-4, B1.5.5-6,10,12-13, B1.6.2, B1.7.4, B1.10.2-4, B1.11.4,
  B1.13.1,3, all of B2.1–B2.6.

---

## Section 5 — EXTEND delivered (Stage 3)

| ID | Feature | Contract / Acceptance | Status | Evidence |
|----|---------|-----------------------|--------|----------|
| E1 | Gmail send w/ photo attachments | Service-account+DWD; MIME multipart with uploaded photos (traversal-guarded); draft-then-send or direct. Gated by `EMAIL_SEND_ENABLED` (default off → Inbox store). | ✔️ | `integration_gmail_chat_test.py` MIME + fake-service draft/send checks. Live send needs GW creds (still 🧩 for the actual network call). |
| E2 | Google Chat team notify | Pluggable adapter stub\|webhook\|api; cardsV2 to routed team's Space webhook; routing mirrors `route_for`. Gated by `CHAT_SEND_ENABLED` (default off → in-app feed). | ✔️ | integration test: routing, stub feed, captured webhook payload, unconfigured-fallback. |
| E3 | Pipeline wiring | `dispatch` attaches photos to the email AND fires the chat notify; `DispatchResult` carries `chat_notified`/`chat_space`. | ✔️ | integration test `test_pipeline_wires_attachments_and_chat`. |
| E4 | `/api/chat-notifications` | GET returns the in-app chat feed (demo visibility). | ✔️ | integration test + route count 12→13. |
| B1.8.2 | GoogleWorkspaceMailer.send | Was 🧩 STUBBED (NotImplementedError). Now implemented + offline-tested; only the live network send remains creds-gated. | ✔️/🧩 | Logic verified; live path 🧩 pending creds (NT5). |
| B1.7.2/3, ApiChatNotifier | CRM mcp/inhouse + Chat API seams | Remain deliberate 🧩 seams (await external inputs). | 🧩 | Unchanged. |

### Change log (appended every step)
- 2026-07-06 — Manifest initialized (Phase 3). Baseline 3/3 green @ `d192516`.
  git: `main` tracking `origin/main`; local June-19 work preserved on
  `snapshot/local-jun19`.
- 2026-07-06 — Stage 1 (Stabilize/Upgrade) complete: characterization tests
  G1–G5 (+EXIF strip proven on a real image); A2 missing-model warning; adopted
  **qwen3.5:9b** + disabled thinking (~30× faster real extraction, live-verified);
  A3/A4 doc fixes; ruff gate (F,E9). Suite 4/4 green. Pushed to `origin/main`.
- 2026-07-06 — Stage 3 (Extend) delivered: **Gmail send with photo attachments**
  and **Google Chat per-team notifications** (E1–E4), both default-OFF and
  offline-tested. Suite 5/5 green. Pushed to `origin/main` @ `10e967c`.
  Note: Stage 2 (Optimize) was captured opportunistically inside Stage 1 (the
  think=false 30× win) at the user's direction to prioritize the Gmail/Chat
  build; a dedicated Optimize pass remains available (see O1).
