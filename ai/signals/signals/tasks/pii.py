"""PII NER: ``gravitee-io/bert-small-pii-detection`` mapped onto ``common.pii``'s vocabulary.

The model tags tokens with 25 Presidio-style labels in BIO form. A text is
scanned whole, in windows of ``tokens.window`` tokens that overlap by
``tokens.stride`` (models.yaml); per window the tokens are decoded into
spans, spans of one entity that overlap across windows are merged, and each
span is mapped onto the fixed vocabulary shared with ``common.pii``
(ROUTE_POLICY_PHASE5_PLAN § 3.1):

* ``LOCATION`` that contains an ASCII digit -> ``street_address``; one
  without a digit -> ``location``, which never forces a route. That one rule
  turned the model's recall on the plan's PII set from 0.58 into 0.89.
* ``PERSON`` -> ``person`` (it forces only together with sensitive data --
  the ruleset's ``with_person``; never alone).
* SSN / passport / driver's licence / ITIN -> ``gov_id``; ``CREDIT_CARD`` ->
  ``card_number``; ``IBAN_CODE`` / ``US_BANK_NUMBER`` -> ``bank_account``;
  ``FINANCIAL`` -> ``account_number``; e-mail and phone -> themselves.
* every other label (dates, ages, organisations, URLs, IPs, passwords, ...)
  -> ``other``. ``DATE_TIME`` is any date or time ("tomorrow at 3"), so it is
  not ``date_of_birth``: a birth date needs context, which is what
  ``common.pii``'s patterns read.

The spans exist only inside ``PiiModel.scan``; what leaves is
``{type: {"count", "max_score"}}``.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Dict, Generator, Iterable, List, Mapping, Optional, Sequence, Tuple

from common.pii import ENTITY_TYPES

from .. import config

# Model label (without its B-/I- prefix) -> vocabulary type. LOCATION is
# decided per span (_LOCATION_DIGIT); anything missing here is "other".
LABEL_TYPES: Dict[str, str] = {
    "PERSON": "person",
    "EMAIL_ADDRESS": "email",
    "PHONE_NUMBER": "phone",
    "US_SSN": "gov_id",
    "US_PASSPORT": "gov_id",
    "US_DRIVER_LICENSE": "gov_id",
    "US_ITIN": "gov_id",
    "CREDIT_CARD": "card_number",
    "IBAN_CODE": "bank_account",
    "US_BANK_NUMBER": "bank_account",
    "FINANCIAL": "account_number",
}
LOCATION_LABEL = "LOCATION"
_LOCATION_DIGIT = re.compile(r"[0-9]")

# A sentence with no PII in it, for the warm-up inference.
WARM_UP_TEXT = "Hello, this is a short warm-up sentence for the signals sidecar."


@dataclass(frozen=True)
class Span:
    """One decoded entity: the model label (no B-/I-), character offsets into
    the text, and the best token probability inside it. Never leaves this
    module."""

    entity: str
    start: int
    end: int
    score: float


def entity_type(entity: str, piece: str) -> str:
    """The vocabulary type of one span of model label ``entity`` whose text is ``piece``."""
    if entity == LOCATION_LABEL:
        return "street_address" if _LOCATION_DIGIT.search(piece) else "location"
    return LABEL_TYPES.get(entity, "other")


def decode_bio(labels: Sequence[str], scores: Sequence[float], offsets: Sequence[Tuple[int, int]],
               special: Sequence[int], min_prob: float) -> List[Span]:
    """Tokens -> spans. A token joins the open span when it carries ``I-``
    of the same entity; ``B-``, another entity, ``O``, a special token or a
    probability below ``min_prob`` closes it (the decoding the plan measured)."""
    spans: List[Span] = []
    cur: Optional[List[Any]] = None
    for label, score, (start, end), is_special in zip(labels, scores, offsets, special):
        if is_special or label == "O" or score < min_prob or end <= start:
            if cur is not None:
                spans.append(Span(*cur))
                cur = None
            continue
        tag, entity = label.split("-", 1) if "-" in label else ("I", label)
        if cur is not None and cur[0] == entity and tag == "I":
            cur[2] = end
            cur[3] = max(cur[3], float(score))
            continue
        if cur is not None:
            spans.append(Span(*cur))
        cur = [entity, start, end, float(score)]
    if cur is not None:
        spans.append(Span(*cur))
    return spans


def merge_spans(spans: Iterable[Span]) -> List[Span]:
    """Merge spans of one entity that overlap -- the same entity seen by two
    windows in their shared ``stride`` tokens is counted once."""
    out: List[Span] = []
    for span in sorted(spans, key=lambda s: (s.entity, s.start, s.end)):
        last = out[-1] if out else None
        if last is not None and last.entity == span.entity and span.start < last.end:
            out[-1] = Span(last.entity, last.start, max(last.end, span.end), max(last.score, span.score))
        else:
            out.append(span)
    return sorted(out, key=lambda s: (s.start, s.end))


def summarise(text: str, spans: Iterable[Span]) -> Dict[str, Dict[str, Any]]:
    """{type: {"count", "max_score"}} over ``spans`` -- the only thing that
    leaves the sidecar. Scores are rounded to 3 decimals."""
    out: Dict[str, Dict[str, Any]] = {}
    for span in spans:
        kind = entity_type(span.entity, text[span.start:span.end])
        entry = out.setdefault(kind, {"count": 0, "max_score": 0.0})
        entry["count"] += 1
        entry["max_score"] = max(entry["max_score"], round(span.score, 3))
    return dict(sorted(out.items()))


def _check_vocabulary() -> None:
    produced = set(LABEL_TYPES.values()) | {"street_address", "location", "other"}
    unknown = produced - set(ENTITY_TYPES)
    if unknown:  # pragma: no cover - a programming error, caught by the tests
        raise RuntimeError(f"pii label mapping produces types outside common.pii.ENTITY_TYPES: {sorted(unknown)}")


_check_vocabulary()


class PiiModel:
    """The NER session, its tokenizer and labels. Used by one lane worker
    thread at a time (the tokenizer's truncation is configured once)."""

    lane = "pii"

    def __init__(self, session: Any, tokenizer: Any, id2label: Mapping[int, str], *, window: int, stride: int,
                 min_prob: float = config.PII_TOKEN_MIN_PROB) -> None:
        from ..runtime import input_names

        self.session = session
        self.tokenizer = tokenizer
        self.labels = [id2label[i] for i in range(len(id2label))]
        self.window = int(window)
        self.stride = int(stride)
        self.min_prob = float(min_prob)
        self._inputs = input_names(session)
        tokenizer.no_padding()
        tokenizer.enable_truncation(self.window, stride=self.stride)

    @classmethod
    def load(cls, root: pathlib.Path, spec: Any, threads: int) -> "PiiModel":
        from tokenizers import Tokenizer

        from ..runtime import ort_session

        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        id2label = {int(k): v for k, v in cfg["id2label"].items()}
        return cls(ort_session(root / spec.model, threads), Tokenizer.from_file(str(root / "tokenizer.json")),
                   id2label, window=spec.tokens["window"], stride=spec.tokens["stride"])

    def _logits(self, encoding: Any) -> Any:
        import numpy as np

        from ..runtime import feed

        ids = np.asarray([encoding.ids], dtype=np.int64)
        return self.session.run(None, feed(self._inputs, ids))[0][0]

    def _decode(self, encoding: Any, logits: Any) -> List[Span]:
        import numpy as np

        probs = np.exp(logits - logits.max(-1, keepdims=True))
        probs /= probs.sum(-1, keepdims=True)
        best = probs.argmax(-1)
        labels = [self.labels[int(j)] for j in best]
        scores = probs[np.arange(len(best)), best].tolist()
        return decode_bio(labels, scores, encoding.offsets, encoding.special_tokens_mask, self.min_prob)

    def scan(self, text: str) -> Generator[Dict[str, Dict[str, Any]], None, Dict[str, Dict[str, Any]]]:
        """Scan ``text`` window by window. Yields the summary so far after
        each window (what a deadline cut reports, ``complete: false``) and
        returns the summary of the whole text."""
        if not text.strip():
            return {}
        first = self.tokenizer.encode(text)
        spans: List[Span] = []
        for encoding in [first, *first.overflowing]:
            spans.extend(self._decode(encoding, self._logits(encoding)))
            yield summarise(text, merge_spans(spans))
        return summarise(text, merge_spans(spans))

    def warm_up(self) -> None:
        encoding = self.tokenizer.encode(WARM_UP_TEXT)
        logits = self._logits(encoding)
        if logits.shape != (len(encoding.ids), len(self.labels)):
            raise RuntimeError(f"pii: logits shape {logits.shape}, expected ({len(encoding.ids)}, {len(self.labels)})")
