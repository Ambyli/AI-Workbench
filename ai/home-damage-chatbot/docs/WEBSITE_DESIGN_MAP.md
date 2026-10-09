# Website Design Map — Service Request page + chatbot

This document maps the design system and behavior of `New-Zeo-Website` (the live
site at `zeoenergy.com`, deployed from `main` by Cloud Build). It covers what the
chatbot must match to build a new version of the Service Request page with the
chatbot embedded. The design facts come from the source and from the live page
rendered on 2026-09-30.

Target page today: `src/app/pages/go-solar/ServicePage.tsx`, routed at both `/service`
and `/contact/service` (`src/app/routes.tsx` L44-45).

---

## 1. Design tokens

### Color (Tailwind v4 `@theme inline`, `src/styles/theme.css` L120-137)

| Token | Hex | Where the site uses it | Chatbot role |
|---|---|---|---|
| `zeo-navy` | `#2b3a4c` | Hero band, hover state on icon circles | Chat header bar |
| `zeo-blue-dark` | `#3d5a80` | Icon circles on the contact cards | User message bubble, bot avatar |
| `zeo-blue-light` | `#98c1d9` | Card borders (`/40`), focus rings, hero eyebrow text | Borders, focus ring, "online" accent |
| `zeo-blue-pale` | `#e3eef5` | Card gradients, commitment icon circles | Bot message bubble |
| `orange-500` / `zeo-orange` | `#ff6633` | Primary CTAs, selected issue card, urgency slider | Send button, selected quick reply |
| `orange-600` | `#e55525` | Hover on primary CTAs | Send button hover |
| `orange-50` / `orange-200` | `#fff5f0` / `#ffc9b3` | Selected-card fill, roof-map panel | Selected-chip fill, map step panel |
| `red-50` / `red-200` / `red-700` | Tailwind defaults | 911 safety bar, error states | Safety (911) message bubble |
| `green-50` / `green-200` / `green-500` | Tailwind defaults | Success state | Submission-confirmed state |
| `muted-foreground` | `#717182` | Secondary text | Timestamps, helper text |
| `border` | `rgba(0,0,0,.1)` | Default input and card borders | Input border, divider |

The site is **light-only**. `.dark` tokens exist but nothing toggles them, so don't build a dark variant.

### Typography

- Fonts load through `<link>` in `index.html` L26: **Barlow** 700/800/900 and **Inter** 400/500/600. **Never** use CSS `@import` (the site's CLAUDE.md forbids it).
- Body: Inter, 16px base. `label` and `button` default to weight 500.
- `h1`–`h4`: Barlow, **weight 900**. The chatbot header title and summary-card headings should use `h3`/`h4` so they inherit this.
- Eyebrow pattern: `text-xs font-semibold tracking-widest uppercase text-zeo-blue-light`.

### Shape, depth, and motion

- Radius: primary cards `rounded-2xl`, inputs and buttons `rounded-lg`, selectable cards `rounded-xl`, icon circles `rounded-full`.
- Primary card recipe (the current form container, which the chat container should reuse exactly):
  `rounded-2xl border-2 border-zeo-blue-light/40 bg-gradient-to-br from-white to-zeo-blue-light/5 shadow-xl`
- Input recipe (`inputCls()` in ServicePage.tsx L148-152):
  `w-full rounded-lg border bg-white px-4 py-3 focus:outline-none focus:ring-2 focus:ring-zeo-blue-light transition-shadow`
- Primary button: `rounded-lg bg-orange-500 px-8 py-4 text-white font-medium hover:bg-orange-600 shadow-lg hover:shadow-xl`, with `whileHover={{scale:1.03}} whileTap={{scale:0.97}}`.
- Motion: `motion/react`. Entrances use `opacity 0→1`, `y 12–24→0`, 0.35–0.55s. Staggered lists use `delay: i * 0.07`. Selected-state spring: `stiffness: 260, damping: 18`.
- Icons: `lucide-react` only. The service page uses `Sun`, `Zap`, `Wrench`, `HelpCircle`, `Phone`, `Mail`, `Clock`, `AlertTriangle`, `Send`, `CheckCircle`, `MapPin`.
- **No `backdrop-blur`.** It causes scroll jank (site CLAUDE.md, performance section).

---

## 2. Current page anatomy (keep everything except the form)

| # | Section | Key classes / notes | Keep? |
|---|---|---|---|
| 1 | Hero band | `bg-zeo-navy py-28 text-center`. Eyebrow "Customer Service", H1 "We're here when you need us.", subtext `text-white/60` | Keep; adjust the subtext to mention the assistant |
| 2 | 911 safety bar | `bg-red-50 border-b border-red-200`, `AlertTriangle`, "call **911** immediately" | Keep; it mirrors the chatbot's safety interceptor |
| 3 | Contact cards (3) | Call (727) 349-4057, Email `serviceintake@zeoenergy.com`, Hours Mon–Fri 9–5 MT | Keep |
| 4 | **Service Request form** | Card recipe above, `max-w-4xl` | **Replace with the chatbot panel** (keep the classic form available as a fallback) |
| 5 | Our Service Commitment | 4 cards, `bg-zeo-blue-pale` icon circles | Keep |
| 6 | Emergency Support | Image plus `bg-orange-50` panel, orange phone CTA | Keep |

---

## 3. Chatbot component recipes (derived from the tokens above)

| Element | Recipe |
|---|---|
| **Chat container** | The same card recipe as the current form (`max-w-4xl`). Fixed height on desktop (about 640px), with an internal scrolling message list |
| **Header bar** | `bg-zeo-navy text-white rounded-t-2xl px-6 py-4`. Avatar: a `bg-zeo-blue-dark` circle with the ZEO mark or `Wrench`. Title "ZEO Service Assistant" (h4, Barlow 900). Status line "Typically replies instantly" with a small `bg-green-400` dot. A "Start over" ghost button on the right |
| **Bot bubble** | Left-aligned, `bg-zeo-blue-pale text-foreground rounded-2xl rounded-tl-md px-4 py-3 max-w-[80%]` |
| **User bubble** | Right-aligned, `bg-zeo-blue-dark text-white rounded-2xl rounded-tr-md px-4 py-3 max-w-[80%]` |
| **System note** | Centered, `text-xs text-muted-foreground` (for example "Got 2 photos — thanks.") |
| **Safety (911) bubble** | `bg-red-50 border border-red-200 text-red-700 rounded-xl` with `AlertTriangle`, the same look as the page's safety bar |
| **Quick replies** | Pill chips: `rounded-full border-2 border-border px-4 py-2 text-sm font-medium hover:border-zeo-blue-light`. Pressed state: `border-orange-500 bg-orange-50` |
| **Issue-type step** | Instead of plain chips, render the **same 4 icon cards as the current form** (Sun/Zap/Wrench/HelpCircle, `rounded-xl border-2`, orange when selected). This keeps visual continuity |
| **Urgency step** | The same orange slider as the form (`accent-orange-500`, Low / Moderate / Emergency labels), inline in the conversation |
| **Damage-pointer step** | Reuse the existing `RoofLeakMap` component (Leaflet, Esri imagery, Nominatim geocode) inside an `orange-50 / orange-200` panel, as the form does today |
| **Photo step** | A dashed drop zone modeled on the commented-out form markup (ServicePage L612-646): `border-2 border-dashed hover:border-orange-400` |
| **Read-back summary** | A structured card (label/value rows), not a monospace text wall. It ends with "Yes, send it" (orange primary) and "Change something" (outline) |
| **Typing indicator** | A bot bubble with 3 dots pulsing via `motion` opacity (compositable; no box-shadow animation) |
| **Composer** | The input recipe plus an orange square send button with `Send`, and an attach button (`Paperclip`) shown only when `allow_upload` is true |
| **Success state** | The same green success card the form uses (`border-green-200 bg-green-50`, spring-in `CheckCircle`) |
| **Queue state** | A bot bubble with the position number and an indeterminate progress bar in `zeo-blue-light` |

---

## 4. "Acts like a professional chatbot": behavior spec

- **Paced replies:** show the typing indicator, and reveal multi-message bot turns one bubble at a time (about 350–600ms apart, scaled by message length). Keep the total delay under about 1.5s.
- **Auto-scroll** to the newest message, but only if the user is already near the bottom. Show a "New messages ↓" pill otherwise.
- **Composer:** Enter sends and Shift+Enter adds a newline. Disable input while awaiting a reply, return focus to the input after each bot turn, and clear quick replies once one is used.
- **Resilience:** on a network error, show an inline "Couldn't send — Retry" on the user bubble. Never lose typed text.
- **Session persistence:** keep `session_id` plus the transcript in `sessionStorage` so a page refresh resumes the conversation (the site already stores form data in `sessionStorage`).
- **Escape hatches, always visible:** "Call (727) 349-4057", "Use the classic form", and a human handoff handled by the backend.
- **Accessibility:** the message list uses `role="log" aria-live="polite"`. Buttons need labels. Keep visible focus rings (`ring-zeo-blue-light`), make every control keyboard reachable, and honor `prefers-reduced-motion`.
- **Mobile:** the panel goes full-width at about 80vh with a sticky composer, and must not collide with the cookie banner (see section 6).
- **Copy:** plain text with no emojis (the chatbot's own rule). Use real `—` and `'` characters, and run the site's mojibake check before committing.

---

## 5. Contract mapping: chatbot API ↔ UI

Chatbot backend (FastAPI, parent folder): `POST /api/chat`, `POST /api/upload`, `GET /api/health`,
`GET /api/queue/status`.

`/api/chat` response fields and how the UI uses each:

| Field | UI behavior |
|---|---|
| `messages[]` `{text, kind: normal/system/safety}` | Render a bubble per message; `kind` picks the bubble style |
| `quick_replies[]` | Chips under the last bot bubble |
| `allow_upload` | Show the attach button and photo drop zone |
| `await_step` | Pick the special renderer: `issue_type` gets the icon cards, `urgency` the slider, `damage_pointer` the map |
| `account_address`, `latitude`, `longitude` | Pre-center the damage-pointer map |
| `state` (`collect/confirm/correct/done/safety/queued/queue_full/human`) | `confirm` gets the summary card, `done` the success state, `queued` the queue state |
| `queued`, `queue_position` | Queue UI, polling `GET /api/queue/status` |
| `done`, `email_id` | Lock the composer and show the success card |

### Field alignment with today's form (`useFormSubmit("service_request")`)

| Site form | Chatbot | Gap / decision |
|---|---|---|
| `issueType` solar/electrical/roof/misc | `issue_type` with the same 4 values | ✅ Identical |
| firstName + lastName | `name` (single field) | Split only if the downstream email needs it |
| email + phone | `contact` (either) | Chatbot accepts one; the form collects both |
| address + city/state/zip | `account_address` (single line) | The chatbot also asks for the account name, which the form doesn't |
| urgency **0–10** | urgency **1–10** | Align the scales |
| description (min 20) | description (verbatim, min 2 words) | Fine |
| contactTime | Not collected | Add or drop |
| leakCoords `"lat,lng"` | `damage_pointer` (map image upload) | Different representation |
| photos (TODO, disabled) | `/api/upload`, fully implemented | ✅ The chatbot fills a real gap |

---

## 6. Integration constraints and gotchas

1. **Tawk.to rule.** The site's CLAUDE.md says "Tawk.to is the only chat widget. Do not add a second one." An **in-page** assistant (not a floating launcher) avoids a second widget, but the Tawk bubble will still float bottom-right on this page. Decide whether to hide it on this route.
2. **Global wheel handler.** `App.tsx` L59-71 calls `preventDefault` and scrolls the *window* on wheel events of 30px or more, which will hijack scrolling inside the message list. The chat scroll container needs `onWheel={e => e.stopPropagation()}`, or the handler must skip an opt-out attribute.
3. **Cookie banner.** It's `fixed bottom-0 z-50`, full width. On mobile it will cover the composer until dismissed.
4. **No `<Toaster/>` is mounted.** `sonner` toasts from `useFormSubmit` are currently invisible site-wide. Don't rely on toasts; use inline messages.
5. **No dev proxy, and no `/api` in nginx.** For local dev, add `server.proxy['/api'] → http://localhost:8000` in `vite.config.ts`. For production, either add an nginx `location /api/ { proxy_pass … ; client_max_body_size 10m; }` before `location /`, or call the FastAPI origin directly.
6. **CORS.** The chatbot's `CORS_ALLOW_ORIGINS` defaults to localhost only. Production needs `https://zeoenergy.com` (plus the Cloud Run URL) if it's called cross-origin.
7. **Env vars.** `VITE_*` values are baked in at build time from the committed `.env.production`. There are no build args in cloudbuild, so a `VITE_CHAT_API_URL` goes there (it's public, not a secret).
8. **Routing mailboxes don't match.** The live site routes service requests to `service@`, `homedamage@`, and a named individual via `keywordRouting.ts`, with confirmations from `service@`. The chatbot's config uses placeholder mailboxes (`service-team@`, `intake-review@`, `performance-team@`).
9. **Privacy page.** `PrivacyPage.tsx` says chat data goes to Tawk.to. A first-party AI assistant that collects PII and photos needs a privacy-page update.
10. **Pre-commit rules (site CLAUDE.md).** Run `npx tsc --noEmit` (only the `baseUrl` deprecation warning is expected) and `grep -rn "â" src/` for mojibake. There is no test or lint suite.
11. **Pre-existing bug.** ServicePage sends `leakCoords` as `"lat,lng"` but `emailTemplates.ts` `buildService` calls `JSON.parse` on it, so the Maps link is never built. This is out of scope, but worth fixing separately.
