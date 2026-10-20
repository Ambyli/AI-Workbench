"""The `default` ruleset and its pattern files -- the only copy of the rules
that route `auto`.

* every ``rulesets/patterns/*.yaml`` is a valid keyword rule, and every
  ``method: regex`` keyword stays in the RE2 subset (no lookaround,
  backreferences, atomic / possessive groups or ``\\Z``) and compiles the way
  ``common.keywordmatch`` compiles it -- "(?i)" prepended unless case
  sensitive, ASCII ``\\b`` / ``\\w``. Python's ``re`` accepts more than RE2,
  so this check is what keeps the patterns portable;
* real prompts from the "Scientific Calculator" chat match ``code_kw`` (or
  don't) -- both through the RE2 stand-in and through the rule the engine
  really runs;
* the ruleset's wiring (version, mode, the turn pin, evaluator order, roles,
  refs, the tool pattern, the cooldown);
* fixed routes on conversation shapes: what an ``auto`` turn is sent to --
  and, with the turn's first decision on the context, what its later calls
  (tool rounds, Open WebUI's hidden auto-continue nudge) are sent to.

Changing a pattern or the wiring is a ruleset change: update the expected
values here and bump ``default.yaml``'s ``version`` (ROUTE_POLICY.md §
Changing a ruleset).
"""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest
import yaml

from rp_helpers import NUDGE, RULESETS_DIR, assistant, context, default_ruleset, run, system, tool_round, user

from common.keywordmatch import compile_keyword_rule
from route_policy.engine.pipeline import run as run_pipeline
from route_policy.evaluators.regex_text import RegexTextConfig

PATTERN_FILES = sorted((RULESETS_DIR / "patterns").glob("*.yaml"))
RULE_KEYS = {"name", "operator", "case_sensitive", "method", "keywords"}

# From the "Scientific Calculator" chat (2026-10). These turns are why
# code_kw became regexes: whole-word literals missed "functions", "JS" and
# "Make me a ... calculator".
SHOULD_MATCH = [
    "Make me a scientific calculator!",
    "there should be a test for each function",
    "the same functions the calculator uses.",
    "move everything into a different environment so you can run a headless JS test then",
    # A few more shapes code_kw is meant for.
    "Why does app.py raise a KeyError here?",
    "```python\nprint(1)\n```",
    "Can you refactor this class into two modules?",
    "Build a small dashboard app for the sales numbers",
    "I get a stack trace when I run docker compose up",
]
SHOULD_NOT_MATCH = [
    "How can I determine how far the earth is to the nearest star",
    "Looks the same",
    "What is the capital of France?",
    "Summarise this email for me, please.",
]

# Syntax RE2 does not support (https://github.com/google/re2/wiki/Syntax):
# lookahead / lookbehind, backreferences (numbered and named), atomic groups,
# possessive quantifiers, and Python's \Z (RE2 spells it \z).
RE2_UNSUPPORTED = [
    (re.compile(r"\(\?<?[=!]"), "lookaround"),
    (re.compile(r"\\[1-9]"), "numbered backreference"),
    (re.compile(r"\(\?P="), "named backreference"),
    (re.compile(r"\\k<"), "named backreference"),
    (re.compile(r"\(\?>"), "atomic group"),
    (re.compile(r"[*+?}]\+"), "possessive quantifier"),
    (re.compile(r"\\Z"), "\\Z (RE2 has \\z)"),
]


def _rule(stem: str) -> dict:
    return yaml.safe_load((RULESETS_DIR / "patterns" / f"{stem}.yaml").read_text(encoding="utf-8"))


def _re2_patterns(rule: dict) -> list:
    """Each keyword of a regex rule as RE2 compiles it -- Python's re with
    re.ASCII stands in (both read \\b and \\w as ASCII; the patterns use
    nothing where the two engines differ)."""
    prefix = "" if rule.get("case_sensitive") else "(?i)"
    return [re.compile(prefix + k, re.ASCII) for k in rule["keywords"]]


def _regex_keywords():
    out = []
    for path in PATTERN_FILES:
        rule = yaml.safe_load(path.read_text(encoding="utf-8"))
        if rule.get("method") == "regex":
            out += [pytest.param(k, id=f"{path.stem}[{i}]") for i, k in enumerate(rule["keywords"])]
    return out


# -- the pattern files ----------------------------------------------------------------


def test_the_pattern_files_are_the_three_the_ruleset_uses():
    assert [p.stem for p in PATTERN_FILES] == ["code", "privacy", "reasoning"]


@pytest.mark.parametrize("path", PATTERN_FILES, ids=lambda p: p.stem)
def test_pattern_file_is_a_valid_keyword_rule(path):
    rule = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(rule) <= RULE_KEYS, f"{path.name}: unknown keys {set(rule) - RULE_KEYS}"
    assert rule["name"] == f"{path.stem}_kw"
    assert rule["keywords"] and all(isinstance(k, str) and k for k in rule["keywords"])
    # A double-quoted YAML string turns "\b" into a backspace; patterns must
    # be single-quoted so the backslash survives.
    for k in rule["keywords"]:
        assert not any(ord(c) < 0x20 for c in k), f"control character in {k!r}"
    RegexTextConfig(rule=rule)            # the evaluator's own validation
    compile_keyword_rule(rule)            # what it runs


def test_code_kw_is_a_case_insensitive_regex_rule():
    rule = _rule("code")
    assert (rule["method"], rule["case_sensitive"], rule["operator"]) == ("regex", False, "OR")


def test_privacy_and_reasoning_are_whole_word_literals():
    for stem in ("privacy", "reasoning"):
        rule = _rule(stem)
        assert "method" not in rule and rule["case_sensitive"] is False and rule["operator"] == "OR", stem


@pytest.mark.parametrize("pattern", _regex_keywords())
def test_regex_pattern_is_re2_compatible(pattern):
    for detector, what in RE2_UNSUPPORTED:
        assert not detector.search(pattern), f"{what} is not RE2 syntax: {pattern!r}"
    re.compile("(?i)" + pattern, re.ASCII)


@pytest.mark.parametrize("text", SHOULD_MATCH)
def test_code_prompts_match(text):
    rule = _rule("code")
    assert any(p.search(text) for p in _re2_patterns(rule)), f"code_kw (RE2) should match {text!r}"
    assert compile_keyword_rule(rule).matches(text), f"code_kw should match {text!r}"


@pytest.mark.parametrize("text", SHOULD_NOT_MATCH)
def test_non_code_prompts_do_not_match(text):
    rule = _rule("code")
    hits = [m.group(0) for p in _re2_patterns(rule) for m in [p.search(text)] if m]
    assert not hits, f"code_kw matched {hits!r} in {text!r}"
    assert not compile_keyword_rule(rule).matches(text)


# -- the ruleset ------------------------------------------------------------------------


def test_default_ruleset_wiring():
    ruleset = default_ruleset()
    assert (ruleset.id, ruleset.version, ruleset.mode, ruleset.pin) == ("default", 3, "enforce", "turn")
    assert ruleset.routes == ["local-general", "local-code", "local-reasoning", "local-private"]
    assert ruleset.default_route == "local-general"
    assert ruleset.document["combiner"] == {"type": "priority"}
    entries = ruleset.entries
    assert [(e.id, e.type, e.role, e.route) for e in entries] == [
        ("privacy", "regex_text", "force", "local-private"),
        ("code-text", "regex_text", "vote", "local-code"),
        ("reasoning-text", "regex_text", "vote", "local-reasoning"),
        ("code-tools", "tool_calls", "vote", "local-code"),
        ("sticky-code", "cooldown", "vote", "local-code"),
    ]
    assert [e.evaluator.rule_spec.name for e in entries[:3]] == ["privacy_kw", "code_kw", "reasoning_kw"]
    refs = [e["config"].get("patterns_ref") for e in ruleset.document["evaluators"][:3]]
    assert refs == ["patterns/privacy.yaml", "patterns/code.yaml", "patterns/reasoning.yaml"]
    tools = entries[3].evaluator.config
    assert tools.pattern == ("sandbox-mcp_.+|run_command|get_process_status|send_process_input|kill_process"
                             "|replace_file_content|read_user_terminal|send_user_terminal_input")
    assert tools.scope == "current_exchange"
    cooldown = entries[4].evaluator.config
    # Judges asked in this order: tool calls first, then the code keywords.
    assert (cooldown.exchanges, cooldown.relevant_if) == (2, ["code-tools", "code-text"])


PLAIN = [user("what's the weather like"), assistant("sunny")]
CODING = [user("start"), *tool_round("sandbox-mcp_write_files")]
# name -> (conversation, the route an `auto` turn gets)
CONVERSATIONS = {
    "small talk": ([system(), user("hello there")], "local-general"),
    "code keywords": ([user("Make me a scientific calculator!")], "local-code"),
    "code fence": ([user("```python\nprint(1)\n```")], "local-code"),
    "file name": ([user("why does app.py fail")], "local-code"),
    "reasoning": ([user("prove that the integral converges")], "local-reasoning"),
    "code beats reasoning": ([user("write a python function to solve this equation")], "local-code"),
    "privacy beats code": ([user("store the customer address in a python dict")], "local-private"),
    "privacy beats tool history": (CODING + [user("now add the credit card field")], "local-private"),
    "reasoning beats tool history": (CODING + [user("derive the formula step by step")], "local-reasoning"),
    "tool call this exchange": ([user("go ahead"), *tool_round("run_command")], "local-code"),
    "tool call 1 ago": (CODING + [user("Looks the same")], "local-code"),
    "tool call 2 ago": (CODING + PLAIN + [user("ok")], "local-code"),
    "tool call 3 ago": (CODING + PLAIN + PLAIN + [user("ok")], "local-general"),   # the 2-exchange cooldown ran out
    "tool call 4 ago": (CODING + PLAIN * 3 + [user("ok")], "local-general"),
    "code text 2 ago": ([user("debug this traceback"), assistant()] + PLAIN + [user("thanks")], "local-code"),
    "code text 3 ago": ([user("debug this traceback"), assistant()] + PLAIN * 2 + [user("thanks")], "local-general"),
    # The 2026-10 chat: a calculator, then the weather. The first weather
    # question is still held (1 irrelevant since < 2), the next one is not.
    "calculator then weather": ([user("Make me a scientific calculator!"), assistant(),
                                 user("check the weather in Utah")], "local-code"),
    "calculator then weather twice": ([user("Make me a scientific calculator!"), assistant(),
                                       user("check the weather in Utah"), assistant("sunny"),
                                       user("and in Denver?")], "local-code"),
    "calculator then weather three times": ([user("Make me a scientific calculator!"), assistant(),
                                             user("check the weather in Utah"), assistant("sunny"),
                                             user("and in Denver?"), assistant("snow"),
                                             user("and Boise?")], "local-general"),
    "unrelated tool": ([user("look it up"), *tool_round("search_web"), user("and?")], "local-general"),
    "offered tools only": ([user("hi")], "local-general"),
    "no user message": ([system("you are a bot")], "local-general"),
    "multimodal": ([user([{"type": "text", "text": "refactor this class"},
                          {"type": "image_url", "image_url": {"url": "data:,"}}])], "local-code"),
    "phrase literal": ([user("show your work please")], "local-reasoning"),
    "whole word literal": ([user("I love physics")], "local-reasoning"),
    "longer word is not the literal": ([user("ask the physicists")], "local-general"),
}


@pytest.mark.parametrize("name", sorted(CONVERSATIONS))
def test_default_ruleset_routes(name):
    messages, expected = CONVERSATIONS[name]
    decision = run(default_ruleset(), messages)
    assert decision.route == expected, (name, decision.reason)
    assert decision.apply is True    # enforce, invariants held


def test_requested_private_is_never_applied_elsewhere():
    decision = run(default_ruleset(), [user("write a python script")], requested_model="local-private")
    assert decision.route == "local-private" and decision.apply is True and decision.overrides == ("private",)


# -- one route per turn ------------------------------------------------------------------

FIRST = uuid.UUID("0b1c2d3e-0000-4000-8000-000000000001")
CALCULATOR = [user("Make me a scientific calculator!"), assistant()]
WEATHER = [user("check the weather in Utah")]
# name -> (a LATER call of the turn, the route the turn's first call got,
#          the route this call gets, the pin's outcome)
LATER_CALLS = {
    # The 2026-10 chat: the weather turn's first call was still on the
    # cooldown; the nudge, a new "exchange", would have expired it mid-answer.
    "nudge after a search": (CALCULATOR + [user("tell me a joke"), assistant()] + WEATHER
                             + tool_round("search_web") + [assistant("I'll look it up again."), user(NUDGE)],
                             "local-code", "local-code", "changed"),
    "tool round of a coding turn": (CALCULATOR + [user("now add tests"), *tool_round("sandbox-mcp_exec")],
                                    "local-code", "local-code", "kept"),
    "tool round of a weather turn": (WEATHER + tool_round("search_web"), "local-general", "local-general", "kept"),
    # A code tool called mid-turn does not move a general turn either.
    "code tool in a general turn": (WEATHER + tool_round("run_command"), "local-general", "local-general",
                                    "changed"),
    # Privacy always wins over a pin...
    "pii in the latest user message": (WEATHER + tool_round("search_web") + [user("my SSN is 123-45-6789")],
                                       "local-general", "local-private", "private"),
    # ...and a private turn stays private through a nudge that names no PII.
    "nudge in a private turn": ([user("my SSN is 123-45-6789, file my taxes"), *tool_round("search_web"),
                                 assistant("I'll file it."), user(NUDGE)],
                                "local-private", "local-private", "changed"),
}


@pytest.mark.parametrize("name", sorted(LATER_CALLS))
def test_later_calls_of_a_turn_keep_its_route(name):
    from route_policy.engine.context import TurnPin

    messages, first_route, expected, outcome = LATER_CALLS[name]
    ctx = context(messages)
    ctx.turn_pin = TurnPin(FIRST, first_route)
    decision = asyncio.run(run_pipeline(default_ruleset(), ctx))
    assert (decision.route, decision.pin_outcome, decision.pinned_from) == (expected, outcome, FIRST), \
        (name, decision.reason)
    assert decision.apply is True
    assert len(decision.votes) == 5   # every evaluator still ran and is logged
    fresh = run(default_ruleset(), messages)   # the same call as the turn's first
    assert fresh.pinned_from is None and fresh.pin_outcome is None
    if outcome == "changed":
        assert fresh.route != expected and decision.decided_by == "pin"
        assert decision.reason == (f"pinned to this turn's first decision ({first_route}, {FIRST}); "
                                   f"fresh: {fresh.route} — {fresh.reason}")
    elif outcome == "private":
        assert decision.reason == f"{fresh.reason}; local-private beats this turn's first decision ({first_route}, {FIRST})"
    else:
        assert decision.reason == fresh.reason and decision.decided_by == fresh.decided_by


# -- Open WebUI's RAG block ---------------------------------------------------------------

# Open WebUI v0.11.4's DEFAULT_RAG_TEMPLATE (backend/open_webui/config.py),
# verbatim. On a request carrying retrieved sources Open WebUI sends it,
# filled in, in FRONT of the user's message. 2026-10-20: its example sentence
# ("the proposed method increases efficiency") matched code_kw ("method"),
# so a weather turn's fetch rounds voted local-code.
OWUI_RAG_TEMPLATE = (
    '### Task:\n'
    'Respond to the user query using the provided context, incorporating inline citations in the format [id] **only when the <source> tag includes an explicit id attribute** (e.g., <source id="1">).\n'
    '\n'
    '### Guidelines:\n'
    "- If you don't know the answer, clearly state that.\n"
    '- If uncertain, ask the user for clarification.\n'
    "- Respond in the same language as the user's query.\n"
    '- If the context is unreadable or of poor quality, inform the user and provide the best possible answer.\n'
    "- If the answer isn't present in the context but you possess the knowledge, explain this to the user and provide the answer using your own understanding.\n"
    '- **Only include inline citations using [id] (e.g., [1], [2]) when the <source> tag includes an id attribute.**\n'
    '- Do not cite if the <source> tag does not contain an id attribute.\n'
    '- Do not use XML tags in your response.\n'
    '- Ensure citations are concise and directly related to the information provided.\n'
    '\n'
    '### Example of Citation:\n'
    'If the user asks about a specific topic and the information is found in a source with a provided id attribute, the response should include the citation like in the following example:\n'
    '* "According to the study, the proposed method increases efficiency by 20% [1]."\n'
    '\n'
    '### Output:\n'
    "Provide a clear and direct response to the user's query, including inline citations in the format [id] only when the <source> tag with id attribute is present in the context.\n"
    '\n'
    '<context>\n'
    '{{CONTEXT}}\n'
    '</context>\n'
)


def owui_wrap(own: str, context: str) -> str:
    return OWUI_RAG_TEMPLATE.replace("{{CONTEXT}}", context) + "\n" + own


def test_the_rag_template_alone_would_trip_code_kw():
    """Why the block must go: without the strip, the template's own words are
    a code_kw hit."""
    assert compile_keyword_rule(_rule("code")).matches(OWUI_RAG_TEMPLATE)


@pytest.mark.parametrize("context,expected", [
    # The template alone (a fetch round's citation markers).
    ('<source id="1" name="weather.gov"></source>', "local-general"),
    # A fetched page full of code and PII words.
    ('<source id="1">Our API endpoint docs: refactor the function, debug the traceback. '
     'Call me, my SSN is 123-45-6789.</source>', "local-general"),
])
def test_rules_read_the_users_words_not_open_webuis_block(context, expected):
    messages = [user("check the weather in Utah"), *tool_round("search_web"), *tool_round("fetch_url", "c2")]
    messages[0] = user(owui_wrap("check the weather in Utah", context))
    decision = run(default_ruleset(), messages)
    assert decision.route == expected, decision.reason
    code_text = next(v for v in decision.votes if v.evaluator == "code-text")
    assert code_text.detail["searched_chars"] == len("check the weather in Utah")


def test_a_wrapped_coding_request_still_routes_to_code():
    """The user's own words still count: "fix this function" stays a code turn
    with sources attached."""
    decision = run(default_ruleset(), [user(owui_wrap("fix this function", '<source id="1">x</source>'))])
    assert decision.route == "local-code", decision.reason
