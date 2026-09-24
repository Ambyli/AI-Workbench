# Zeo Energy Service Chatbot — Integration Templates & Mock Architecture

## Overview
This document specifies the integration templates for **Google Workspace (Gmail)** and **Salesforce / In-House CRM (Chatter Feeds)** generated automatically by the Zeo Energy Service Chatbot disposition pipeline.

Visual diagrams and interactive mock layouts are maintained separately as internal design collateral and are not tracked in this repository.

---

## 1. Gmail Handoff Email Template (`backend/templates/handoff_email.html.j2`)

The Gmail handoff email is delivered to team disposition mailboxes (`service-team@zeoenergy.com`, `nonstandard@zeoenergy.com`, `performance-team@zeoenergy.com`, `intake-review@zeoenergy.com`).

### Visual Elements:
1. **Urgency & Priority Banner**:
   - High Priority (`#c0262b` / Urgency 8-10 / Red border)
   - Elevated (`#e08a1e` / Urgency 5-7 / Amber border)
   - Standard (`#3a8d5b` / Urgency 1-4 / Green border)
2. **Account Verification Table**:
   - Displays matched account number, verified service address on file, solar system kW size, and install date.
3. **Issue Diagnostic Breakdown**:
   - Dynamic key-value pairs per category (Roof, Electrical, Solar, Miscellaneous).
4. **Satellite Damage Pointer Map Card**:
   - Embedded aerial view showing customer-dropped pinpoint location.
5. **Photo Attachment Cards**:
   - Realistic Gmail attachment pills with thumbnail preview, filename, and EXIF-stripped indicator.
6. **AI Summary & Verbatim Statements**:
   - Highlighting key facts with clear untrusted input demarcations.

---

## 2. Salesforce / In-House CRM Chatter Note Template (`backend/templates/chatter_note.html.j2`)

The Chatter Note is posted directly to the Case Feed upon case creation in the CRM.

### Structure:
```
+---------------------------------------------------------------+
| Zeo Service Bot (Automated Intake)                            |
| Case #CASE-MOCK-001 · Account #ZEO-MOCK-001 · Auto-Dispatched |
+---------------------------------------------------------------+
| SERVICE INTAKE CASE NOTE                                      |
| Category: Roof Damage | Priority Tier: HIGH PRIORITY (8/10)   |
| Summary: Active roof leak reported near chimney.              |
+---------------------------------------------------------------+
| INTAKE DETAILS:                                               |
| - Account Number: ZEO-MOCK-001                                |
| - Customer Name:  Jane Doe                                    |
| - Service Address: 100 Solar Way, Tampa, FL 33601             |
| - Disposition Routing: service-team@zeoenergy.com             |
+---------------------------------------------------------------+
| CUSTOMER STATEMENT (Untrusted Verbatim):                      |
| "Water is dripping into the upstairs hallway..."              |
+---------------------------------------------------------------+
| ATTACHED PHOTOS & DAMAGE LOCATION:                            |
| [ Damage Location Pointer ]   [ roof_chimney_1.jpg ]          |
+---------------------------------------------------------------+
| Notice: Generated automatically. Images signature-validated.  |
+---------------------------------------------------------------+
```

---

## 3. Security & File Processing Lifecycle
1. **Header Magic-Byte Signature Check**: File headers are inspected to verify real image formats (PNG, JPEG, WEBP). Executable scripts masked with image extensions are rejected immediately.
2. **5MB File Ceiling**: Files above 5MB are refused.
3. **Pillow EXIF / GPS Scrubbing**: Metadata is stripped before writing to disk.
4. **Sandboxed HTML Rendering**: Jinja2 autoescape is enabled for all dynamic variables, neutralizing script injection or breakout attempts.
