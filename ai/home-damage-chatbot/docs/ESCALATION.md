# Escalated Customers, Handoffs and Incomplete Requests

How the Service assistant handles customers who want a person, who are upset, who stop
partway, or whose answers can't be understood, and what the business still needs to put
in place. Covered by `tests/escalation_test.py` (offline) and flows F7, F16, F18 and F19 in
`tests/e2e/real_flows.py` (real model).

## What the assistant does

### 1. Asking for a person (any step)

**Triggers only on clear requests**, for example:
- "can I talk to a real person", "I want to speak with a manager", "get me a human"
- "agent please", "representative", "what's your phone number?"
- "how do I contact someone", "stop with the bot"

Ordinary answers that merely contain those words do **not** trigger it. Each of these used
to end the chat by mistake (fixed 2026-10-02):
- "my phone number is …"
- "my insurance agent came out"
- "the support beam is cracked"
- "the person on the account is …"
- "the property manager called"

**Reply** (chatbot best practice: empathy, one clear next step, no gatekeeping, no loops):

| Situation | Number given |
|---|---|
| Solar production, microinverters or Enphase (solar issue chosen) | (727) 349-4057, Service Department |
| Roof, electrical, battery or other damage | (727) 382-0075, Nonstandard Department |
| **No issue type chosen yet** | **(727) 382-0075** (default, owner decision) |

The reply also includes:
- the hours (Mon–Fri, 9am–5pm Mountain Time)
- the department email
- outside business hours, "Our team is closed right now and opens at 9am Mountain Time on the next business day…"
- if the customer had already given contact or account details, "I've passed along what you've told me so far, so you won't have to start over"; otherwise, a reminder to have their account name and address ready
- if the customer had been upset, an apology first

### 2. Frustrated or upset customers

Frustration or escalation language triggers the response below. Examples:
- "ridiculous", "unacceptable", "fed up", "waste of time"
- profanity
- "complaint", "lawyer", "BBB"

What happens:
- **Acknowledged once** with an info card: "I'm sorry this has been frustrating. If you'd rather talk to someone now, call … Otherwise, I'll keep going with your request." The card uses the right number for their issue.
- **The conversation continues.** If the same message also answered the question ("this is ridiculous, my roof is leaking"), the answer still counts.
- **It is never repeated** in the same chat, so the customer isn't nagged.
- **Staff see a red "Customer may be upset" banner** on the handoff email or alert.

### 3. Incomplete requests: department alerts

The department gets an **"[Incomplete]" email** (plus the team-chat notification) when a customer:
- asks for a person before finishing,
- hits the 60-turn limit, or
- **stops responding** for 20 minutes (`ABANDON_AFTER_SECONDS`), checked every minute and again before an expired session is deleted.

The alert lists:
- why the request stopped
- the answers collected so far
- **what was not answered**
- the account-check result, or the closest matching projects
- any flags: 911 message shown, customer may be upset, unclear answers

Rules:
- **Only sent when staff can follow up:** the customer gave a contact, or both the account name and address. Otherwise there's nothing to act on.
- **Sent at most once per conversation.** If the customer comes back and finishes, the finished request is sent normally and notes that an alert went out earlier.
- **Never opens a CRM case.** Staff decide after following up.
- **Delivered by the account service**, like every handoff (separation layer). If the service is down, the alert waits in the outbox.

### 4. Answers that can't be understood

- Each required question is asked again up to twice, with a specific hint.
- After that, the account name and address are kept but **flagged "Unverified"**.
- Other answers are marked **"Unclear answers: …"** on the staff email. An urgency the customer never gave shows as "not given", not a made-up number.
- An issue type that stays unclear goes to the general ("other damage") branch, and staff see it listed as unclear.
- The 911 safety check runs before everything else, on every message.

## What the business still needs (not code)

| # | Item | Why | Owner |
|---|---|---|---|
| 1 | **Who works "[Incomplete]" alerts, and how fast** | They contain a customer waiting for a call back. They need an owner and a target time (for example, same business day). | Service and Nonstandard managers |
| 2 | **Mailbox mapping** | Alerts and requests route to `MAILBOX_DISPOSITION` / `MAILBOX_UNVERIFIED` / `MAILBOX_SOLAR` (placeholder addresses). Confirm the real department inboxes, including a **Nonstandard** inbox for roof, electrical and battery. | Ops (Stage 5 mailbox reconciliation) |
| 3 | **After-hours coverage** | Outside Mon–Fri 9–5 MT the assistant says the team is closed. Is there an after-hours or emergency line to offer instead? Today only 911 is offered for danger. | Service leadership |
| 4 | **Escalation path for complaints or legal threats** | The assistant flags "lawyer / BBB / complaint" as upset but routes like any request. Should those also go to a supervisor or customer-care lead inbox? | Customer Care |
| 5 | **Idle threshold** | 20 minutes before "stopped responding" fires. Too short creates noise from people taking photos; too long delays follow-up. | Product owner |
| 6 | **Callback promise wording** | "Someone will locate your account before reaching out" and "1–2 business days" must match what staff can actually meet (and the website's "one business day" promise; still open). | Product owner + Legal |
| 7 | **Monitoring** | A daily count of incomplete alerts, handoffs and upset flags shows where the form loses people. | Ops |
| 8 | **Staff script for upset callers** | Calls from upset customers will arrive with context already emailed; staff should check the alert before calling back. | Customer Care |

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `ABANDON_AFTER_SECONDS` | 1200 | Idle time before a "stopped responding" alert |
| `MAX_TURNS` (code) | 60 | Turns before the assistant hands off |
| `SUPPORT_HOURS` (code) | Mon–Fri, 9am–5pm Mountain Time | Shown in every handoff; drives the after-hours line |
| Department numbers (code) | 349-4057 / 382-0075 | `SERVICE_DEPT` / `NONSTANDARD_DEPT` in `backend/state_machine.py` |
