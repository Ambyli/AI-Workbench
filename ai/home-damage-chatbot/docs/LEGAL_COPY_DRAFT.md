# Draft copy for legal review: Service assistant (W-05, W-06)

**Status: DRAFT, not applied to the website.** Removing Tawk.to and adding the Service assistant make parts of the cookie banner and Privacy Policy inaccurate. Below are proposed replacements, file by file, for legal to approve or edit. Once approved they are a few line edits on the `service-chatbot` branch.

Facts the copy relies on (verify before approving):
- **What the assistant collects:**
  - the name and address on the Solar Account
  - a phone number or email
  - the issue details the customer types
  - an optional map pin and photos
- **Model processing:** the customer's answers, one message at a time, are sent to ZEO's **own AI model server** (`api.zeoenergy.com`, run by ZEO) to read each answer. They are not sent to an outside AI company.
- **Account check:** the account match runs in ZEO's internal systems. The AI model is never shown account records.
- **Third parties:**
  - the satellite map and photo of the pin come from **Esri** (map imagery)
  - the address search on the map uses **OpenStreetMap Nominatim**; the production geocoder vendor is still undecided (S-M6), so name the final vendor here
- **Retention:**
  - photos and staff handoff emails held by the assistant: 30 days (`RETENTION_DAYS`)
  - the request itself: in the CRM per the existing 3-year policy
  - the chat transcript: not kept after the request is submitted; the conversation is discarded from memory
- **Incomplete requests:** if a customer stops partway (20 minutes idle) or asks for a person, the answers given so far are emailed to the service team so they can follow up.
- **Uploads:** photo location data (EXIF/GPS) is removed on upload.

## 1. `src/app/components/layout/CookieConsent.tsx`

Line 38 (consent-required variant). Current:
> Submitting a quote request and using live chat require your consent to process and follow up on your inquiry.

Proposed:
> Submitting a quote or service request requires your consent to process and follow up on your inquiry.

Line 39 (default variant). Current:
> We use cookies for site analytics (Google Analytics) and live chat. Analytics data is processed by Google. No PII is collected until you submit a form.

Proposed:
> We use cookies for site analytics (Google Analytics). Analytics data is processed by Google. We only collect personal information you choose to give us, for example in a form or our service assistant.

**Decision for legal:** does using the service assistant require cookie consent? The recommendation is no: it works like a form and sets no cookies. It keeps the conversation in the browser's session storage until the tab is closed.

## 2. `src/app/pages/legal/PrivacyPage.tsx`

**Section 2, "Communications" bullet.** Current:
> • Communications — messages sent via contact or service forms, chat conversations

Proposed:
> • Communications — messages sent via contact or service forms, and answers you give our service assistant (including the name and address on your solar account, your contact details, a description of the issue, an optional map pin of the damage location, and optional photos)

**Section 2, automatically collected.** Delete:
> • Chat transcripts — if you use the Tawk.to live chat widget

**Section 4.** Replace the "Tawk.to Live Chat" block with:
> Service Assistant (no cookies)
> Purpose: Helps you submit a service request by asking questions step by step. Your answers are read by an automated assistant running on ZEO Energy's own systems, and the completed request is sent to our service team. If you stop partway, or ask to speak with a person, the answers you've given are sent to our service team so they can follow up with you.
> Data sent to: ZEO Energy only. Map imagery for the damage-location step is loaded from Esri, and address searches on the map are sent to [geocoding provider]. Location data is removed from uploaded photos.
> Retention: See Section [retention].

**Section 5 (sharing), "Analytics and chat" paragraph.** Current:
> Analytics and chat — As described in Section 4, analytics data is processed by Google and chat data is processed by Tawk.to.

Proposed:
> Analytics and mapping — As described in Section 4, analytics data is processed by Google; map imagery is provided by Esri and map address searches by [geocoding provider].

**Retention section.** Replace:
> Chat transcripts: Governed by Tawk.to retention settings.

with:
> Service assistant: Your answers are kept only until your request is sent to our service team; requests are then retained like other service requests. Photos and copies of submitted requests held by the assistant are deleted after 30 days.

**Section 3.** The sentence "We do not use your information to make solely automated decisions that significantly affect you without human review" stays true: the assistant routes the request, and every request is reviewed by staff. Legal should confirm.

## 3. `CLAUDE.md` (site repo, developer notes)

Line 152, "Tawk.to is the only chat widget. Do not add a second one." Proposed:
> The Service page uses the first-party Service Assistant (`components/service-chat/`). Tawk.to is disabled (kept in `TawkToLoader.tsx` for a one-line rollback). Do not add a second chat widget.
