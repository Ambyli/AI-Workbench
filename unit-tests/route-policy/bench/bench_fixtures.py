"""Fabricated Open WebUI chats for the benchmark tests -- no real data.

Builders for messages as ``export_chats.py`` writes them (Open WebUI 0.11.4's
stored shape: ``content`` plus the assistant's ``output`` items), whole
exported chats, and the ruleset / criteria fixtures the run tests load.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, List, Optional, Sequence

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
SERVICE_DIR = ROOT / "ai" / "route-policy"
RULESETS_DIR = SERVICE_DIR / "rulesets"
FOOTER_SOURCE = ROOT / "ai" / "litellm" / "hooks" / "footer" / "render.py"

T0 = 1_760_000_000  # epoch seconds of the first message

CRITERIA = {
    "local-code": "The latest user request is about writing, running or debugging code.",
    "local-reasoning": "The latest user request needs careful multi-step reasoning such as maths.",
    "local-private": "The conversation contains personal data about a real person.",
}


def ruleset_document(*, version: int = 1, exchanges: int = 4, max_chars: int = 6000, min_score: int = 7,
                     otherwise: Optional[str] = "local-general",
                     criteria: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A shadow ruleset whose only evaluator is an llm_classifier `topic-llm`."""
    return {
        "id": "bench-test",
        "version": version,
        "description": "fabricated criteria set for the benchmark tests",
        "mode": "shadow",
        "routes": ["local-general", "local-code", "local-reasoning", "local-private"],
        "default_route": "local-general",
        "evaluators": [{
            "id": "topic-llm",
            "type": "llm_classifier",
            "config": {
                "model": "qwen3.5-4b",
                "criteria": dict(criteria or CRITERIA),
                "otherwise": otherwise,
                "min_score": min_score,
                "history": {"exchanges": exchanges, "max_chars": max_chars},
            },
        }],
        "combiner": {"type": "priority"},
    }


def ruleset_yaml(**kw: Any) -> str:
    return yaml.safe_dump(ruleset_document(**kw), sort_keys=False, allow_unicode=True)


def write_ruleset(directory: pathlib.Path, name: str = "bench-test.yaml", **kw: Any) -> pathlib.Path:
    path = directory / name
    path.write_text(ruleset_yaml(**kw), encoding="utf-8", newline="\n")
    return path


# -- output items ---------------------------------------------------------------------


def out_text(text: str) -> Dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def out_call(call_id: str, name: str, arguments: str = "{}", status: str = "completed") -> Dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments, "status": status}


def out_result(call_id: str, text: str = "done", images: Sequence[str] = ()) -> Dict[str, Any]:
    parts: List[Dict[str, Any]] = [{"type": "input_text", "text": text}]
    parts += [{"type": "input_image", "image_url": url} for url in images]
    return {"type": "function_call_output", "call_id": call_id, "output": parts}


def out_reasoning(text: str = "thinking") -> Dict[str, Any]:
    return {"type": "reasoning", "summary": [{"type": "output_text", "text": text}]}


# -- stored messages ------------------------------------------------------------------


class Branch:
    """Builds a linear branch: each message's parent is the one before it."""

    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.clock = T0

    def _add(self, msg: Dict[str, Any], gap: float) -> Dict[str, Any]:
        self.clock += gap
        msg.setdefault("id", f"m{len(self.messages) + 1}")
        msg["parentId"] = self.messages[-1]["id"] if self.messages else None
        msg.setdefault("timestamp", self.clock)
        msg.setdefault("model", None)
        msg.setdefault("done", True)
        msg.setdefault("error", False)
        msg.setdefault("files", [])
        msg.setdefault("context_summary", False)
        msg.setdefault("sources", 0)
        msg.setdefault("output", None)
        self.messages.append(msg)
        return msg

    def user(self, text: Any, *, files: Sequence[Dict[str, Any]] = (), gap: float = 30.0, **kw: Any) -> Dict[str, Any]:
        return self._add({"role": "user", "content": text, "files": list(files), **kw}, gap)

    def assistant(self, text: str = "ok", *, output: Optional[List[Dict[str, Any]]] = None, gap: float = 5.0,
                  **kw: Any) -> Dict[str, Any]:
        msg = {"role": "assistant", "content": text, "model": "auto", **kw}
        if output is not None:
            msg["output"] = output
        return self._add(msg, gap)

    def chat(self, chat_id: str = "chat-1", **kw: Any) -> Dict[str, Any]:
        return chat_record(self.messages, chat_id=chat_id, **kw)


def chat_record(messages: List[Dict[str, Any]], *, chat_id: str = "chat-1", user_id: str = "user-1",
                context_compaction: bool = False, function_calling: Optional[str] = None) -> Dict[str, Any]:
    return {
        "chat_id": chat_id,
        "user_id": user_id,
        "created_at": T0,
        "updated_at": T0 + 3600,
        "current_message_id": messages[-1]["id"] if messages else None,
        "source": "chat_message",
        "context_compaction": context_compaction,
        "function_calling": function_calling,
        "messages": messages,
    }


def tool_answer(branch: Branch, name: str, *, call_id: str = "call-1", before: str = "Let me run it.",
                after: str = "It works.") -> Dict[str, Any]:
    """An assistant answer that called ``name`` once (stored as output items)."""
    output = [out_text(before), out_call(call_id, name), out_result(call_id), out_text(after)]
    return branch.assistant(before + "\n" + after, output=output)
