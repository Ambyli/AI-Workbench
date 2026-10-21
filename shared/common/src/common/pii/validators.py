"""Checksums and number rules that keep the PII recognisers precise.

A sixteen-digit run is a card number about one time in ten by chance (the
Luhn check) and far less often once the issuer prefix has to fit too; an
IBAN's two check digits make a random string pass one time in 97, and its
country fixes its length; an ABA routing number carries a weighted check
digit; an SSN has ranges the SSA never issues. Every recogniser in
:mod:`common.pii.recognizers` that matches a bare number runs it through one
of these before it counts, which is what lets ``find`` match number shapes
without context words and still not report an order id or a timestamp.

Pure functions over digit strings (or, for the IBAN, the compact
alphanumeric form). Nothing here raises on malformed input: a string that
is not the right shape is simply not valid.
"""

from __future__ import annotations

from typing import Mapping


def _digits_only(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdigit()


def luhn_valid(digits: str) -> bool:
    """The Luhn (mod 10) check every payment card number carries."""
    if not _digits_only(digits):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def card_iin_valid(digits: str) -> bool:
    """Does the number start with a prefix a card network issues, at a length
    that network uses? Visa (4, 13/16/19), Mastercard (51-55 and
    2221-2720, 16), American Express (34/37, 15), Diners Club (300-305, 36,
    38, 39; 14-19), JCB (3528-3589, 16-19), Discover (6011, 644-649, 65;
    16-19) and UnionPay (62, 16-19). A Luhn-valid run with any other prefix
    is far more likely an id than a card."""
    if not _digits_only(digits):
        return False
    n = len(digits)
    if digits[0] == "4":
        return n in (13, 16, 19)
    two, four = int(digits[:2]), int(digits[:4]) if n >= 4 else -1
    if 51 <= two <= 55 or 2221 <= four <= 2720:
        return n == 16
    if two in (34, 37):
        return n == 15
    if 300 <= int(digits[:3]) <= 305 or two in (36, 38, 39):
        return 14 <= n <= 19
    if 3528 <= four <= 3589:
        return 16 <= n <= 19
    if four == 6011 or 644 <= int(digits[:3]) <= 649 or two in (62, 65):
        return 16 <= n <= 19
    return False


# IBAN lengths by country (the SWIFT IBAN registry). A country not listed is
# not matched at all: guessing its length would let mod-97 pass one string
# in 97 at every length tried.
IBAN_LENGTHS: Mapping[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24,
    "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SC": 31,
    "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23, "TN": 24,
    "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}


def iban_valid(compact: str) -> bool:
    """The ISO 13616 check on an IBAN written without spaces: a known
    country, that country's length, and mod 97 == 1 over the rearranged
    number (letters as 10..35)."""
    if not isinstance(compact, str) or not compact.isascii():
        return False
    s = compact.upper()
    if IBAN_LENGTHS.get(s[:2]) != len(s) or not s[2:4].isdigit() or not s.isalnum():
        return False
    remainder = 0
    for ch in s[4:] + s[:4]:
        value = ord(ch) - 48 if ch.isdigit() else ord(ch) - 55
        remainder = (remainder * (100 if value > 9 else 10) + value) % 97
    return remainder == 1


def aba_valid(digits: str) -> bool:
    """An ABA routing transit number: nine digits, a Federal Reserve prefix
    (00-12, 21-32, 61-72, or 80), and the 3-7-1 weighted check digit."""
    if not _digits_only(digits) or len(digits) != 9:
        return False
    prefix = int(digits[:2])
    if not (prefix <= 12 or 21 <= prefix <= 32 or 61 <= prefix <= 72 or prefix == 80):
        return False
    return sum((ord(c) - 48) * w for c, w in zip(digits, (3, 7, 1) * 3)) % 10 == 0


def ssn_valid(area: str, group: str, serial: str) -> bool:
    """The parts of a US Social Security number as the SSA issues them: area
    never 000, 666 or 9xx, group never 00, serial never 0000."""
    if not (_digits_only(area) and _digits_only(group) and _digits_only(serial)):
        return False
    if (len(area), len(group), len(serial)) != (3, 2, 4):
        return False
    return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"


def itin_valid(area: str, group: str, serial: str) -> bool:
    """An IRS Individual Taxpayer Identification Number: SSN-shaped, area
    9xx, group 50-65, 70-88, 90-92 or 94-99."""
    if not (_digits_only(area) and _digits_only(group) and _digits_only(serial)):
        return False
    if (len(area), len(group), len(serial)) != (3, 2, 4) or area[0] != "9":
        return False
    g = int(group)
    return 50 <= g <= 65 or 70 <= g <= 88 or 90 <= g <= 92 or 94 <= g <= 99
