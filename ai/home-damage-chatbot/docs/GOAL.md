# GOAL.md — Program Purpose & Intent (Build Tree Framework, Phase G)

> This is the lens for every downstream decision. Discovery, contracts,
> implementation, optimization, and audit are all judged against this — not only
> against the literal task request. A technically correct change that drifts from
> this purpose is not a success.

## What this program is

**Zeo Energy — Local-LLM Service Chatbot.** A conversational replacement for a
solar-energy company's static "Service Request" web form. It intakes home-damage
/ service issues from homeowners (Roof, Electrical, Solar-production, Misc),
optionally matches the requester to a customer account, and produces a
**template-rendered staff handoff email** plus (scaffolded) CRM case disposition
and email routing.

Who it's for: (a) **homeowners** — the chat UI, must be low-friction, forgiving,
and safe; (b) **Zeo staff** — the handoff email / CRM case / routing, which must
be accurate, structured, and trustworthy enough to act on.

## The load-bearing design thesis (the thing that must never be broken)

**Deterministic code owns the flow and every security/authority decision; the LLM
is a narrow, stateless field-extractor with no authority.**

- The state machine decides branching, slot order, limits, when to look up an
  account, when to open a CRM case, where to route, and what the email says.
- `safety.py` 911 classification is **deterministic, never an LLM judgment**, and
  fires on **every** message regardless of flow position.
- The LLM (`llm.py`) is only ever asked to (a) extract one slot value at a time and
  (b) give an **advisory** "is this answer responsive?" signal. It sees no
  conversation history, so multi-turn injection/drift is structurally impossible.
- The staff handoff email is **template-rendered (Jinja2, autoescaped), never
  free-written** by the model.

This is the program's reason for being built this way. Any change that lets the
LLM decide flow, authorize a lookup/match, choose routing, or write staff-facing
copy directly is a **goal violation**, even if it passes tests.

## What "success" / "quality" means in THIS domain

This is a **safety- and trust-critical intake system**, not a toy chatbot.
Priorities, in order:

1. **Safety** — the 911/hazard path must fire deterministically, everywhere,
   every turn. Never regressed, never LLM-gated.
2. **Security & injection-resistance** — untrusted homeowner text can never
   escalate into flow control, data authority, or staff-facing content.
   (Input sanitization, XML delineation, Pydantic edge validation, per-IP rate
   limiting, locked CORS, security headers, no stack-trace leakage, session TTL.)
3. **Correctness & trustworthiness of the handoff** — staff act on the email/case,
   so identity that couldn't be validated must be visibly flagged
   **⚠ unconfirmed**, matches must be high-confidence, and the read-back/confirm
   step must reflect reality. No silent "looks matched."
4. **Homeowner UX** — forgiving intake (don't advance on a non-answer), a clear
   read-back/confirm, guided human handoff, graceful "talk to a person" at any
   point. Latency/feel matter because a real person is waiting.
5. **Operability without secrets** — the demo must run end-to-end with **no
   credentials and no model** (mock LLM + in-memory inbox + stub CRM), so adapters
   are provider-agnostic seams (`stub|mcp|inhouse` CRM, in-app inbox vs Gmail).

## Implicit domain invariants (not always stated in code)

- **Least privilege on lookup:** account lookup returns the *minimum* context and
  requires a **second factor** (name alone is rejected; name+address or name+email
  matches). The LLM never has lookup authority.
- **Fail safe, not open:** on ambiguity (e.g. "maybe" at confirm), do **not**
  advance or guess a destructive branch; re-prompt / hold.
- **Privacy:** uploaded photos must be stripped of EXIF/GPS before storage;
  logs must not leak PII in the clear (they hash/redact — e.g. `acct_name=i***(3)`).
- **Bounded resources:** message length caps, upload size/type caps, session
  count/TTL caps, rate limits — a homeowner (or attacker) cannot exhaust the box.
- **Green-without-network:** the full test suite runs in-process (`TestClient`,
  `USE_MOCK_LLM=1`) with no live server, no Ollama, no external calls.

## Known scaffolded seams (intentional NotImplementedError, not bugs)

- CRM `mcp`/`inhouse` backends (await in-house CRM tool schemas/REST docs).
- Gmail real-send path (`EMAIL_SEND_ENABLED=true`, awaits service-account creds).
These are deliberate placeholders with defined interfaces — treat as tracked
STUBBED seams, not incomplete work to "finish" without the missing inputs.

## Environment reality (as observed on this machine, 2026-07-06)

- Runs on the Mac mini (Apple M4). Ollama is live at `localhost:11434` with
  **`qwen3.5:9b`** and **`qwen3.5:4b`** installed — **not** the `qwen3:14b` the
  README/config assume. Real config-vs-reality drift to reconcile in Stage 1.
- venv is **Python 3.9.6** though docs say 3.10+. App imports and all tests pass
  under 3.9.6, so it's doc/config drift, not a break.

## Tensions to watch

- "Optimize for speed" vs. safety/audit: the safety check and the deterministic
  boundary are on the hot path *by design*. Never optimize by making safety
  probabilistic, LLM-gated, cached-stale, or skippable. Latency wins that trade
  only where they don't touch the safety/authority path.
- "Add a feature" vs. the LLM-has-no-authority thesis: new features must keep
  authority in deterministic code. A feature that hands the model the wheel is
  out of bounds regardless of demand.
