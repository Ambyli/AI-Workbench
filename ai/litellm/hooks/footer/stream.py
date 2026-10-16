"""The `auto` footer on a streamed answer (see auto.py for what it says)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .auto import AutoFooter
from .render import choice_has_tool_calls, footer_text, get_field, headers_of, set_field, to_int


class FooterStream:
    """Per-request state for the streaming footer. before() / finish() never raise.

    A choice qualifies when it streamed non-empty `delta.content` and no tool
    call. Its footer goes out once, ahead of its finish chunk: appended to that
    chunk's own `delta.content` when it carries text (so the footer is never
    put in front of the last words), otherwise as an extra chunk yielded just
    before it. A stream that ends without a finish chunk gets the footer last.
    """

    def __init__(self, auto: AutoFooter, wrapper: Any, data: dict) -> None:
        self.auto = auto
        self.wrapper = wrapper
        self.data = data
        self.ok = True
        self.text: Dict[int, bool] = {}
        self.tools: Dict[int, bool] = {}
        self.done: set = set()
        self.last_chunk: Any = None
        self._footer: Optional[str] = None
        self._resolved = False

    def footer(self, chunk: Any) -> Optional[str]:
        if not self._resolved:
            self._resolved = True
            headers = {**headers_of(chunk), **headers_of(self.wrapper)}
            label = self.auto.label(headers, self.data)
            self._footer = footer_text(label) if label else None
        return self._footer

    def before(self, chunk: Any) -> List[Any]:
        if not self.ok:
            return []
        try:
            choices = getattr(chunk, "choices", None)
            if not isinstance(choices, list):
                return []
            self.last_chunk = chunk
            appends: List[Any] = []
            separate: List[int] = []
            for c in choices:
                idx = to_int(get_field(c, "index"))
                delta = get_field(c, "delta")
                if delta is not None:
                    if choice_has_tool_calls(delta):
                        self.tools[idx] = True
                    content = get_field(delta, "content")
                    if isinstance(content, str) and content.strip():
                        self.text[idx] = True
                if not get_field(c, "finish_reason") or idx in self.done:
                    continue
                self.done.add(idx)
                if not self.text.get(idx) or self.tools.get(idx):
                    continue
                if self.footer(chunk) is None:
                    continue
                content = get_field(delta, "content") if delta is not None else None
                if isinstance(content, str) and content:
                    appends.append(delta)
                else:
                    separate.append(idx)
            extra = [self._make_chunk(chunk, separate)] if separate else []
            footer = self._footer or ""
            for delta in appends:
                set_field(delta, "content", get_field(delta, "content") + footer)
            if appends or separate:
                self.auto.stats["footers"] += 1
            return extra
        except Exception as exc:
            self.ok = False
            self.auto.error("stream", exc)
            return []

    def finish(self) -> List[Any]:
        if not self.ok or self.last_chunk is None:
            return []
        try:
            pending = [i for i, has in self.text.items() if has and not self.tools.get(i) and i not in self.done]
            if not pending or self.footer(self.last_chunk) is None:
                return []
            self.done.update(pending)
            self.auto.stats["footers"] += 1
            return [self._make_chunk(self.last_chunk, sorted(pending))]
        except Exception as exc:
            self.ok = False
            self.auto.error("stream-end", exc)
            return []

    def _make_chunk(self, template: Any, indices: List[int]) -> Any:
        from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

        kwargs: Dict[str, Any] = {}
        model = get_field(template, "model")
        if isinstance(model, str):
            kwargs["model"] = model
        chunk = ModelResponseStream(
            id=get_field(template, "id"),
            created=get_field(template, "created"),
            choices=[StreamingChoices(index=i, delta=Delta(content=self._footer), finish_reason=None) for i in indices],
            **kwargs,
        )
        hidden = getattr(template, "_hidden_params", None)
        if isinstance(hidden, dict):
            chunk._hidden_params = dict(hidden)
        return chunk
