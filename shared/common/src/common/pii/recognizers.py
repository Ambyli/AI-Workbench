"""The PII recognisers: regular expressions, the context they need, the
order they claim text in, and how a long text is scanned fast. See the
package docstring for what each one finds.

**Three kinds of rule.**

* *Shape + checksum, no context* -- emails, card numbers (Luhn + issuer
  prefix), IBANs (country length + mod 97), dashed SSNs and ITINs (SSA /
  IRS ranges), CURPs, North-American phone numbers written with separators,
  ``+`` international numbers, street addresses.
* *Context + value* -- a bare or spaced SSN, a passport / licence / ITIN /
  EIN / tax id, a bank or account number, a date of birth, a PO box, a
  Latin-American street: the value counts only right after its word
  (``SSN 523456789``, ``account number is 77812-33901``, ``DOB
  04/12/1979``, ``Calle Reforma 222``), with nothing but filler (``number``,
  ``no.``, ``is``, ``#``, ``:``) between.
* *Value + nearby context* -- ten bare digits (or three groups split by
  spaces only) are a phone number when a phone word (``call``, ``cell``,
  ``my number`` ...) is within a few words.

**Claim order.** Every recogniser yields candidates ``(rule, start, end)``;
:func:`find` keeps them in :data:`RULES` order -- a more specific, validated
rule first -- and drops any candidate that overlaps a span already kept. So
one piece of text is one entity: a card number is never also a phone
number, a dashed SSN is not counted again by the SSN-with-context rule, and
an account number already claimed as a phone is not counted twice.

**Scanning.** A regular expression run over a whole 100 KB text costs
CPython about a millisecond even when it matches nothing, so running each
rule's pattern over the text would cost tens of milliseconds. Instead the
text is read twice -- once for its digit runs (``[0-9]+``) and once,
lower-cased, for the rules' trigger words (``str.find``, at C speed) -- and
each pattern is then tried only where it can start: a street pattern at a
house number followed by a space, a card / phone / SSN pattern over a
cluster of digit runs holding at least eight digits, a context pattern at
its word, an email around its ``@``. The patterns themselves are written so
they cannot backtrack catastrophically (every repetition consumes its text
one way only), and the windows they run in are bounded.
"""

from __future__ import annotations

import re
from bisect import bisect_left, insort
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Pattern, Sequence, Tuple

from .validators import (
    IBAN_LENGTHS,
    aba_valid,
    card_iin_valid,
    iban_valid,
    itin_valid,
    luhn_valid,
    ssn_valid,
)

# The entity vocabulary, shared with the signals sidecar's NER labels
# (ROUTE_POLICY_PHASE5_PLAN § 3.1). Patterns produce the first eight; person
# and location come from NER only.
ENTITY_TYPES: Tuple[str, ...] = (
    "email", "phone", "street_address", "gov_id", "card_number", "bank_account",
    "account_number", "date_of_birth", "person", "location", "other",
)
# What forces local-private on its own (the user's list, 2026-10-20).
SENSITIVE_TYPES: Tuple[str, ...] = ENTITY_TYPES[:7]
# What this module's patterns can produce.
PATTERN_TYPES: Tuple[str, ...] = ENTITY_TYPES[:8]

# rule -> entity type, in CLAIM order: an earlier rule's span wins an overlap.
RULES: Mapping[str, str] = {
    "email": "email",
    "email_obfuscated": "email",
    "card": "card_number",
    "iban": "bank_account",
    "ssn": "gov_id",
    "itin": "gov_id",
    "curp": "gov_id",
    "ssn_context": "gov_id",
    "gov_id_context": "gov_id",
    "dob_context": "date_of_birth",
    "phone": "phone",
    "phone_intl": "phone",
    "bank_context": "bank_account",
    "account_context": "account_number",
    "phone_context": "phone",
    "po_box": "street_address",
    "street_grid": "street_address",
    "street_latam": "street_address",
    "street_us": "street_address",
}
_PRIORITY = {rule: i for i, rule in enumerate(RULES)}


@dataclass(frozen=True)
class PiiMatch:
    """One entity found in a text: its vocabulary ``type``, the span
    ``text[start:end]`` and the ``rule`` (a key of :data:`RULES`) that found
    it. Spans exist so a caller can count, merge or redact; they are never
    meant to leave the process that scanned the text."""

    type: str
    start: int
    end: int
    rule: str


Candidate = Tuple[str, int, int]


# ── the text being scanned ───────────────────────────────────────────────────
_RUN = re.compile(r"[0-9]+")
_NON_DIGIT = re.compile(r"[^0-9]")
# Characters that may separate the digit runs of one number ("(801) 555-0147",
# "4111 1111 1111 1111", "+1 480"), at most two of them between two runs.
_CLUSTER_GAP = " ().-/+"
_CLUSTER_MIN_DIGITS = 8


@lru_cache(maxsize=None)
def _trigger_regex(triggers: Tuple[str, ...]) -> Pattern[str]:
    return re.compile("|".join(re.escape(t) for t in triggers), re.I)


class _Text:
    """One text, with what several recognisers share: its digit runs, the
    clusters they form, and where trigger words occur."""

    __slots__ = ("text", "n", "_lower", "runs")

    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)
        self.runs: List[Tuple[int, int]] = [m.span() for m in _RUN.finditer(text)]
        lower = text.lower()
        # A handful of characters ("İ") lower-case to two; then offsets in
        # the lowered copy no longer match, and triggers are found by regex.
        self._lower: Optional[str] = lower if len(lower) == self.n else None

    def positions(self, triggers: Tuple[str, ...]) -> List[int]:
        """Sorted start offsets of every (case-insensitive) occurrence of any
        of ``triggers`` -- lower-case literals."""
        found = set()
        lower = self._lower
        if lower is None:
            found.update(m.start() for m in _trigger_regex(triggers).finditer(self.text))
        else:
            for trigger in triggers:
                i = lower.find(trigger)
                while i != -1:
                    found.add(i)
                    i = lower.find(trigger, i + 1)
        return sorted(found)

    def clusters(self) -> Iterator[Tuple[int, int, int, int]]:
        """(start, end, digits, longest run) of each stretch of digit runs
        joined by at most two separator characters, holding at least eight
        digits: where a card, a phone number or an SSN can be."""
        text, runs = self.text, self.runs
        i = 0
        while i < len(runs):
            start, end = runs[i]
            digits = longest = end - start
            j = i + 1
            while j < len(runs):
                nxt = runs[j][0]
                if nxt - end > 2 or text[end:nxt].strip(_CLUSTER_GAP):
                    break
                end = runs[j][1]
                digits += end - nxt
                longest = max(longest, end - nxt)
                j += 1
            if digits >= _CLUSTER_MIN_DIGITS:
                yield start, end, digits, longest
            i = j


def _at_triggers(t: _Text, triggers: Tuple[str, ...], pattern: Pattern[str]) -> Iterator["re.Match[str]"]:
    """``pattern`` tried at every occurrence of ``triggers`` (it must start
    with the trigger word)."""
    text = t.text
    for pos in t.positions(triggers):
        m = pattern.match(text, pos)
        if m is not None:
            yield m


def _ndigits(value: str) -> int:
    return len(_NON_DIGIT.sub("", value))


# Filler allowed between a context word and its value: "number", "no.",
# "num", "#", "is", ":", "of" and their Spanish twins. One separator
# character or one whole word per repetition, so it cannot backtrack.
_FILL = (r"(?:[\s:#=.]|\b(?:number|numbers|num|nbr|no|nos|id|is|was|of|n[uú]mero|es|de)\b)"
         r"{0,16}")
_DATE_SHAPES = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}|[0-9]{1,2}-[0-9]{1,2}-[0-9]{2,4}")
# A number right after one of these words is an id, not a phone or an
# account: "order 801-555-0147", "SKU 300-450-1234".
_ID_BEFORE = re.compile(
    r"\b(?:order|invoice|sku|part|model|serial|ticket|case|ref|reference|tracking|confirmation|"
    r"item|version|build|isbn|patent|project|job|po|issue|pr|commit)\s*(?:number|no\.?|num|#|id)?"
    r"\s*[:#=]?\s*$", re.I)


def _id_before(text: str, start: int) -> bool:
    return _ID_BEFORE.search(text, max(0, start - 30), start) is not None


# ── email ────────────────────────────────────────────────────────────────────
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,24}(?![A-Za-z0-9-])")


def _emails(t: _Text) -> Iterator[Candidate]:
    text, n = t.text, t.n
    at = text.find("@")
    covered = -1
    while at != -1:
        if at >= covered:
            for m in _EMAIL.finditer(text, max(0, at - 64), min(n, at + 256)):
                if m.start() > at:
                    break
                if m.end() <= at:
                    continue
                end = m.end()
                covered = end
                # git@github.com:org/repo -- an SSH remote, not a mailbox.
                if not (end + 1 < n and text[end] == ":" and not text[end + 1].isspace()):
                    yield "email", m.start(), end
                break
        at = text.find("@", at + 1)


# "jenna dot price at gmail dot com", "amanda.lee(at)protonmail.com",
# "carlos arroba gmail punto com". A bracketed at ("(at)", "[at]", " @ ")
# is obfuscation on its own; a plain " at " counts only when the domain's
# dot is spelled out too -- "look at google.com" is prose.
_AT_STRONG = r"(?:\s*(?:\(at\)|\[at\]|\{at\}|<at>|\(arroba\))\s*|\s+@\s+)"
_AT_WORD = r"\s+(?:at|arroba)\s+"
_DOT_WORD = r"(?:\s*(?:\(dot\)|\[dot\]|\{dot\}|<dot>|\(punto\))\s*|\s+(?:dot|punto)\s+)"
_DOT_ANY = r"(?:" + _DOT_WORD + r"|\.)"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_TLD = r"(?:com|net|org|edu|gov|mil|io|co|us|mx|ca|uk|info|biz|me|app|dev|ai)"
# The local part and the domain take at most four spelled-out dots each, so
# a run of "dot dot dot" costs a bounded amount of backtracking per start.
_EMAIL_OBF = re.compile(
    r"(?<![A-Za-z0-9._%+-])(?P<local>[A-Za-z0-9][A-Za-z0-9._%+-]*(?:" + _DOT_WORD
    + r"[A-Za-z0-9][A-Za-z0-9_%+-]*){0,4})"
    r"(?:" + _AT_STRONG + _LABEL + r"(?:" + _DOT_ANY + _LABEL + r"){0,4}?" + _DOT_ANY + _TLD
    + r"|" + _AT_WORD + _LABEL + r"(?:" + _DOT_ANY + _LABEL + r"){0,4}?" + _DOT_WORD + _TLD + r")"
    r"(?![A-Za-z0-9-])", re.I)
_OBF_TRIGGERS = ("dot", "punto", "(at)", "[at]", "{at}", "<at>", "arroba")
_OBF_WINDOW = 120
# A plain " at " after one of these is prose ("contact us at zeo dot com").
_OBF_LOCAL_STOP = frozenset((
    "us", "me", "him", "her", "them", "you", "it", "this", "that", "look", "visit", "here", "there",
    "online", "home", "site", "website", "page", "sign", "log", "find", "check", "go", "see", "found",
    "located", "written", "hosted", "live", "posted", "listed", "available",
))


def _emails_obfuscated(t: _Text) -> Iterator[Candidate]:
    text, n = t.text, t.n
    triggers = t.positions(_OBF_TRIGGERS)
    # " @ " -- an @ between spaces; a plain address's @ is _emails' business.
    at = text.find("@")
    while at != -1:
        if 0 < at < n - 1 and text[at - 1].isspace() and text[at + 1].isspace():
            insort(triggers, at)
        at = text.find("@", at + 1)
    # One pass over each stretch of text within reach of a trigger (every
    # match holds one, by the pattern's construction).
    lo = hi = -1
    for pos in triggers + [None]:
        if pos is not None and pos - _OBF_WINDOW <= hi:
            hi = min(n, pos + _OBF_WINDOW)
            continue
        if hi > lo:
            for m in _EMAIL_OBF.finditer(text, lo, hi):
                if m.group("local").lower() not in _OBF_LOCAL_STOP:
                    yield "email_obfuscated", m.start(), m.end()
        if pos is not None:
            lo, hi = max(0, pos - _OBF_WINDOW), min(n, pos + _OBF_WINDOW)


# ── numbers in a cluster: cards, SSNs, phones ────────────────────────────────
_CARD = re.compile(
    r"(?<![0-9])(?:[0-9]{13,19}"
    r"|[0-9]{4}(?P<s>[ -])[0-9]{4}(?P=s)[0-9]{4}(?P=s)[0-9]{1,4}(?:(?P=s)[0-9]{3})?"
    r"|[0-9]{4}(?P<t>[ -])[0-9]{6}(?P=t)[0-9]{4,5})(?![0-9])")
_SSN_DASHED = re.compile(r"(?<![0-9-])([0-9]{3})-([0-9]{2})-([0-9]{4})(?![0-9]|-[0-9])")
# North-American numbers: area and exchange codes start 2-9 and are never
# N11 (211, 911 ... are service codes). With separators they count on their
# own -- unless an id word sits right before them; as ten bare digits, or
# three groups split by spaces only, they need a phone word nearby.
_NANP_AREA = r"[2-9](?!11)[0-9]{2}"
_NANP_EXCH = r"[2-9](?!11)[0-9]{2}"
_PHONE = re.compile(
    r"(?<![0-9A-Za-z+.\-])(?:"
    r"(?:\+?1[ .-]?)?\(" + _NANP_AREA + r"\)[ .-]?" + _NANP_EXCH + r"[ .-][0-9]{4}"
    r"|(?:\+?1[.-])?" + _NANP_AREA + r"(?P<s>[.-])" + _NANP_EXCH + r"(?P=s)[0-9]{4}"
    r"|(?:\+?1[ ])?" + _NANP_AREA + r" " + _NANP_EXCH + r"-[0-9]{4}"
    r"|\+1[ .-]?" + _NANP_AREA + r"[ .-]?" + _NANP_EXCH + r"[ .-]?[0-9]{4}"
    r")(?![0-9]|-[0-9])")
_PHONE_BARE = re.compile(
    r"(?<![0-9A-Za-z+.\-])(?:1[ .-]?)?" + _NANP_AREA + r"(?P<s>[ ]?)" + _NANP_EXCH + r"(?P=s)[0-9]{4}(?![0-9])")
_PHONE_WORDS = re.compile(
    r"\b(?:phone|phones|cell|cellphone|mobile|call|calls|calling|text|txt|tel|telephone|fax|whatsapp|sms|"
    r"reach|contact|dial|n[uú]mero|tel[eé]fono|celular|m[oó]vil|ll[aá]ma(?:me|r|nos)?|marca(?:r)?|"
    r"(?:my|his|her|their|your|our|the|a)\s+number)\b", re.I)
# "+44 20 7946 0958", "+52 55 1234 5678", "+49 (0)30 123456": one way to
# read every character, so no backtracking; 8-15 digits, as in E.164.
_PHONE_INTL = re.compile(
    r"(?<![0-9A-Za-z+])\+[2-9](?:[0-9]|[ .-](?=[0-9(])|\((?=[0-9])|\)(?=[ .-]?[0-9]))*(?<=[0-9])(?![0-9])")


def _cluster_numbers(t: _Text) -> Iterator[Candidate]:
    text, n = t.text, t.n
    for start, end, digits, longest in t.clusters():
        lo, hi = max(0, start - 4), min(n, end + 3)
        stretch = text[lo:end]
        # Each pattern only where its shape can be: a card needs 13 digits,
        # a dashed SSN a dash, an international number a "+", and ten bare
        # digits a 10-digit run or groups split by spaces.
        if digits >= 13:
            for m in _CARD.finditer(text, lo, hi):
                card = _NON_DIGIT.sub("", m.group(0))
                if card_iin_valid(card) and luhn_valid(card):
                    yield "card", m.start(), m.end()
        if "-" in stretch:
            for m in _SSN_DASHED.finditer(text, lo, hi):
                if ssn_valid(*m.groups()):
                    yield "ssn", m.start(), m.end()
                elif itin_valid(*m.groups()):
                    yield "itin", m.start(), m.end()
        if digits >= 10:
            for m in _PHONE.finditer(text, lo, hi):
                if not _id_before(text, m.start()):
                    yield "phone", m.start(), m.end()
        if "+" in stretch:
            for m in _PHONE_INTL.finditer(text, lo, hi):
                if 8 <= _ndigits(m.group(0)) <= 15 and not _id_before(text, m.start()):
                    yield "phone_intl", m.start(), m.end()
        if longest >= 10 or (digits >= 10 and " " in stretch):
            for m in _PHONE_BARE.finditer(text, lo, hi):
                s, e = m.span()
                if (_PHONE_WORDS.search(text, max(0, s - 30), min(n, e + 20)) is not None
                        and not _id_before(text, s)):
                    yield "phone_context", s, e


# ── numbers at a digit run: IBANs, CURPs, street addresses ───────────────────
_CURP = re.compile(
    r"(?<![A-Za-z0-9])[A-Z][AEIOUX][A-Z]{2}[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[HMX]"
    r"(?:AS|B[CS]|C[CLMSH]|D[FG]|G[TR]|HG|JC|M[CNS]|N[ETL]|OC|PL|Q[TR]|S[PLR]|T[CSL]|VZ|YN|ZS|NE)"
    r"[B-DF-HJ-NP-TV-Z]{3}[0-9A-Z][0-9](?![A-Za-z0-9])")


def _iban_at(text: str, start: int) -> int:
    """End of a valid IBAN starting at ``start`` (country letters), else -1.
    Its characters are read one by one up to the country's length, allowing
    single spaces between groups, so an IBAN followed by an upper-case word
    is still found."""
    if start > 0 and text[start - 1].isascii() and text[start - 1].isalnum():
        return -1
    need = IBAN_LENGTHS.get(text[start:start + 2])
    if need is None:
        return -1
    chars, i, n = [text[start:start + 4]], start + 4, len(text)
    have = 4
    while have < need and i < n:
        ch = text[i]
        if ch == " " and i + 1 < n and text[i + 1] != " ":
            i += 1
            continue
        if not (ch.isascii() and (ch.isdigit() or ch.isupper())):
            break
        chars.append(ch)
        have += 1
        i += 1
    if have != need or (i < n and text[i].isascii() and text[i].isalnum()):
        return -1
    return i if iban_valid("".join(chars)) else -1


def _ascii_ci(word: str) -> str:
    """``word`` in any ASCII case and nothing else, for use inside ``(?-i:...)``:
    ``re.I`` alone also folds four non-ASCII letters onto ASCII ones -- "ſ"
    (U+017F) onto s, "K" (U+212A) onto k, "ı" / "İ" (U+0131 / U+0130) onto i
    -- in literals and in classes such as ``[A-Za-z]`` alike."""
    return "".join("[%s%s]" % (c.upper(), c) for c in word)


# The Utah (Salt Lake grid) form: "77 N 400 E", "1234 South 700 East". The
# two directions must be on different axes, which is a lookup keyed by the
# matched letter -- so the directions are ASCII-only (not "ſouth"), and the
# lookup is a .get() all the same. The \s stays Unicode: a no-break space
# after the house number is still a space.
_DIRECTION = r"(?-i:[NSEWnsew]|" + "|".join(_ascii_ci(w) for w in ("north", "south", "east", "west")) + r")"
_GRID = re.compile(
    r"(?<![0-9A-Za-z$#.,/:-])[0-9]{1,5}\s+(?P<d1>" + _DIRECTION + r")\.?\s+[0-9]{1,5}\s+(?P<d2>"
    + _DIRECTION + r")(?![A-Za-z0-9])\.?", re.I)
_AXIS = {"n": "ns", "s": "ns", "e": "ew", "w": "ew"}


def _grid_ok(m: "re.Match[str]") -> bool:
    axis1 = _AXIS.get(m.group("d1")[:1].lower())
    axis2 = _AXIS.get(m.group("d2")[:1].lower())
    return axis1 is not None and axis2 is not None and axis1 != axis2

# US style: a house number, an optional direction, one to four name words,
# a street suffix. Abbreviated suffixes (St, Rd, Ave, Dr, Ln ...) count in
# any case; spelled-out ones (Street, Way, Court, Drive ...) only
# capitalised, after a capitalised name -- "a 2 lane road" and "3 cats on
# the way" are prose.
_SUFFIX_ABBR = frozenset((
    "st", "str", "rd", "ave", "av", "blvd", "dr", "ln", "ct", "cir", "pl", "pkwy", "hwy", "ter", "trl",
    "cv", "cres", "sq", "expy", "fwy",
))
_SUFFIX_FULL = frozenset((
    "street", "road", "avenue", "boulevard", "drive", "lane", "way", "court", "circle", "place", "parkway",
    "highway", "terrace", "trail", "loop", "cove", "bend", "plaza", "row", "crescent", "square",
))
_SUFFIXES = _SUFFIX_ABBR | _SUFFIX_FULL
_STREET = re.compile(
    r"(?<![0-9A-Za-z$#.,/:+-])(?P<num>[0-9]{1,6}[A-Za-z]?)\s+"
    r"(?:(?P<dir>[NSEW]|north|south|east|west|NE|NW|SE|SW)\.?\s+)?"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9'.-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'.-]*){0,3}?)\s+"
    r"(?P<suf>" + "|".join(sorted(_SUFFIXES, key=len, reverse=True)) + r")"
    r"(?![A-Za-z0-9'-])\.?", re.I)
# Name words that never belong to a street name: articles, prepositions,
# pronouns, units -- "3 cats on the way", "5 miles down the road".
_STREET_STOP = frozenset((
    "the", "a", "an", "of", "to", "on", "in", "at", "for", "and", "or", "per", "my", "your", "his", "her",
    "our", "their", "its", "it", "by", "from", "with", "into", "onto", "is", "are", "was", "were", "be",
    "this", "that", "these", "those", "down", "up", "over", "under", "than", "each", "every", "all", "some",
    "any", "no", "not", "times", "x", "more", "less", "about", "around", "out", "off", "back", "away",
    "miles", "mile", "mi", "km", "ft", "feet", "foot", "sq", "kwh", "kw", "w", "mph", "lbs", "kg", "hours",
    "hrs", "hour", "minutes", "min", "days", "day", "weeks", "years", "year", "months", "people",
    "users", "panels", "items", "steps", "ways", "you", "we", "they", "he", "she", "i", "me", "us",
    "them", "if", "so", "then", "when", "via", "like", "just", "only", "also", "very", "new",
))
# What the street and grid patterns' lookbehinds refuse before a house
# number, checked first so the patterns are not even tried ("8.1 kW", "$5").
_NOT_BEFORE_HOUSE_NUMBER = frozenset("0123456789$#.,/:+-") | frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
# A four-digit "house number" right after one of these is a year.
_YEAR_BEFORE = re.compile(r"\b(?:the|in|of|since|during|until|by|from|before|after)\s+$", re.I)


def _street_ok(m: "re.Match[str]", text: str) -> bool:
    words = m.group("name").split()
    num = m.group("num")
    if len(num) == 4 and num[:2] in ("19", "20") and _YEAR_BEFORE.search(text, max(0, m.start() - 12),
                                                                          m.start()):
        return False  # "the 2019 Main Street festival", "in 2024 Park Avenue ..."
    if any(w.lower().strip(".'-") in _STREET_STOP for w in words):
        return False
    suffix = m.group("suf")
    if suffix.lower() in _SUFFIX_FULL:
        return suffix[0].isupper() and (words[0][0].isupper() or words[0][0].isdigit())
    # Abbreviated, after a lowercase multi-word name: only when the suffix is
    # lowercase too ("1482 w cedar hollow dr", typed in one case) --
    # "2 patients saw Dr" is a title, not a street.
    if len(words) > 1 and all(w[0].islower() for w in words):
        return suffix[0].islower()
    return True


def _run_numbers(t: _Text) -> Iterator[Candidate]:
    text, n = t.text, t.n
    for start, end in t.runs:
        length = end - start
        if start >= 2 and "A" <= text[start - 1] <= "Z":
            # "DE89 3704 ...": two capitals, then the check digits.
            if length >= 2 and "A" <= text[start - 2] <= "Z":
                iban_end = _iban_at(text, start - 2)
                if iban_end != -1:
                    yield "iban", start - 2, iban_end
            # "GOMC790412HDFRRR05": four letters, then the birth date.
            if length == 6 and start >= 4:
                m = _CURP.match(text, start - 4)
                if m is not None:
                    yield "curp", m.start(), m.end()
        if length > 6 or end >= n or (start and text[start - 1] in _NOT_BEFORE_HOUSE_NUMBER):
            continue
        # A house number is followed by a space and a word ("1900 N", "123B
        # Main", "123 3rd Ave").
        after = end + 1 if text[end].isalpha() else end
        if not (after + 1 < n and text[after].isspace() and text[after + 1].isalnum()):
            continue
        if text[after + 1] in "NSEWnsew":
            m = _GRID.match(text, start)
            if m is not None and _grid_ok(m):
                yield "street_grid", m.start(), m.end()
        # The street pattern only when a suffix word is among the next six
        # (a direction, up to four name words, the suffix): most numbers in
        # prose are followed by no "St" or "Road" at all.
        words = text[after:after + 80].split(None, 6)[:6]
        if not any(w.rstrip(".,;:)").lower() in _SUFFIXES for w in words):
            continue
        m = _STREET.match(text, start)
        if m is not None and _street_ok(m, text):
            yield "street_us", m.start(), m.end()


# ── context + value ──────────────────────────────────────────────────────────
_SSN_CONTEXT = re.compile(
    r"\b(?:ssn|ss\s*#|ss\s*no|soc(?:ial)?\.?\s*sec(?:urity)?|social(?:\s+security)?|seguro\s+social)"
    r"(?:\s*(?:number|num|no\.?|#))?" + _FILL
    + r"(?P<v>[0-9]{3}(?P<s>[ -]?)[0-9]{2}(?P=s)[0-9]{4})(?![0-9])", re.I)
_SSN_TRIGGERS = ("ssn", "ss", "soc", "seguro")


def _ssns_context(t: _Text) -> Iterator[Candidate]:
    for m in _at_triggers(t, _SSN_TRIGGERS, _SSN_CONTEXT):
        d = _NON_DIGIT.sub("", m.group("v"))
        if ssn_valid(d[:3], d[3:5], d[5:]) or itin_valid(d[:3], d[3:5], d[5:]):
            yield "ssn_context", m.start("v"), m.end("v")


# A passport / licence / national id / ITIN / EIN / tax id right after its
# word. The value: an optional one- or two-letter prefix ("X", "UT "), then
# digit-led groups ("D1234-56789-01234", "K 0412 9981", "47-1234567"),
# holding 5-20 digits and not shaped like a date.
_GOV_CONTEXT = re.compile(
    r"\b(?:passports?|pasaporte|driver'?s?\s+licen[cs]e|driving\s+licen[cs]e|licen[cs]e|"
    r"licencia(?:\s+de\s+conducir)?|dl\s*(?:#|no\.?|number)|itin|(?-i:EIN|FEIN|TIN)|"
    r"tax\s*(?:payer\s+)?id(?:entification)?|employer\s+identification|national\s+id(?:entity)?|"
    r"id\s+card|state\s+id|c[eé]dula|identificaci[oó]n|rfc)\b" + _FILL
    + r"(?P<v>(?:[A-Z]{1,2}[ -]?)?[0-9][0-9A-Z-]*(?: [0-9][0-9A-Z-]*){0,3})", re.I)
_GOV_TRIGGERS = ("passport", "pasaporte", "driver", "driving", "licen", "dl", "itin", "ein", "fein", "tin",
                 "tax", "employer", "national", "id ", "state", "cédula", "cedula", "identificaci", "rfc")


def _gov_ids_context(t: _Text) -> Iterator[Candidate]:
    for m in _at_triggers(t, _GOV_TRIGGERS, _GOV_CONTEXT):
        value = m.group("v").rstrip("-")
        if 5 <= _ndigits(value) <= 20 and len(value) <= 24 and not _DATE_SHAPES.fullmatch(value):
            yield "gov_id_context", m.start("v"), m.start("v") + len(value)


_MONTH_NAMES = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|enero|febrero|marzo|abril|mayo|"
    r"junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\.?")
_DOB_CONTEXT = re.compile(
    r"\b(?:dob|d\.o\.b\.?|date\s+of\s+birth|birth\s*date|born(?:\s+on)?|fecha\s+de\s+nacimiento|"
    r"nacid[oa]\s+el|naci[oó]\s+el)" + _FILL
    + r"(?P<v>(?P<a>[0-9]{1,2})[/.-](?P<b>[0-9]{1,2})[/.-](?P<y>[0-9]{4}|[0-9]{2})"
    r"|(?P<iy>[0-9]{4})[/.-](?P<im>[0-9]{1,2})[/.-](?P<id>[0-9]{1,2})"
    r"|" + _MONTH_NAMES + r"\s+(?P<md>[0-9]{1,2})(?:st|nd|rd|th)?,?\s+(?P<my>[0-9]{4})"
    r"|(?P<dd>[0-9]{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?" + _MONTH_NAMES + r",?\s+(?:del?\s+)?(?P<dy>[0-9]{4}))"
    r"(?![0-9])", re.I)
_DOB_TRIGGERS = ("dob", "d.o.b", "date", "birth", "born", "fecha", "naci")


def _year_ok(year: str) -> bool:
    return len(year) == 2 or 1900 <= int(year) <= 2099


def _dobs(t: _Text) -> Iterator[Candidate]:
    for m in _at_triggers(t, _DOB_TRIGGERS, _DOB_CONTEXT):
        g = m.groupdict()
        if g["a"] is not None:
            a, b = int(g["a"]), int(g["b"])
            ok = _year_ok(g["y"]) and ((1 <= a <= 12 and 1 <= b <= 31) or (1 <= b <= 12 and 1 <= a <= 31))
        elif g["iy"] is not None:
            ok = _year_ok(g["iy"]) and 1 <= int(g["im"]) <= 12 and 1 <= int(g["id"]) <= 31
        elif g["md"] is not None:
            ok = _year_ok(g["my"]) and 1 <= int(g["md"]) <= 31
        else:
            ok = _year_ok(g["dy"]) and 1 <= int(g["dd"]) <= 31
        if ok:
            yield "dob_context", m.start("v"), m.end("v")


# Bank and account numbers: 6-20 digits (dashes between digits allowed)
# right after their word, not a decimal, not "1,234,567", not a date.
_ACCOUNT_VALUE = r"[0-9](?:[0-9]|-(?=[0-9])){4,23}"
_VALUE_END = r"(?![0-9A-Za-z]|\.[0-9]|,[0-9]{3})"
_BANK_CONTEXT = re.compile(
    r"\b(?:routing|aba|rtn|ach|iban|swift|clabe|bank\s+account|checking\s+account|savings\s+account|"
    r"cuenta\s+(?:bancaria|clabe))\b" + _FILL + r"(?P<v>" + _ACCOUNT_VALUE + r")" + _VALUE_END, re.I)
_BANK_TRIGGERS = ("routing", "aba", "rtn", "ach", "iban", "swift", "clabe", "bank", "checking", "savings",
                  "cuenta")
_ACCOUNT_CONTEXT = re.compile(
    r"\b(?:account|acct|a/c|cuenta|(?:loan|member|policy)\s*(?:number|no\.?|num|#|id))\b" + _FILL
    + r"(?P<v>" + _ACCOUNT_VALUE + r")" + _VALUE_END, re.I)
_ACCOUNT_TRIGGERS = ("account", "acct", "a/c", "cuenta", "loan", "member", "policy")
# "ACH: 021000021 / 4471882209": a number listed right after the first
# shares its context.
_CONTINUATION = re.compile(r"\s*(?:[/,;&]|and|y)\s*(?P<v>" + _ACCOUNT_VALUE + r")" + _VALUE_END, re.I)


def _context_numbers(t: _Text, triggers: Tuple[str, ...], pattern: Pattern[str]) -> Iterator[Tuple[int, int]]:
    text = t.text
    for m in _at_triggers(t, triggers, pattern):
        start, end = m.span("v")
        while True:
            value = text[start:end]
            if 6 <= _ndigits(value) <= 20 and not _DATE_SHAPES.fullmatch(value):
                yield start, end
            nxt = _CONTINUATION.match(text, end)
            if nxt is None:
                break
            start, end = nxt.span("v")


def _bank_numbers(t: _Text) -> Iterator[Candidate]:
    for start, end in _context_numbers(t, _BANK_TRIGGERS, _BANK_CONTEXT):
        yield "bank_context", start, end


def _account_numbers(t: _Text) -> Iterator[Candidate]:
    for start, end in _context_numbers(t, _ACCOUNT_TRIGGERS, _ACCOUNT_CONTEXT):
        # An ABA-valid routing number listed as an "account" is still a bank number.
        digits = _NON_DIGIT.sub("", t.text[start:end])
        yield ("bank_context" if aba_valid(digits) else "account_context"), start, end


_PO_BOX = re.compile(
    r"\b(?:p\.?\s*o\.?\s*box|post\s+office\s+box|apartado(?:\s+postal)?|casilla(?:\s+postal)?)\s*#?\s*"
    r"[0-9]{1,6}(?![0-9])", re.I)
_PO_BOX_TRIGGERS = ("box", "apartado", "casilla")


def _po_boxes(t: _Text) -> Iterator[Candidate]:
    text, n = t.text, t.n
    covered = -1
    for pos in t.positions(_PO_BOX_TRIGGERS):
        if pos < covered:
            continue
        # "box" sits inside the match ("P.O. Box 381"): search around it.
        for m in _PO_BOX.finditer(text, max(0, pos - 20), min(n, pos + 40)):
            if m.start() <= pos < m.end():
                covered = m.end()
                yield "po_box", m.start(), m.end()
                break


_LATAM = re.compile(
    r"\b(?i:calle|avenida|av\.|avda\.?|carrera|cra\.?|calzada|camino|paseo|privada|callej[oó]n|rua|"
    r"travessa|diagonal|transversal)\s+"
    r"(?:(?:de|del|la|las|los|el|y)\s+|[A-ZÁÉÍÓÚÑ0-9][\wÁÉÍÓÚÑáéíóúñü.]*,?\s+){1,5}?"
    r"(?i:#|no\.?|n[uú]m\.?|n[°º]|nro\.?)?\s*[0-9]{1,5}(?![0-9])")
_LATAM_TRIGGERS = ("calle", "avenida", "av.", "avda", "carrera", "cra", "calzada", "camino", "paseo",
                   "privada", "callej", "rua", "travessa", "diagonal", "transversal")


def _latam_addresses(t: _Text) -> Iterator[Candidate]:
    for m in _at_triggers(t, _LATAM_TRIGGERS, _LATAM):
        yield "street_latam", m.start(), m.end()


# ── the scan ─────────────────────────────────────────────────────────────────
# (recogniser, needs a digit in the text)
_RECOGNISERS: Sequence[Tuple[Callable[[_Text], Iterator[Candidate]], bool]] = (
    (_emails, False),
    (_emails_obfuscated, False),
    (_cluster_numbers, True),
    (_run_numbers, True),
    (_ssns_context, True),
    (_gov_ids_context, True),
    (_dobs, True),
    (_bank_numbers, True),
    (_account_numbers, True),
    (_po_boxes, True),
    (_latam_addresses, True),
)


def find(text: str) -> List[PiiMatch]:
    """Every PII entity the patterns recognise in ``text``, as non-overlapping
    :class:`PiiMatch` spans in text order. Where two rules claim overlapping
    text the earlier rule in :data:`RULES` keeps it. ``[]`` for an empty
    string or anything that is not a string; never raises -- a recogniser
    that raises on this text keeps what it found before the error, loses the
    rest of this text, and the other recognisers still run."""
    if not isinstance(text, str) or not text:
        return []
    scan = _Text(text)
    candidates: List[Tuple[int, int, int, str]] = []
    for recogniser, needs_digit in _RECOGNISERS:
        if needs_digit and not scan.runs:
            continue
        try:
            for rule, start, end in recogniser(scan):
                if end > start:
                    candidates.append((_PRIORITY[rule], start, end, rule))
        except Exception:  # noqa: BLE001 -- a bug in one rule must not cost the others (or the caller)
            continue
    candidates.sort()
    starts: List[int] = []
    ends: Dict[int, Tuple[int, str]] = {}
    for _, start, end, rule in candidates:
        i = bisect_left(starts, start)
        if i < len(starts) and starts[i] < end:
            continue  # a kept span starts inside this one
        if i > 0 and ends[starts[i - 1]][0] > start:
            continue  # this one starts inside a kept span
        insort(starts, start)
        ends[start] = (end, rule)
    return [PiiMatch(RULES[ends[s][1]], s, ends[s][0], ends[s][1]) for s in starts]


def counts(text: str) -> Dict[str, int]:
    """``{type: number of entities}`` for the types :func:`find` sees in
    ``text``, in :data:`ENTITY_TYPES` order; types with none are absent."""
    tally: Dict[str, int] = {}
    for match in find(text):
        tally[match.type] = tally.get(match.type, 0) + 1
    return {t: tally[t] for t in ENTITY_TYPES if t in tally}
