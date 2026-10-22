"""common.pii — find structured personal data in text, with no model.

Deterministic, stdlib-only recognisers for the PII that has a SHAPE: a
regular expression finds the candidate, and a checksum or a context word
decides whether it counts. Sub-millisecond on a chat message, so a caller
can run it on every request and still have an answer when nothing else
does — route-policy's ``pii`` evaluator runs it in-process on every turn, so
it still forces ``local-private`` while the ``signals`` sidecar (whose NER
model adds free-form addresses, names and ids without a fixed format) is
down or slow.

What it recognises (each a rule in :data:`RULES`):

* **email** — plain addresses, and obfuscated ones (``jenna dot price at
  gmail dot com``, ``amanda.lee(at)protonmail.com``, ``arroba`` / ``punto``);
* **phone** — North-American numbers with separators (``(801) 555-0147``,
  ``385-555-0199``, ``+1 480 555 0133``), the same as ten bare digits or with
  spaces only when a phone word is near (``call me 8015550147``), and
  ``+``-prefixed international numbers (8-15 digits);
* **street_address** — US addresses (house number, direction, name, a
  street suffix: ``1482 W Cedar Hollow Dr``), the Utah grid (``77 N 400 E``),
  PO boxes, and Latin-American streets (``Calle Reforma 222``);
* **gov_id** — dashed SSNs and ITINs inside the SSA / IRS ranges, a bare or
  spaced SSN after its word (``social is 412 55 9087``), Mexican CURPs, and
  a passport / licence / ITIN / EIN / tax id after its word (``Passport
  number X12345678``, ``Tax ID 47-1234567``);
* **card_number** — 13-19 digits, contiguous or in card groups, with an
  issuer prefix and a valid Luhn check;
* **bank_account** — IBANs (country length + mod 97), and numbers after a
  banking word (``routing``, ``ABA``, ``ACH``, ``IBAN``, ``bank account``),
  or after ``account`` when they pass the ABA routing checksum;
* **account_number** — any other 6-20 digit number after ``account`` /
  ``acct`` / ``cuenta`` / ``loan number`` / ``member id`` / ``policy no``;
* **date_of_birth** — a valid date right after ``DOB`` / ``date of birth``
  / ``born (on)`` / ``fecha de nacimiento``. A date with no such word is
  never a date of birth.

``person`` and ``location`` are in the vocabulary but never produced here:
names and places have no shape, only the sidecar's NER finds them.

**Precision over recall.** A ruleset forces ``local-private`` on these
types, so a pattern that fires on an order number, a build version, a
product code, a balance or an example in a regex question costs a route.
Numbers that only look like data — a 13-digit millisecond timestamp, a
Luhn-valid id with no card prefix, ``555 times 147``, ``port 8080``, a
phone-shaped number right after ``order`` / ``SKU`` / ``serial`` — are left
alone, and so are bare names, cities and states. What has no shape
(``unit 4B at the Lofts on 300 South``, spelled-out digits) is left to the
NER. Known false positives are documented in the tests (a fictional
``4 Privet Drive``, an example phone number inside a regex question).

**Spans never leave the process.** :func:`find` returns offsets so a caller
can count, merge with another detector's findings, or redact; what a caller
reports or stores is :func:`counts` (types and numbers only).

Stdlib only, pure, never raises on any input (``find(None) == []``; a
recogniser that raises on a text loses its own matches there and nothing
else). Letter classes a lookup is keyed by are ASCII-only: ``re.I`` also
folds ``ſ`` / ``K`` / ``ı`` / ``İ`` onto ASCII letters.
Fast and linear: on one laptop core a 200-character message takes ~20 µs,
2,000 characters ~0.2 ms, 100 KB of prose ~6 ms and 100 KB of dense
contact rows ~16 ms (``tests/test_pii.py`` holds 100 KB under 20 ms, and
pathological inputs — ``dot dot dot ...``, a wall of colons after ``SSN`` —
under a second; how the scan avoids running every pattern over the whole
text is in :mod:`.recognizers`). Consumers: route-policy's ``pii``
evaluator (``ai/route-policy``); the ``signals`` sidecar (``ai/signals``)
maps its NER labels onto the same vocabulary.

Public API:
    find(text)    -> [PiiMatch(type, start, end, rule), ...] in text order, non-overlapping
    counts(text)  -> {type: n} for the types found, in ENTITY_TYPES order
    PiiMatch      — frozen dataclass: type, start, end, rule
    ENTITY_TYPES  — the shared vocabulary (email, phone, street_address, gov_id,
                    card_number, bank_account, account_number, date_of_birth,
                    person, location, other)
    SENSITIVE_TYPES — the types that force local-private on their own (the
                    first seven)
    PATTERN_TYPES — the types the patterns can produce (the first eight)
    RULES         — rule name -> type, in the order rules claim overlapping text
    luhn_valid, card_iin_valid, iban_valid, aba_valid, ssn_valid, itin_valid
                  — the validators, over digit strings
"""

from .recognizers import (
    ENTITY_TYPES,
    PATTERN_TYPES,
    RULES,
    SENSITIVE_TYPES,
    PiiMatch,
    counts,
    find,
)
from .validators import aba_valid, card_iin_valid, iban_valid, itin_valid, luhn_valid, ssn_valid

__all__ = [
    "ENTITY_TYPES",
    "PATTERN_TYPES",
    "RULES",
    "SENSITIVE_TYPES",
    "PiiMatch",
    "aba_valid",
    "card_iin_valid",
    "counts",
    "find",
    "iban_valid",
    "itin_valid",
    "luhn_valid",
    "ssn_valid",
]
