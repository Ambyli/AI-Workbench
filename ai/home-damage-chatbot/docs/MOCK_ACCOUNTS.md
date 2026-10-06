# Mock accounts: account-check cheat sheet

Start the mock with `powershell -ExecutionPolicy Bypass -File scripts\mock-start.ps1`, then open http://localhost:8080.

Synthetic accounts for testing the account check on the mock Service page. Every name and street is invented; the solar-themed street names do not exist. Cities and ZIP codes are real so the parsing and scoring behave realistically.

- Data: [`backend/fixtures/mock_projects.json`](../backend/fixtures/mock_projects.json), used only when `CRM_BACKEND=stub` (the default).
- Checked by `tests/mock_accounts_test.py`. Every row below is run through the matcher and through `/api/chat` on each test run, so this sheet cannot drift from the code.

## How to test

1. Start the chat. The first question is the name on the Solar Account: type the **Account name** column.
2. For the Solar Account address, type the **Address** column exactly.
3. The next message is an info card with the result:
   - **Found:** "We found your account. Please continue with the rest of the form."
   - **Not found:** "We were unable to locate your account. Please continue filling out the form, and someone from our team will locate your account before reaching out."

The account check is **gapped**: the chatbot only learns found / not found. The project id, candidates and scores stay in the separate account service, which appends them to the staff handoff at submit. The customer never sees the project id. To see which project matched, or which close candidates staff get when nothing matched, finish the request and run `scripts\mock-inbox.ps1`. The mock page has the staff API off, like production.

## Scenarios

| ID | What it tests | Account name | Address | Result | Project |
|---|---|---|---|---|---|
| S01 | Clean match | `Jane Doe` | `100 Solar Way, Tampa, FL 33601` | Found | 900001 |
| S02 | Typos in street, suffix and city | `Marcus Bell` | `318 Kilowat Drive, Clearwatr FL 33755` | Found | 900002 |
| S03 | No commas, no ZIP, lower-case name | `marcus bell` | `318 Kilowatt Dr Clearwater FL` | Found | 900002 |
| S04 | One person of a couple on the account | `Maria Alvarez` | `42 Sunbeam Ln, Orlando, FL 32801` | Found | 900003 |
| S05 | Trust account, customer gives own name | `Robert Hensley` | `77 Photon Ct, Sarasota, FL 34236` | Not found | none |
| S06 | Trust account, customer gives the trust name | `Hensley Family Trust` | `77 Photon Ct, Sarasota, FL 34236` | Found | 900004 |
| S07 | Two projects on one home (re-install) | `Priya Raman` | `1500 Inverter Blvd, St. Petersburg, FL 33701` | Found | 900005 |
| S08 | Same name and street, Tampa home | `Michael Johnson` | `25 Array Ave, Tampa, FL 33602` | Found | 900007 |
| S09 | Same name and street, Lakeland home | `Michael Johnson` | `25 Array Ave, Lakeland, FL 33801` | Found | 900008 |
| S10 | Right house number, wrong street | `Laura Chen` | `300 Meadow St, Tampa, FL 33603` | Not found | none |
| S11 | Right street, wrong house number | `Laura Chen` | `302 Voltage St, Tampa, FL 33603` | Not found | none |
| S12 | Unit number typed as Apt | `Devon Price` | `88 Battery Blvd Apt 4B, Tampa, FL 33606` | Found | 900010 |
| S13 | Service address stored on street line 2 | `Grace Kim` | `910 Solstice Ave, Brandon, FL 33511` | Found | 900011 |
| S14 | Directional spelled out | `Omar Haddad` | `720 South Photon Way, Lehi, UT 84043` | Found | 900012 |
| S15 | Two homes too close to call (no ZIP) | `Chris Taylor` | `12 Panel Ct, Tampa, FL` | Not found | none |
| S16 | Nickname for the account name | `Bob Smith` | `59 Wattage Way, Tampa, FL 33607` | Not found | none |
| S17 | Full account name | `Robert Smith` | `59 Wattage Way, Tampa, FL 33607` | Found | 900015 |
| S18 | Real address, wrong person | `Nobody Atall` | `100 Solar Way, Tampa, FL 33601` | Not found | none |

The "Not found" rows are deliberate. They show what the gates refuse (wrong street, wrong house number, ambiguous homes) and the known gaps that fall to staff (trusts under a personal name, nicknames).

## Notes

- **One check per chat.** To try another scenario, use **Start over**. Correcting details at the summary step does not re-run the search.
- **Rate limit.** One browser gets 60 messages a minute. Running many scenarios back to back can briefly trigger "One moment...".
- **Map step.** These streets don't exist, so the damage map can't center on the typed address. Use the map's address search or pan to any house; the pin location is independent of the account check.
- **Adding accounts.** Add a row to `projects` with a new `9000xx` id, and a row to `scenarios` if it tests something new. Then run `python tests/mock_accounts_test.py` and add the row to the table above. Never put real customer data in the fixture.
