"""Deterministic US street-address parsing for account matching.

Splits what the customer typed into the pieces the phoenix address query scores on
(street, city, ZIP). Deliberately NOT an LLM step: the account match is model-blind,
so its query inputs come only from the customer's own text, parsed by code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .trgm import similarity

_MAX_LEN = 200

_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh",
    "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut",
    "vt", "va", "wa", "wv", "wi", "wy", "pr",
}

# Street suffixes -> USPS abbreviation (so "Street" and "St" compare as equal).
_SUFFIX = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave", "road": "rd",
    "rd": "rd", "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln", "way": "way", "wy": "way",
    "court": "ct", "ct": "ct", "boulevard": "blvd", "blvd": "blvd", "place": "pl", "pl": "pl",
    "circle": "cir", "cir": "cir", "terrace": "ter", "ter": "ter", "parkway": "pkwy",
    "pkwy": "pkwy", "highway": "hwy", "hwy": "hwy", "trail": "trl", "trl": "trl", "loop": "loop",
    "square": "sq", "sq": "sq", "crossing": "xing", "xing": "xing", "cove": "cv", "cv": "cv",
    "path": "path", "pike": "pike", "run": "run", "row": "row", "alley": "aly", "aly": "aly",
}
_DIRECTIONAL = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"}
_UNIT = re.compile(r"\b(?:apt|apartment|suite|ste|unit|bldg|building|rm|room)\.?\s*[a-z0-9-]+\b|#\s*[a-z0-9-]+",
                   re.IGNORECASE)
_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
_NUMBER = re.compile(r"^\s*(\d+[a-z]?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class AddressParts:
    street: str = ""   # house number + street name, unit removed ("318 Kilowat Drive")
    number: str = ""   # house number ("206"); empty -> cannot match
    city: str = ""
    state: str = ""
    zip: str = ""      # 5 digits or empty


def _clean(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text or "")[:_MAX_LEN]
    text = re.sub(r"\b(usa|united states)\b\.?", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ,.")


def parse(text: str) -> AddressParts:
    s = _clean(text)
    if not s:
        return AddressParts()

    zip5 = ""
    m = _ZIP.search(s)
    if m:
        zip5 = m.group(1)
        s = s[: m.start()].rstrip(" ,.")

    state = ""
    m = re.search(r"(?:^|[\s,])([A-Za-z]{2})\.?$", s)
    if m and m.group(1).lower() in _STATES:
        state = m.group(1).upper()
        s = s[: m.start()].rstrip(" ,.")

    s = re.sub(r"\s+", " ", _UNIT.sub(" ", s)).strip(" ,.")
    if "," in s:
        head, _, tail = s.partition(",")
        street, city = head, tail.split(",")[0]
    else:
        street, city = _split_on_suffix(s)

    street = street.strip(" ,.")
    nm = _NUMBER.match(street)
    return AddressParts(
        street=street,
        number=nm.group(1).lower() if nm else "",
        city=city.strip(" ,."),
        state=state,
        zip=zip5,
    )


def _split_on_suffix(s: str) -> tuple[str, str]:
    """No commas: street ends at the last street suffix (plus an optional trailing
    directional); the rest is the city. Without a recognizable suffix, keep it all
    as the street (the query still scores it; the city just contributes nothing)."""
    words = s.split()
    cut = None
    for i, w in enumerate(words):
        if i > 0 and w.lower().strip(".") in _SUFFIX:
            cut = i
    if cut is None:
        return s, ""
    if cut + 1 < len(words) and words[cut + 1].lower().strip(".") in _DIRECTIONAL:
        cut += 1
    return " ".join(words[: cut + 1]), " ".join(words[cut + 1:])


def house_number(street: str) -> str:
    m = _NUMBER.match(street or "")
    return m.group(1).lower() if m else ""


def _street_forms(street: str) -> tuple[str, str]:
    """(full, core): lower-case street without the house number or unit, with suffixes
    abbreviated; core also drops suffixes and directionals ("solar")."""
    words = re.findall(r"[a-z0-9]+", _UNIT.sub(" ", (street or "").lower()))
    if words and _NUMBER.match(words[0]):
        words = words[1:]
    full = [_SUFFIX.get(w, w) for w in words]
    core = [w for w in words if w not in _SUFFIX and w not in _DIRECTIONAL]
    return " ".join(full), " ".join(core)


def street_similarity(a: str, b: str) -> float:
    """Street-name similarity ignoring the house number: the best of the full form
    (suffixes normalized) and the core form (suffixes and directionals dropped)."""
    fa, ca = _street_forms(a)
    fb, cb = _street_forms(b)
    return max(similarity(fa, fb), similarity(ca, cb) if ca and cb else 0.0)
