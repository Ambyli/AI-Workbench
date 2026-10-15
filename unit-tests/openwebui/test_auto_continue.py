"""Unit tests for Open WebUI patch 0004 (auto-continue announced tool calls).

The module under test, ``backend/open_webui/utils/zeo_auto_continue.py``, only
exists inside ``ai/openwebui/patches/0004-auto-continue-announced-tool-calls.patch``
(it is created in the image at build time). So instead of keeping a copy that
could drift, these tests extract the module's source from the patch's
``/dev/null`` hunk and import that — they always exercise exactly what ships.

    uv run --no-sync pytest unit-tests/openwebui -q
"""

from __future__ import annotations

import pathlib
import re
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PATCH = _ROOT / "ai" / "openwebui" / "patches" / "0004-auto-continue-announced-tool-calls.patch"
_MODULE_PATH = "backend/open_webui/utils/zeo_auto_continue.py"
_MIDDLEWARE_PATH = "backend/open_webui/utils/middleware.py"
_LITELLM_DIR = _ROOT / "ai" / "litellm"


def _file_sections(patch_text: str) -> dict[str, list[str]]:
    """Map each ``+++ b/<path>`` in a unified diff to the hunk lines that follow it."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in patch_text.split("\n"):
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :].split("\t")[0]
            sections[current] = []
        elif line.startswith("--- ") and current is not None:
            current = None
        elif current is not None:
            sections[current].append(line)
    return sections


def _new_file_source(lines: list[str]) -> str:
    assert lines and lines[0].startswith("@@ -0,0 "), "expected a single /dev/null hunk"
    body = lines[1:]
    while body and body[-1] == "":
        body.pop()
    assert all(line.startswith("+") for line in body), "a new-file hunk has only + lines"
    return "\n".join(line[1:] for line in body) + "\n"


_PATCH_TEXT = _PATCH.read_text(encoding="utf-8")
_SECTIONS = _file_sections(_PATCH_TEXT)


def _load_module() -> types.ModuleType:
    source = _new_file_source(_SECTIONS[_MODULE_PATH])
    module = types.ModuleType("zeo_auto_continue")
    module.__file__ = str(_PATCH) + "::" + _MODULE_PATH
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


zac = _load_module()

FOOTER = "\n\n---\n*muse-glimmer · local-general*"

# The final text of three real consecutive turns on 2026-10-15 (muse-glimmer
# via `auto`). Each ended the turn without the tool call it announced.
ANNOUNCEMENTS = [
    "The regex escaping got mangled when writing the file. Let me fix the test harness and run it properly."
    "\n\nI'll rewrite the test file with correct JS strings and re-run.",
    "I paused because the last write produced a syntax error… I'm fixing it now and will re-run the headless tests."
    "\n\nGive me one moment to rewrite the test harness with correct escaping and run it.",
    "Permission denied on `/app/test.js` via exec. I'll use `write_files` to create the test harness correctly,"
    " then run it.",
]

# Real legitimate endings: a question to the user, and a finished-work summary.
LEGIT = [
    "Want me to add a proper `e` constant replacement to both the calculator and the tests so `ln(e)` passes,"
    " or keep the tests strictly mirroring the current behavior?",
    "You should now see the full 5×7 scientific pad, DEG/RAD toggle, expression line, result, history and"
    " memory indicator.",
]


def _turn(final_text: str, *, reasoning: bool = False) -> list[dict]:
    """An Open WebUI output list: one tool round, then the final reply."""
    items = [
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Running the tests."}],
        },
        {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}", "status": "completed"},
        {
            "type": "function_call_output",
            "call_id": "c1",
            "status": "failed",
            "output": [{"type": "input_text", "text": "Error: permission denied"}],
        },
    ]
    if reasoning:
        items.append({"type": "reasoning", "content": [{"type": "output_text", "text": "I should rewrite it."}]})
    items.append(
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": final_text}],
        }
    )
    return items


def _nudge(output, **overrides):
    kwargs = dict(nudges_used=0, tool_rounds=1, native=True, tools_offered=True, max_nudges=2)
    kwargs.update(overrides)
    return zac.should_nudge(output, **kwargs)


# ── the heuristic ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ANNOUNCEMENTS)
@pytest.mark.parametrize("footer", ["", FOOTER])
def test_real_announcements_trigger(text, footer):
    assert zac.looks_like_announced_action(text + footer)


@pytest.mark.parametrize("text", LEGIT)
@pytest.mark.parametrize("footer", ["", FOOTER])
def test_real_legit_endings_do_not_trigger(text, footer):
    assert not zac.looks_like_announced_action(text + footer)


@pytest.mark.parametrize("text", ["", "   \n\n ", FOOTER, FOOTER.strip()])
def test_empty_text_does_not_trigger(text):
    assert not zac.looks_like_announced_action(text)


@pytest.mark.parametrize(
    "text",
    [
        "The write failed again. I'll try a different path — or would you rather I use the session's /tmp?",
        "I'll keep going after this. Let me know if you want the old version back.",
        "If you'd like, I'll add a dark-mode toggle next.",
        "Done. Should I also update the README, or leave it?",
        "I can fix it now. Want me to?",
    ],
)
def test_question_or_offer_in_tail_does_not_trigger(text):
    assert not zac.looks_like_announced_action(text)


def test_question_in_an_earlier_paragraph_still_triggers():
    text = "Was the file there before? It was not.\n\nLet me create it and run the suite again."
    assert zac.looks_like_announced_action(text)


@pytest.mark.parametrize(
    "text",
    [
        "Now I'll patch `calc.js` and re-run the suite.",
        "I'm going to switch to `write_files` for this.",
        "I AM GOING TO REWRITE THE HARNESS.",
        "Let’s re-run the headless tests with the fixed escaping.",
        "I’ll rewrite the harness.",
        "Hold on, rewriting the harness with proper escaping.",
        "I'm rewriting the harness now.",
        "Next, I install the dependencies and run the build.",
    ],
)
def test_intent_variants_trigger(text):
    assert zac.looks_like_announced_action(text)


@pytest.mark.parametrize(
    "text",
    [
        # Bare imperatives are instructions to the user in a finished answer.
        "All tests pass. Re-run them any time with `npm test`.",
        "Save the file, then run it.",
        "The calculator is in `/app/index.html` and the preview is live.",
        # A question mark inside inline code is not a question.
        "Fixed the regex to `a?b` in the parser. All 12 tests pass.",
    ],
)
def test_finished_answers_do_not_trigger(text):
    assert not zac.looks_like_announced_action(text)


def test_only_the_last_two_sentences_of_the_last_paragraph_count():
    text = (
        "I'll start with the parser. The parser is fixed. The tests were updated. "
        "All 12 tests pass on the headless run."
    )
    assert not zac.looks_like_announced_action(text)


def test_code_block_with_question_mark_is_ignored():
    text = "Permission denied.\n\n```js\nconst re = /a?b/;\n```\n\nI'll write it with `write_files` instead."
    assert zac.looks_like_announced_action(text)


# ── the LiteLLM footer ────────────────────────────────────────────────────


def test_strip_footer_removes_overflow_footer_variants():
    for label in ("muse-glimmer · local-general", "claude-sonnet-5 · local-general · overflow (local busy)"):
        assert zac.strip_footer("Answer." + "\n\n---\n*" + label + "*") == "Answer."
    assert zac.strip_footer("Answer." + FOOTER + FOOTER) == "Answer."
    assert zac.strip_footer("A rule\n\n---\nnot a footer") == "A rule\n\n---\nnot a footer"


def test_footer_regex_matches_litellm_overflow():
    sys.path.insert(0, str(_LITELLM_DIR))
    try:
        overflow = pytest.importorskip("overflow")
    finally:
        sys.path.remove(str(_LITELLM_DIR))
    assert zac.FOOTER_RE.pattern == overflow.FOOTER_RE.pattern
    text = ANNOUNCEMENTS[2] + overflow.footer_text("muse-glimmer · local-general")
    assert zac.strip_footer(text) == ANNOUNCEMENTS[2]


# ── should_nudge: the gates around the heuristic ──────────────────────────


@pytest.mark.parametrize("text", ANNOUNCEMENTS)
def test_should_nudge_after_tool_round(text):
    assert _nudge(_turn(text + FOOTER)) == text + FOOTER


@pytest.mark.parametrize("text", LEGIT)
def test_should_not_nudge_legit_endings(text):
    assert _nudge(_turn(text + FOOTER)) is None


def test_reasoning_between_tool_result_and_reply_is_skipped():
    assert _nudge(_turn(ANNOUNCEMENTS[0], reasoning=True)) is not None


def test_reasoning_text_is_not_read():
    output = _turn("All 12 tests pass.")
    output.insert(-1, {"type": "reasoning", "content": [{"type": "output_text", "text": "Let me run it."}]})
    assert _nudge(output) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_rounds": 0},
        {"native": False},
        {"tools_offered": False},
        {"max_nudges": 0},
        {"nudges_used": 2},
        {"nudges_used": 1, "max_nudges": 1},
    ],
)
def test_gates_block_the_nudge(overrides):
    assert _nudge(_turn(ANNOUNCEMENTS[2]), **overrides) is None


def test_no_nudge_when_reply_did_not_follow_a_tool_result():
    output = [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ANNOUNCEMENTS[2]}]}
    ]
    assert _nudge(output) is None
    assert _nudge([]) is None
    assert _nudge(None) is None


def test_second_nudge_judges_the_continuation():
    """After a nudge, the output holds announcement + continuation; the latter decides."""
    output = _turn(ANNOUNCEMENTS[2])
    output.append(
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "One moment."}]}
    )
    assert _nudge(output, nudges_used=1) is not None
    output[-1]["content"][0]["text"] = "Actually the harness is already correct; all tests pass."
    assert _nudge(output, nudges_used=1) is None


def test_last_round_text_joins_trailing_messages():
    output = _turn("First.")
    output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Second."}]})
    assert zac.last_round_text(output) == ("First.\n\nSecond.", True)


# ── config + logging ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 2), ("", 2), ("  ", 2), ("0", 0), ("1", 1), ("5", 5), ("-3", 0), ("two", 2), (" 3 ", 3)],
)
def test_read_max_nudges(raw, expected):
    env = {} if raw is None else {"AUTO_CONTINUE_MAX_NUDGES": raw}
    assert zac.read_max_nudges(env) == expected


def test_describe_nudge_is_one_short_line():
    line = zac.describe_nudge("chat-123", 1, 2, ANNOUNCEMENTS[1] + FOOTER)
    assert line.startswith("auto-continue: chat chat-123 nudge 1/2: ")
    assert "\n" not in line and "muse-glimmer" not in line
    assert len(line) < 300
    assert zac.describe_nudge(None, 2, 2, "x").startswith("auto-continue: chat - nudge 2/2")


def test_nudge_text_offers_the_model_an_out():
    assert "tool call" in zac.NUDGE_TEXT
    assert "summary" in zac.NUDGE_TEXT and "ask the question" in zac.NUDGE_TEXT


# ── the middleware hunk only uses what the module provides ────────────────


def test_middleware_hunk_references_existing_module_api():
    added = "\n".join(line[1:] for line in _SECTIONS[_MIDDLEWARE_PATH] if line.startswith("+"))
    code = "\n".join(line for line in added.split("\n") if not line.lstrip().startswith("#"))
    used = set(re.findall(r"\bzeo_auto_continue\.([A-Za-z_]+)", code))
    assert used, "the hunk should call into the module"
    assert used <= set(dir(zac)), used - set(dir(zac))
    assert "while (tool_calls or await zeo_auto_continue_round())" in added
    # The nudge must stay out of `output` (what is shown and saved).
    assert "output.append" not in added
