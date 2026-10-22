"""text_shape -- does the latest user message look like pasted code?

Pure (no sidecar): counts lines, code fences, lines that look like code,
URLs and attachments of the latest user message.

Config (at least one of the two)::

    min_lines: 5           # at least this many non-empty lines ...
    min_code_ratio: 0.5    # ... of which at least this share look like code

A line "looks like code" when it is inside a ``` fence, or (outside one)
matches :data:`CODE_LINE`: a statement keyword at its start (``def``,
``import``, ``const``, ``SELECT`` ...), a line ending in ``;`` / ``{`` /
``}``, one starting with a closing bracket or a ``//`` comment, an
operator (``=>``, ``::``, ``->``, ``==``, ``&&``), an assignment
(``name = value``), a call alone on its line, a markup tag, or 4+ spaces /
a tab of indentation. A heuristic for measuring -- the ``default-signals``
draft only observes it -- not a parser.

**Attachments** are counted from the latest user message's content parts by
their ``type`` (the LiteLLM hook replaces each inline base64 payload with a
short placeholder but keeps the part and its type): ``image_url`` /
``input_image`` / ``image`` -> image, ``file`` / ``input_file`` /
``document`` -> file, ``input_audio`` / ``audio`` -> audio.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from pydantic import Field, model_validator

from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares

CODE_LINE = re.compile(
    r"^\s*(?:def|class|import|from\s+[\w.]+\s+import|return|elif|except|finally|async\s+def|await|"
    r"function|const|let|var|public|private|protected|static|void|package|using|namespace|#include|"
    r"fn|impl|struct|enum|interface|SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|WITH)\b"
    r"|[;{}]\s*$"
    r"|^\s*[}\])]"
    r"|^\s*(?://|/\*|\*/)"
    r"|=>|::|->|==|!=|&&|\|\|"
    r"|^\s*[\w.\[\]'\"]+\s*[+\-*/]?=\s*\S"
    r"|^\s*[\w.]+\([^()]*\)\s*(?:;\s*)?$"
    r"|^\s*</?[A-Za-z][\w-]*(?:\s[^>]*)?/?>"
    r"|^(?: {4}|\t)\S"
)
FENCE = re.compile(r"^\s*(```|~~~)")
URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
ATTACHMENT_KINDS = {
    "image_url": "image", "input_image": "image", "image": "image",
    "file": "file", "input_file": "file", "document": "file",
    "input_audio": "audio", "audio": "audio",
}


class TextShapeConfig(EvaluatorConfig):
    min_lines: Optional[int] = Field(None, ge=1, le=100_000)
    min_code_ratio: Optional[float] = Field(None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _something(self) -> "TextShapeConfig":
        if self.min_lines is None and self.min_code_ratio is None:
            raise ValueError("give min_lines, min_code_ratio, or both")
        return self


def shape(text: str) -> Dict[str, Any]:
    """The counts of one text (no attachments)."""
    lines = [line for line in text.splitlines() if line.strip()]
    fenced_blocks = code_lines = 0
    inside = False
    for line in lines:
        if FENCE.match(line):
            if not inside:
                fenced_blocks += 1
            inside = not inside
            continue
        if inside or CODE_LINE.search(line):
            code_lines += 1
    counted = [line for line in lines if not FENCE.match(line)]
    return {
        "chars": len(text),
        "lines": len(lines),
        "fenced_blocks": fenced_blocks,
        "code_lines": code_lines,
        "code_ratio": round(code_lines / len(counted), 6) if counted else 0.0,
        "urls": len(URL.findall(text)),
    }


def attachments(message: Any) -> Dict[str, int]:
    out = {"image": 0, "file": 0, "audio": 0}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for part in content:
            kind = ATTACHMENT_KINDS.get(part.get("type")) if isinstance(part, dict) else None
            if kind:
                out[kind] += 1
    return out


@declares(OutputSpec(
    type="text_shape",
    summary="The shape of the latest user message: lines, code fences, code-looking lines, URLs, "
            "attachments. Pure, no sidecar.",
    votes="vote.route when the latest user message has at least `min_lines` non-empty lines and at least "
          "`min_code_ratio` of them look like code (each condition only when set); abstains otherwise, "
          "and when there is no user message.",
    fields={
        "chars": FieldSpec("Characters of the user text", "integer"),
        "lines": FieldSpec("Non-empty lines", "integer"),
        "fenced_blocks": FieldSpec("``` / ~~~ fenced blocks", "integer"),
        "code_lines": FieldSpec("Lines that look like code (inside a fence, or CODE_LINE)", "integer"),
        "code_ratio": FieldSpec("code_lines / non-empty lines outside the fence markers", "number"),
        "urls": FieldSpec("http(s) URLs", "integer"),
        "attachments": FieldSpec("{image, file, audio}: content parts of the latest user message by type",
                                 "object"),
    },
))
class TextShape(Evaluator):
    Config = TextShapeConfig

    async def evaluate(self, ctx) -> Outcome:
        detail = {**shape(ctx.current_text), "attachments": attachments(ctx.latest_user_message)}
        if not ctx.exchanges:
            return Outcome.abstain("no user message", detail)
        lines, ratio = detail["lines"], detail["code_ratio"]
        enough_lines = self.config.min_lines is None or lines >= self.config.min_lines
        enough_code = self.config.min_code_ratio is None or ratio >= self.config.min_code_ratio
        if enough_lines and enough_code:
            return Outcome.vote(f"looks like code: {detail['code_lines']}/{lines} lines", detail)
        return Outcome.abstain(f"does not look like code: {detail['code_lines']}/{lines} lines", detail)
