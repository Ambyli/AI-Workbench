"""common.pii — the stdlib PII recognisers route-policy forces local-private on.

Every name, address and number below is invented (ROUTE_POLICY_PHASE5_PLAN
§ 2.1's fabricated set, extended). The tests lean on PRECISION as much as on
recall: a recogniser that fires on an order number, a build version or a
date that is not a birth date costs a route, so every rule has negatives
next to its positives, and the known misses and false positives are pinned
so a change to them shows up.

    UV_LINK_MODE=copy uv run --no-sync pytest shared/common/tests/test_pii.py -q
"""

from __future__ import annotations

import time

import pytest

from common.pii import (
    ENTITY_TYPES,
    PATTERN_TYPES,
    RULES,
    SENSITIVE_TYPES,
    PiiMatch,
    aba_valid,
    card_iin_valid,
    counts,
    find,
    iban_valid,
    itin_valid,
    luhn_valid,
    ssn_valid,
)


def spans(text):
    return [(m.type, text[m.start:m.end]) for m in find(text)]


def sensitive(text):
    return {t for t in counts(text) if t in SENSITIVE_TYPES}


# ── the vocabulary ─────────────────────────────────────────────────────────────
def test_the_vocabulary_is_the_shared_one():
    assert ENTITY_TYPES == ("email", "phone", "street_address", "gov_id", "card_number", "bank_account",
                            "account_number", "date_of_birth", "person", "location", "other")
    assert SENSITIVE_TYPES == ("email", "phone", "street_address", "gov_id", "card_number", "bank_account",
                               "account_number")
    assert PATTERN_TYPES == ENTITY_TYPES[:8]
    # Every rule produces a pattern type, and every pattern type has a rule;
    # person / location / other come from NER only.
    assert set(RULES.values()) == set(PATTERN_TYPES)
    assert not {"person", "location", "other"} & set(RULES.values())


def test_pii_match_is_a_frozen_value():
    m = PiiMatch("email", 0, 3, "email")
    with pytest.raises(AttributeError):
        m.type = "phone"  # type: ignore[misc]
    assert m == PiiMatch("email", 0, 3, "email")


# ── the validators ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("digits,ok", [
    ("4111111111111111", True), ("4242424242424242", True), ("378282246310005", True),
    ("4111111111111112", False), ("", False), ("4111-1111", False), ("١٢٣٤", False),
])
def test_luhn(digits, ok):
    assert luhn_valid(digits) is ok


@pytest.mark.parametrize("digits,ok", [
    ("4111111111111111", True),        # Visa, 16
    ("4222222222222", True),           # Visa, 13
    ("5555555555554444", True),        # Mastercard 51-55
    ("2223003122003222", True),        # Mastercard 2-series
    ("378282246310005", True),         # Amex, 15
    ("30569309025904", True),          # Diners, 14
    ("6011111111111117", True),        # Discover
    ("3530111333300000", True),        # JCB
    ("1729425600000", False),          # a millisecond timestamp
    ("7111111111111111", False),       # no network issues 7
    ("411111111111111", False),        # Visa at 15
    ("37828224631000", False),         # Amex at 14
])
def test_card_iin(digits, ok):
    assert card_iin_valid(digits) is ok


@pytest.mark.parametrize("iban,ok", [
    ("DE89370400440532013000", True),
    ("GB82WEST12345698765432", True),
    ("de89370400440532013000", True),          # case-insensitive inside the validator
    ("DE89370400440532013001", False),         # check digits
    ("DE8937040044053201300", False),          # Germany is 22 long
    ("ZZ89370400440532013000", False),         # not a registry country
    ("", False), ("DE89 3704 0044", False),
])
def test_iban(iban, ok):
    assert iban_valid(iban) is ok


@pytest.mark.parametrize("digits,ok", [
    ("124003116", True), ("021000021", True), ("011000015", True),
    ("123456789", False),       # the check digit
    ("994003116", False),       # no Federal Reserve prefix 99
    ("12400311", False), ("", False),
])
def test_aba(digits, ok):
    assert aba_valid(digits) is ok


@pytest.mark.parametrize("parts,ssn,itin", [
    (("523", "45", "6789"), True, False),
    (("000", "45", "6789"), False, False),
    (("666", "45", "6789"), False, False),
    (("923", "45", "6789"), False, False),
    (("523", "00", "6789"), False, False),
    (("523", "45", "0000"), False, False),
    (("912", "70", "1234"), False, True),
    (("912", "89", "1234"), False, False),
    (("912", "93", "1234"), False, False),
])
def test_ssn_and_itin(parts, ssn, itin):
    assert ssn_valid(*parts) is ssn
    assert itin_valid(*parts) is itin


# ── every recogniser, with the exact span it claims ────────────────────────────
@pytest.mark.parametrize("text,expected", [
    # email, plain and obfuscated
    ("write to sarah.k.olsen@gmail.com today", [("email", "sarah.k.olsen@gmail.com")]),
    ("email me: john.smith+solar@company.co", [("email", "john.smith+solar@company.co")]),
    ("Write to jenna dot price at gmail dot com", [("email", "jenna dot price at gmail dot com")]),
    ("Amanda Lee - amanda.lee(at)protonmail.com - wants a quote", [("email", "amanda.lee(at)protonmail.com")]),
    ("jenna [at] gmail [dot] com", [("email", "jenna [at] gmail [dot] com")]),
    ("escribe a carlos arroba gmail punto com", [("email", "carlos arroba gmail punto com")]),
    # phones
    ("Call back on (801) 555-0147 tomorrow", [("phone", "(801) 555-0147")]),
    ("the customer's number is 385-555-0199", [("phone", "385-555-0199")]),
    ("Reach me at 801.555.0147", [("phone", "801.555.0147")]),
    ("Write a text for +1 480 555 0133", [("phone", "+1 480 555 0133")]),
    ("contact: 1-801-555-0147", [("phone", "1-801-555-0147")]),
    ("my cell is 8015550147", [("phone", "8015550147")]),
    ("llámame al 801 555 0147", [("phone", "801 555 0147")]),
    ("her number is +44 20 7946 0958", [("phone", "+44 20 7946 0958")]),
    ("Mexican number +52 55 1234 5678", [("phone", "+52 55 1234 5678")]),
    # street addresses
    ("Draft a note to 1482 W Cedar Hollow Dr, Lehi, UT 84043", [("street_address", "1482 W Cedar Hollow Dr")]),
    ("send it to 913 s main st please", [("street_address", "913 s main st")]),
    ("1482 w cedar hollow dr", [("street_address", "1482 w cedar hollow dr")]),
    ("4521 Sunset Ridge Road, Boise", [("street_address", "4521 Sunset Ridge Road")]),
    ("the office at 123 3rd Ave in the Avenues", [("street_address", "123 3rd Ave")]),
    ("deliver to 123B Main Street", [("street_address", "123B Main Street")]),
    ("Paul's place is at 11 Willow Bend, Highland", [("street_address", "11 Willow Bend")]),
    ("Brian Taylor, 77 N 400 E, Provo UT", [("street_address", "77 N 400 E")]),
    ("Ship it to 1234 South 700 East, Salt Lake City", [("street_address", "1234 South 700 East")]),
    ("PO Box 381, Alpine UT 84004", [("street_address", "PO Box 381")]),
    ("mail to P.O. Box 1234, Lehi", [("street_address", "P.O. Box 1234")]),
    ("Envía la factura a Calle Reforma 222, Col. Juárez", [("street_address", "Calle Reforma 222")]),
    ("Dirección: Calle 50 # 23-45, Medellín", [("street_address", "Calle 50 # 23")]),
    ("Avenida Insurgentes Sur 1602, CDMX", [("street_address", "Avenida Insurgentes Sur 1602")]),
    # government ids
    ("My SSN is 523-45-6789, fill in the form", [("gov_id", "523-45-6789")]),
    ("social is 412 55 9087 for the loan", [("gov_id", "412 55 9087")]),
    ("ssn 523456789", [("gov_id", "523456789")]),
    ("ITIN 912-70-1234", [("gov_id", "912-70-1234")]),
    ("Passport number X12345678 expires", [("gov_id", "X12345678")]),
    ("Her passport: K 0412 9981, issued in Canada", [("gov_id", "K 0412 9981")]),
    ("Driver's license UT 123456789 was rejected", [("gov_id", "UT 123456789")]),
    ("license number D1234-56789-01234", [("gov_id", "D1234-56789-01234")]),
    ("Tax ID 47-1234567 for the client", [("gov_id", "47-1234567")]),
    ("EIN 12-3456789", [("gov_id", "12-3456789")]),
    ("CURP GOMC790412HDFRRR05", [("gov_id", "GOMC790412HDFRRR05")]),
    # cards, banks, accounts
    ("charge card 4111 1111 1111 1111 exp 09/28", [("card_number", "4111 1111 1111 1111")]),
    ("pay to 4111111111111111", [("card_number", "4111111111111111")]),
    ("Card: 3782 822463 10005", [("card_number", "3782 822463 10005")]),
    ("Use IBAN DE89 3704 0044 0532 0130 00 for it.", [("bank_account", "DE89 3704 0044 0532 0130 00")]),
    ("IBAN GB82 WEST 1234 5698 7654 32", [("bank_account", "GB82 WEST 1234 5698 7654 32")]),
    ("DE89370400440532013000", [("bank_account", "DE89370400440532013000")]),
    ("routing number 124003116", [("bank_account", "124003116")]),
    ("Bank routing 124003116 account 000123456789",
     [("bank_account", "124003116"), ("account_number", "000123456789")]),
    ("Pay the supplier by ACH: 021000021 / 4471882209",
     [("bank_account", "021000021"), ("bank_account", "4471882209")]),
    ("Their account number is 77812-33901", [("account_number", "77812-33901")]),
    ("Account #: 00123456789", [("account_number", "00123456789")]),
    ("acct 4471882209", [("account_number", "4471882209")]),
    ("member id 77120345 for the insurance", [("account_number", "77120345")]),
    ("Mi cuenta es 1234567890", [("account_number", "1234567890")]),
    # dates of birth
    ("Customer Dana Brooks, DOB 04/12/1979", [("date_of_birth", "04/12/1979")]),
    ("date of birth: March 3, 1979", [("date_of_birth", "March 3, 1979")]),
    ("fecha de nacimiento: 12/04/1979", [("date_of_birth", "12/04/1979")]),
    ("born on 1979-04-12", [("date_of_birth", "1979-04-12")]),
    ("nació el 3 de marzo de 1979", [("date_of_birth", "3 de marzo de 1979")]),
])
def test_recognisers_find_the_exact_span(text, expected):
    assert spans(text) == expected


def test_each_rule_is_exercised():
    rules = set()
    for text in ("a.b@c.io", "x(at)y.com", "4111111111111111", "DE89370400440532013000", "523-45-6789",
                 "912-70-1234", "GOMC790412HDFRRR05", "ssn 523456789", "passport X12345678",
                 "DOB 04/12/1979", "801-555-0147", "+44 20 7946 0958", "routing 124003116", "acct 1234567",
                 "cell 8015550147", "PO Box 1", "77 N 400 E", "Calle Reforma 222", "12 Oak St"):
        rules.update(m.rule for m in find(text))
    assert rules == set(RULES)


# ── precision: what must NOT count ─────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    # numbers that are data, not people
    "The build failed at commit 4f3a2b1c9d, version 2.14.0, port 8080",
    "What is 555 times 147?",
    "set timeout to 30000 ms and retries to 3 in the config",
    "the API returns user_id 88213 and order_id 551092, why is the order missing?",
    "timestamp 1729425600000 in the log",
    "uuid 550e8400-e29b-41d4-a716-446655440000",
    "the total was 1,234,567 dollars",
    "ip 192.168.100.200 and 10.0.0.12, version 10.2.3.4567",
    "ISBN 978-3-16-148410-0",
    "pi is 3.14159265358979",
    "Track UPS 1Z999AA10123456784",
    "Error code 0x80070005 at offset 1234567",
    "1 2 3 4 5 6 7 8 9 10 11 12 13 14 15",
    "4111,1111,1111,1111",
    "assert luhn('4111111111111112') is False",
    "docker run -p 8080:80 nginx:1.25.3",
    "SELECT * FROM users WHERE id = 5551234567",
    "2024-10-20T12:34:56Z started, 2024-10-20T12:35:01Z done",
    # phone-shaped product and order numbers
    "Order #801-555-0147 shipped yesterday",
    "SKU 300-450-1234 is out of stock",
    "Serial number 412-555-0199 is on the inverter label",
    "the order id is 5551234567",
    # dates that are not a date of birth, amounts that are not accounts
    "Our meeting is on 04/12/2025",
    "The birthday party is on 06/14/2025",
    "I was born in 1979 in Provo",
    "the account balance is 1234567",
    "Our account has 1500000 followers",
    "the loan amount is 250000",
    "my account was created on 2024-10-20",
    "invoice 2024-10-20-0042 is overdue",
    # words without data
    "What is the format of a US social security number?",
    "How do credit card numbers use the Luhn checksum?",
    "How do I renew my passport?",
    "driver's license renewal costs $52",
    "Apache License 2.0 applies",
    "The SSN format is 000-00-0000",
    "Write SQL to select users where phone is null",
    "Call Mike about the 401k meeting at 3pm",
    # names, places, prose with numbers
    "my name is Sam, can you help me plan a birthday party?",
    "Hi, I'm Jessica. What's a good recipe for banana bread?",
    "check the weather in Utah",
    "Who is Elon Musk and why is Tesla's stock down?",
    "Can you recommend restaurants on Main Street in Park City?",
    "How many solar panels do I need for a 2,000 sq ft house in Salt Lake City?",
    "convert 1482 kWh per year to monthly average",
    "We have 3 cats on the way",
    "it is a 2 lane road",
    "5 miles down the road",
    "We drove 300 miles on the main road",
    "I ate 2 Big Macs at St. George",
    "the 2019 Main Street festival",
    "the 1st Ave location is closed",
    # emails that are not mailboxes
    "Write a function that validates an email address with a regex",
    "Visit us at zeoenergy dot com",
    "look at google.com for it",
    "git clone git@github.com:org/repo.git",
    "use @decorator in Python",
    # Spanish
    "la calle está cerrada",
    "Necesito ayuda con mi cuenta de correo",
    "Tengo 3 hijos y 2 perros",
])
def test_negatives_find_nothing_sensitive(text):
    assert sensitive(text) == set(), spans(text)


# ── the force decision on the plan's fabricated set (§ 2.1) ────────────────────
FORCE = [
    "Draft an email to John Martinez at 1482 W Cedar Hollow Dr, Lehi, UT 84043 about his install date.",
    "Can you write a follow-up to sarah.k.olsen@gmail.com thanking her for the referral?",
    "Call back Maria Gonzalez on (801) 555-0147 tomorrow, write me a script.",
    "the customer's number is 385-555-0199, summarize what I should tell him",
    "Send the proposal to 2210 E Desert Willow Ln, Gilbert AZ 85296",
    "Here are the notes: Brian Taylor, 77 N 400 E, Provo UT, wants 12 panels.",
    "Please reformat this contact: Emily Chen, emily.chen@outlook.com, 208-555-0110",
    "Reply to kevin_ross77@yahoo.com and say the inspection moved to Monday",
    "Translate into Spanish: Dear Mr. Ramirez, your appointment at 913 S Main St is confirmed.",
    "Write a text for +1 480 555 0133 reminding them about the site survey",
    "Email from jdoe@zeo-customer.net: why is my bill higher this month? draft a reply",
    "Mrs. Patel lives at 4521 Sunset Ridge Rd, Boise ID 83702, how far is that from our Meridian office?",
    "My SSN is 523-45-6789, can you fill in this form for me?",
    "social is 412 55 9087 for the loan application, put it in the right field",
    "Passport number X12345678 expires next month, what do I need to renew?",
    "Driver's license UT 123456789 was rejected by the portal, why?",
    "Customer Dana Brooks, DOB 04/12/1979, license number D1234-56789-01234",
    "charge card 4111 1111 1111 1111 exp 09/28 cvv 123 for the deposit",
    "Bank routing 124003116 account 000123456789, set up autopay",
    "Their account number is 77812-33901, look up why the payment failed",
    "Use IBAN DE89 3704 0044 0532 0130 00 for the supplier transfer.",
    "Card ending 4242 was declined for Tom Weller at 18 Oak St, Orem — what now?",
    "Summarize this ticket: 'Hi, this is Laura Kim, 1900 N Canyon Rd Provo, my inverter shows error 34 since Tuesday.'",
    "I need a polite late-payment notice for Greg Hansen, account 55120994, balance $412.",
    "Lisa Moreno (lisa.moreno@icloud.com) asked about the federal tax credit, write a reply",
    "Make a table of these leads: Jake Fox 801-555-0123, Amy Lund 801-555-0178",
    "Write to jenna dot price at gmail dot com and tell her the permit came through",
    "Envía la factura a Carlos Ruiz, Calle Reforma 222, Col. Juárez, Ciudad de México",
    "Paul Jensen's place is the blue house at 11 Willow Bend, Highland -- add him to the route",
    "Pay the supplier by ACH: 021000021 / 4471882209",
    "Her passport: K 0412 9981, issued in Canada",
    "Tax ID 47-1234567 for the commercial client, which form do they need?",
    "Amanda Lee - amanda.lee(at)protonmail.com - wants a quote",
    "The homeowner, Steve Park, PO Box 381, Alpine UT 84004, asked for a copy of the contract.",
]
# What the patterns cannot see (the sidecar's NER is for these). Pinned so a
# change shows up.
KNOWN_MISSES = [
    "Customer is Rachel Nguyen, she's in unit 4B at the Lofts on 300 South in Salt Lake.",
    "mi número es ocho cero uno, cinco cinco cinco, cero uno cuatro siete, llámame",
]
KNOWN_FALSE_POSITIVES = [
    "In the novel, Harry Potter lives at 4 Privet Drive. Why does that matter to the plot?",
    "Write a regex for phone numbers like (801) 555-0123",
]


@pytest.mark.parametrize("text", FORCE)
def test_forces_on_sensitive_data(text):
    assert sensitive(text), text


@pytest.mark.parametrize("text", KNOWN_MISSES)
def test_known_misses(text):
    assert sensitive(text) == set()


@pytest.mark.parametrize("text", KNOWN_FALSE_POSITIVES)
def test_known_false_positives(text):
    assert sensitive(text)


def test_date_of_birth_alone_is_not_sensitive_and_needs_its_word():
    assert counts("DOB 04/12/1979") == {"date_of_birth": 1}
    assert sensitive("DOB 04/12/1979") == set()
    for text in ("the install date is 04/12/1979", "DOB 13/13/1979", "born on 1979", "born in March 1979"):
        assert "date_of_birth" not in counts(text), text


def test_a_bare_name_is_never_found():
    assert find("my name is Sam") == [] and find("Sam and Alex are coming over") == []


# ── one entity per piece of text ───────────────────────────────────────────────
def test_overlaps_are_claimed_once_in_rule_order():
    # The card is not also a phone or an account number.
    assert spans("account 4111111111111111") == [("card_number", "4111111111111111")]
    # A dashed SSN after its word is counted once (rule "ssn", not "ssn_context").
    assert [m.rule for m in find("SSN: 523-45-6789")] == ["ssn"]
    # A phone after "account" stays a phone.
    assert spans("account 801-555-0147") == [("phone", "801-555-0147")]


def test_find_is_sorted_and_non_overlapping():
    text = ("Laura Kim, 1900 N Canyon Rd, Provo UT 84604, phone 801-555-0144, laura.kim@example.com, "
            "acct 55120994, card 4111 1111 1111 1111, 77 N 400 E, SSN 523-45-6789. ") * 5
    found = find(text)
    assert found == sorted(found, key=lambda m: m.start)
    assert all(a.end <= b.start for a, b in zip(found, found[1:]))
    assert counts(text) == {"email": 5, "phone": 5, "street_address": 10, "gov_id": 5, "card_number": 5,
                            "account_number": 5}
    assert list(counts(text)) == [t for t in ENTITY_TYPES if t in counts(text)]


# ── never raises ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("junk", [None, 42, b"523-45-6789", ["523-45-6789"], {"t": 1}, "", " " * 10])
def test_junk_is_empty(junk):
    assert find(junk) == [] and counts(junk) == {}


def test_odd_unicode():
    # A lone surrogate, a right-to-left mark, full-width digits: no crash, no
    # entity from digits that are not ASCII.
    assert counts("\ud800 SSN ‏５２３４５６７８９") == {}
    # "İ" lower-cases to two characters: triggers are then found by regex and
    # every span still points at the original text.
    text = "İİİ ssn 523456789 and DOB 04/12/1979 İ"
    assert spans(text) == [("gov_id", "523456789"), ("date_of_birth", "04/12/1979")]


# re.I folds these four onto ASCII letters ("ſ" onto s, "K" onto k, "ı" / "İ"
# onto i), in literals and in [A-Za-z] alike. "77 N 400 ſ" used to raise
# KeyError('ſ') from the grid's axis lookup -- a character of the user's text,
# which then disabled route-policy's pii force for the whole chat.
CASE_FOLDS = ("İ", "ı", "ſ", "K")
_DIRECTIONS = ("N", "S", "E", "W", "n", "s", "North", "South", "East", "West", "north", "south")
_SUFFIX_WORDS = ("St", "Rd", "Ave", "Blvd", "Dr", "Ln", "Ct", "Cir", "Pl", "Pkwy", "Hwy", "Ter", "Trl", "Cv", "Sq",
                 "Street", "Road", "Avenue", "Boulevard", "Drive", "Lane", "Way", "Court", "Circle", "Place",
                 "Parkway", "Highway", "Terrace", "Trail", "Loop", "Cove", "Bend", "Plaza", "Row", "Crescent",
                 "Square", "st", "dr")


def _fold_variants(word):
    """``word`` with each of its letters, one at a time, replaced by each case-fold character."""
    return [word[:i] + ch + word[i + 1:] for i in range(len(word)) for ch in CASE_FOLDS]


def test_case_folded_letters_never_raise_in_a_grid_address():
    for good in ("N", "S", "E", "W", "North", "South", "East", "West"):
        for bad in _fold_variants(good):
            for text in (f"77 N 400 {bad}", f"77 S 400 {bad}", f"77 {bad} 400 E", f"77 {bad} 400 W",
                         f"Brian Taylor, 77 N 400 {bad}, Provo UT", f"1234 {bad} 700 East"):
                found = find(text)
                # A folded letter is not a direction: no grid address from it.
                assert all(m.rule != "street_grid" for m in found), (text, found)
    assert spans("Brian Taylor, 77 N 400 E, Provo UT") == [("street_address", "77 N 400 E")]
    assert spans("1234 South 700 East") == [("street_address", "1234 South 700 East")]
    # Case still does not matter for ASCII, and \s is still Unicode (a no-break space).
    assert spans("77 n 400 e") == [("street_address", "77 n 400 e")]
    assert spans("77 NORTH 400 eAsT") == [("street_address", "77 NORTH 400 eAsT")]
    assert spans("77 N 400 E") == [("street_address", "77 N 400 E")]


def test_case_folded_letters_never_raise_in_a_street_address():
    pairs = [(d, s) for s in _SUFFIX_WORDS for d in ("W", "south")]
    pairs += [(bad, s) for s in ("Dr", "Street") for d in _DIRECTIONS for bad in _fold_variants(d)]
    pairs += [(d, bad) for d in ("W", "s", "North") for s in _SUFFIX_WORDS for bad in _fold_variants(s)]
    for direction, suffix in pairs:
        find(f"1482 {direction} Cedar Hollow {suffix}, Lehi")
        find(f"send it to 913 {direction} main {suffix} please")
        counts(f"77 {direction} 400 {direction} and 18 Oak {suffix}")
    assert spans("1482 W Cedar Hollow Dr") == [("street_address", "1482 W Cedar Hollow Dr")]
    assert spans("1482 W Cedar Hollow Dr") == [("street_address", "1482 W Cedar Hollow Dr")]


def test_case_folded_letters_never_raise_anywhere():
    # Every context word and value shape, with the fold characters dropped in.
    for ch in CASE_FOLDS:
        for text in (f"{ch}sn 523456789", f"s{ch}n 523-45-6789", f"pa{ch}{ch}port X12345678", f"DOB {ch}04/12/1979",
                     f"date of birth: Augu{ch}t 3, 1979", f"accoun{ch} 4471882209", f"routing {ch}124003116",
                     f"jenna dot price at gmail dot com{ch}", f"a{ch}b(at)y{ch}.com", f"PO Bo{ch} 381",
                     f"Calle Refo{ch}ma 222", f"Ta{ch} ID 47-1234567", f"cell{ch} 8015550147", f"DE89{ch}3704",
                     f"{ch}" * 50 + " 77 N 400 " + ch, f"12{ch} Oak St", f"1-{ch}801-555-0147"):
            find(text)


class _Boom(Exception):
    pass


def test_a_recogniser_that_raises_costs_only_its_own_matches(monkeypatch):
    from common.pii import recognizers

    def broken(t):
        raise _Boom("a message that quotes the text")

    def half(t):
        # Finds one candidate, then fails: what it found before the error stays.
        yield "po_box", 0, 3
        raise _Boom("boom")

    text = "Laura Kim, laura.kim@example.com, 801-555-0144, 1900 N Canyon Rd"
    whole = spans(text)
    assert [t for t, _ in whole] == ["email", "phone", "street_address"]
    table = list(recognizers._RECOGNISERS)
    cluster = [i for i, (fn, _) in enumerate(table) if fn is recognizers._cluster_numbers][0]
    table[cluster] = (broken, True)
    monkeypatch.setattr(recognizers, "_RECOGNISERS", tuple(table))
    assert spans(text) == [w for w in whole if w[0] != "phone"]
    table[cluster] = (half, True)
    monkeypatch.setattr(recognizers, "_RECOGNISERS", tuple(table))
    assert spans(text)[0] == ("street_address", "Lau")
    assert [t for t, _ in spans(text)[1:]] == ["email", "street_address"]
    # A recogniser that is not even a generator, or yields a rule nobody knows.
    table[cluster] = (lambda t: iter([("no-such-rule", 0, 3)]), True)
    monkeypatch.setattr(recognizers, "_RECOGNISERS", tuple(table))
    assert spans(text) == [w for w in whole if w[0] != "phone"]


# ── speed ──────────────────────────────────────────────────────────────────────
# A chat-like mix: prose with numbers, a tool row with contact data.
_PARAGRAPH = (
    "We installed 412 residential systems last quarter across Utah, Idaho and Arizona, up from 355. "
    "Average size went from 8.1 kW to 8.6 kW because the new panel line is 430 W instead of 400 W. "
    "Cancellations were 9 percent, down from 12. The biggest delays were permitting in two counties "
    "and a two-week shortage of one inverter model; please summarise it for the leadership team in "
    "plain language and lead with the three numbers that matter most. "
    "project 18821 | owner: Laura Kim | 1900 N Canyon Rd, Provo UT 84604 | phone 801-555-0144 | 8.6 kW\n")


def _best_ms(text, runs=5):
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        find(text)
        best = min(best, (time.perf_counter() - start) * 1000.0)
    return best


def test_100kb_scans_in_under_20ms():
    text = (_PARAGRAPH * (100_000 // len(_PARAGRAPH) + 1))[:100_000]
    assert counts(text)["phone"] > 100
    assert _best_ms(text) < 20.0


ADVERSARIAL = {
    "dots": " dot" * 25_000, "at-brackets": "a(at)" * 20_000, "dashes": "1-" * 50_000,
    "ssn-words": "ssn " * 25_000 + "523", "account-words": "account " * 12_000 + "1", "plus-ones": "+1" * 50_000,
    "spaced-digits": "9 " * 50_000, "grid-ish": "1 N " * 25_000, "calles": "Calle " * 16_000,
    "dobs": "DOB " * 25_000, "long-local": "a" * 100_000 + "@", "colons": "ssn" + ":" * 100_000,
    "at-words": "x at " * 20_000 + "y dot com", "iban-ish": "AB12 " * 20_000, "parens": "(" * 100_000,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_adversarial_input_stays_linear(name):
    # Patterns that could backtrack catastrophically (a run of "dot dot dot",
    # colons after a context word) are written so they cannot; this bounds a
    # 100 KB worst case well below a second.
    assert _best_ms(ADVERSARIAL[name], runs=1) < 1000.0
