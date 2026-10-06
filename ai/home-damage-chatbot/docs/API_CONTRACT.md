# Service Assistant API — Contract v2.1

This is the contract between the website's Service page (React) and the chatbot backend
(FastAPI). **v2** is the post-remediation contract: server-issued sessions, a read-only
queue check, and session-bound uploads. The frontend builds against this document, and the
backend is changed to match it. Any change to this file must be agreed by both sides.

**v2.1 (2026-10-02, additive, older clients keep working):** `outcome` on finished
conversations, the `notice` message kind, and the account-name-first question order.

All routes are same-origin under `/api/*`, served by nginx and proxied to FastAPI. JSON
unless noted. No other `/api` routes are reachable publicly.

---

## Sessions

- Sessions are **issued by the server**. The client never invents a session id.
- To start, call `POST /api/chat` with **no `session_id`** and `message: ""`. The response
  carries the new `session_id` (an opaque token of 22 or more URL-safe characters).
- The client keeps `session_id` and the transcript in `sessionStorage` (key
  `zeo_service_chat_v1`), so a refresh resumes the conversation.
- If the server doesn't know a `session_id` (expired, purged, or never issued), any route
  returns **HTTP 404** `{"detail": "session_expired"}`. The client clears its storage, shows
  the system note "Your previous session expired — let's start again.", and starts a new
  session.

## `POST /api/chat`

Request:
```json
{
  "session_id": "string | omitted on start",
  "message": "string (0-1000 chars)",
  "attachments": ["upload filename", "..."],
  "pointer": { "lat": 27.95, "lng": -82.45 }
}
```
- `attachments` is only sent at the photo step. Every name must have been returned by
  `/api/upload` **for this session**.
- `pointer` is sent only at the `damage_pointer` step, alongside `message` = the uploaded
  pointer-image filename. It gives staff an exact Maps link.
- `mode` is always `"standard"` (it can be omitted). The public page never uses lookup mode.

Response 200:
```json
{
  "session_id": "string",
  "messages": [{ "text": "string", "kind": "normal | system | safety | notice" }],
  "quick_replies": ["string"],
  "allow_upload": false,
  "await_step": "string | null",
  "state": "collect | confirm | correct | done | human | safety | queued | queue_full",
  "done": false,
  "outcome": "submitted | handoff | ended | null",
  "queued": false,
  "queue_position": 0,
  "location": { "address": "string", "lat": 0.0, "lng": 0.0 },
  "summary": {
    "rows": [{ "label": "string", "value": "string" }],
    "unverified": ["string"],
    "note": "string"
  }
}
```
- `location` is present **only** when `await_step == "damage_pointer"`, otherwise `null`.
  `lat`/`lng` may be `null` if geocoding failed; the client then lets the user search or pan.
- `summary` is present **only** when `state == "confirm"`, otherwise `null`. It holds
  structured read-back rows for the confirmation card. The same content is also in
  `messages[]` as plain text, so an older client still works.
- `outcome` (v2.1) is set only when `done` is true. `submitted`: a request was sent to the
  team, so show the green success card. `handoff`: the customer was given a phone number.
  `ended`: any other ending (turn limit, queue full). `null` while the chat continues. A
  client that sees no `outcome` (older server) falls back to "done right after a confirm".
- `kind: "notice"` (v2.1) is an important status the customer must not miss. Today that
  is only the account-check result. Render it as an info card, not as muted system text.
  Unknown kinds render as a normal bubble.
- `messages[].text` is **plain text**. Render it with `textContent` or as a React text node,
  **never as HTML**. `\n` means a line break.

### How the UI renders each step (driven by `await_step`)

| `await_step` | UI | What the client sends |
|---|---|---|
| `account_name` (first question) | Composer | The name on the Solar Account |
| `account_address` | Composer | The service address. The reply that follows carries one `notice` with the account-check result ("We found your account..." or "We were unable to locate your account..."). The account itself is never described. |
| `issue_type` | 4 icon cards (Solar / Electrical / Roof / Other) | The card's quick-reply text: `"Solar not producing"`, `"Electrical"`, `"Roof"`, `"Misc / other"` |
| `urgency` | 1–10 slider + "Send" | `"7"` |
| `damage_pointer` | Satellite map step: (a) "Is this your home?", (b) drop the pin, (c) confirm | Upload the image, then send `message=<filename>` and `pointer={lat,lng}`. **Never** send `"fallback"`. |
| `_photos` | Photo drop zone plus a "Skip" chip | Upload each file, then send `message:""` with `attachments:[...]`. Skip sends `"skip"`. |
| Any other step with `quick_replies` | Chips (plus a free-text composer) | The chip text or typed text |
| `__confirm__` (`state: confirm`) | Read-back card from `summary`, with "Yes, send it" / "Change something" | `"Yes, send it"` / `"No, change something"` |
| `__done__` (`done: true`) | Success card; composer locked; "Start a new request" | — |

### Queue

- There are **3 active conversations at a time** (`MAX_ACTIVE_USERS=3`). Extra users wait in a FIFO queue (`MAX_QUEUE_SIZE`, 10 in the mock).
- `state: "queued"` means waiting. `queue_position` is 1-based. The client shows the waiting room and polls `/api/queue/status`.
- `state: "queue_full"` means the queue itself is full (`queue_position: -1`, `done: true`). The client shows the full-capacity panel, offering call, email, and the classic form, plus "Try again".

## `POST /api/queue/status`

This route is read-only: it **never** grants a slot or creates a queue entry. For a session
already waiting, it refreshes that session's place in line.

Request: `{ "session_id": "string" }`
Response 200:
```json
{ "state": "active | queued | untracked", "queue_position": 3 }
```
- Poll every **3 s** while queued. Stop polling while the tab is hidden, and resume on
  `visibilitychange`. A queued session that isn't polled for **60 s** loses its place.
- When the response is `active`, the slot has been granted. Call `POST /api/chat`
  `{session_id, message: ""}` to receive the greeting.
- When the response is `untracked`, the place was lost. Call `POST /api/chat` with the same
  `session_id` to rejoin at the back of the queue.

## `POST /api/upload` (multipart/form-data)

Fields: `file` (the image) and `session_id`.
- 200: `{ "filename": "string" }`
- 400: invalid or unparseable image
- 404: `session_expired`
- 413: larger than 5 MB
- 429: too many uploads

Accepted types are JPEG, PNG, WebP, and GIF. There's a 5 MB cap per file and 10 files per
session. The server strips EXIF/GPS and rejects any file it cannot re-encode.

## `GET /api/health`

- 200 `{ "ok": true, "assistant_available": true }` means use the assistant.
- 503 `{ "ok": false, "assistant_available": false }` (model required and down), or any
  network error, means **auto-switch to the classic form**, with the note "Our assistant is
  offline right now — you can still submit a request below."

## Errors (all routes)

| Status | Client behavior |
|---|---|
| 404 `session_expired` | Reset the session (see Sessions) |
| 413 / 400 on upload | Show an inline error on that file; the rest of the chat continues |
| 422 | Show an inline "That message couldn't be sent" and keep the typed text |
| 429 | Retry after the `Retry-After` header (default 5 s); show "One moment…" |
| 5xx / network | Mark the user bubble "Not sent — Retry" and keep the typed text. After 2 failures, offer the classic form. |

## Timeouts

- The client aborts `/api/chat` after **30 s** and treats it as a network error.
- The backend's model budget is about 10–15 s per model call, which fits within that.
- The typing indicator shows while a request is in flight.
