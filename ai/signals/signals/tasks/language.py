"""Language identification: fastText ``lid.176.ftz`` (CC-BY-SA-3.0, used unmodified).

0.01-0.3 ms per text, so it runs inline on the event loop rather than on a
lane. fastText reads one line, so whitespace (newlines included) is
collapsed to single spaces first. A text with no letter in it has no
language: ``und`` with confidence 0 (fastText would guess ``en`` at ~0.12).
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict

WARM_UP_TEXT = "Hello, this is a short warm-up sentence for the signals sidecar."
UNDETERMINED = "und"
_LABEL_PREFIX = "__label__"


class LanguageModel:
    """A loaded fastText model."""

    lane = "language"

    def __init__(self, model: Any) -> None:
        self.model = model

    @classmethod
    def load(cls, root: pathlib.Path, spec: Any, threads: int) -> "LanguageModel":
        import fasttext

        return cls(fasttext.load_model(str(root / spec.model)))

    def detect(self, text: str) -> Dict[str, Any]:
        """``{"language": <ISO code>, "confidence": 0..1}``."""
        line = " ".join(text.split())
        if not any(ch.isalpha() for ch in line):
            return {"language": UNDETERMINED, "confidence": 0.0}
        labels, probs = self.model.predict(line, k=1)
        label = labels[0] if labels else ""
        language = label[len(_LABEL_PREFIX):] if label.startswith(_LABEL_PREFIX) else (label or UNDETERMINED)
        confidence = min(1.0, max(0.0, float(probs[0]))) if len(probs) else 0.0
        return {"language": language, "confidence": round(confidence, 3)}

    def warm_up(self) -> None:
        if self.detect(WARM_UP_TEXT)["language"] != "en":
            raise RuntimeError("language: the warm-up sentence was not identified as English")
