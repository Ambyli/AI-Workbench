"""Jailbreak / prompt injection: ``NeuralTrust/prompt-guard-oss-small``.

A sequence classifier (benign / jailbreak); the feature is P(jailbreak),
the softmax of the label the model's config.json calls ``jailbreak``. A long
text is not scored whole -- that is ~340 ms for 2,000 characters at 2
threads on the box -- but on at most two windows of ``max_tokens``
(``head_tail``, the wire's only window mode): the first and the LAST
``max_tokens`` tokens, since an attack is usually at the start or appended
at the end; one window when the text fits in one. The score is the larger
of the two. Route-policy logs it (role ``observe``)
and decides nothing on it yet (plan § 4.3).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Generator, List

WARM_UP_TEXT = "Hello, this is a short warm-up sentence for the signals sidecar."


def positive_index(id2label: Dict[int, str]) -> int:
    """The index of the jailbreak / injection label in ``id2label``."""
    for index, label in sorted(id2label.items()):
        if any(word in label.lower() for word in ("jailbreak", "injection", "malicious")):
            return index
    raise RuntimeError(f"injection: no jailbreak label in {sorted(id2label.values())}")


class InjectionModel:
    """The classifier session and tokenizer. Used by one lane worker thread at a time."""

    lane = "injection"

    def __init__(self, session: Any, tokenizer: Any, positive: int, *, max_tokens: int) -> None:
        from ..runtime import input_names

        self.session = session
        self.tokenizer = tokenizer
        self.positive = int(positive)
        self.max_tokens = int(max_tokens)
        self._inputs = input_names(session)
        self._truncation = None
        # tokenizer.json ships with padding and a 512-token truncation on.
        tokenizer.no_padding()

    @classmethod
    def load(cls, root: pathlib.Path, spec: Any, threads: int) -> "InjectionModel":
        from tokenizers import Tokenizer

        from ..runtime import ort_session

        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        id2label = {int(k): v for k, v in cfg["id2label"].items()}
        return cls(ort_session(root / spec.model, threads), Tokenizer.from_file(str(root / "tokenizer.json")),
                   positive_index(id2label), max_tokens=spec.tokens["max"])

    def _truncate(self, max_tokens: int, direction: str) -> None:
        if self._truncation != (max_tokens, direction):
            self.tokenizer.enable_truncation(max_tokens, direction=direction)
            self._truncation = (max_tokens, direction)

    def windows(self, text: str, max_tokens: int) -> List[List[int]]:
        """Token ids of the head window, plus the tail window when the text
        does not fit in one. Each window keeps the tokenizer's special tokens."""
        max_tokens = max(8, min(int(max_tokens), self.max_tokens))
        self._truncate(max_tokens, "right")
        head = self.tokenizer.encode(text)
        out = [list(head.ids)]
        if head.overflowing:
            self._truncate(max_tokens, "left")
            out.append(list(self.tokenizer.encode(text).ids))
        return out

    def _score(self, ids: List[int]) -> float:
        import numpy as np

        from ..runtime import feed

        logits = self.session.run(None, feed(self._inputs, np.asarray([ids], dtype=np.int64)))[0][0]
        exp = np.exp(logits - logits.max())
        return float((exp / exp.sum())[self.positive])

    def scan(self, text: str, max_tokens: int) -> Generator[None, None, Dict[str, Any]]:
        """``{"score", "windows"}``; one window per step."""
        windows = self.windows(text, max_tokens)
        best = 0.0
        for ids in windows:
            best = max(best, self._score(ids))
            yield None
        return {"score": round(best, 4), "windows": len(windows)}

    def warm_up(self) -> None:
        score = self._score(self.windows(WARM_UP_TEXT, 64)[0])
        if not 0.0 <= score <= 1.0:
            raise RuntimeError(f"injection: score {score} outside [0, 1]")
