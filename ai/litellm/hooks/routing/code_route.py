"""THE CODE-TOOL REMAP (run from Handler.async_pre_call_hook). The semantic
router reads only the latest user message and cannot see tool NAMES, so a
coding chat whose latest turn is "Looks the same" lands on local-general.
When the conversation in a request for ``local-general`` contains a CALL to
a tool whose function name matches CODE_ROUTE_TOOL_REGEX in full (default:
the sandbox MCP tools, ``sandbox-mcp_*``, and Open Terminal's distinctive
operationIds such as ``run_command``), the hook rewrites ``data["model"]`` to
``local-code``. Tools that are merely offered (``data["tools"]``) do not
count: a chat with the sandbox attached but never used stays on
local-general. The history is split into EXCHANGES, one per user message; an
exchange is relevant when the model called such a tool during it or its user
message matches the semantic router's own ``code_kw`` signal (read from the
router's config.yaml, CODE_ROUTE_ROUTER_CONFIG). The remap holds while the
current exchange is relevant or fewer than CODE_ROUTE_COOLDOWN (default 3)
irrelevant exchanges in a row followed the last relevant one; after that the
chat drops back to local-general until it looks like code again.

The proxy routes on the rewritten model (pre_call_hook -> route_request in
common_request_processing.py) and the Router sets metadata.model_group from
it, so the overflow filter, the spend log and the `auto` footer all see
local-code. Only ``local-general`` is ever remapped -- never local-private
(PII routing stays the router's decision), local-reasoning, local-code
itself, ``auto`` or a concrete alias. One ``[route] local-general ->
local-code (<reason>)`` line per remap (throttled like the spill lines). An
empty CODE_ROUTE_TOOL_REGEX disables it; an invalid one is logged and
disables it. See ai/litellm/LITELLM.md > local-code: what it adds.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from common.conversation import called_tool_names, split_exchanges
from common.keywordmatch import KeywordRule, compile_keyword_rule, find_keyword_rule

from ..log import ThrottledLog, logger

CODE_ROUTE_FROM = "local-general"
CODE_ROUTE_TO = "local-code"
# Tool names as Open WebUI v0.11.4 presents them to the model:
#   * the sandbox MCP server's tools carry the server-id prefix `sandbox-mcp_`
#     (sandbox-mcp_run, sandbox-mcp_write_files, sandbox-mcp_exec, ...);
#   * Open Terminal's tools are its OpenAPI operationIds, UNPREFIXED --
#     utils/tools.py::get_terminal_tools uses `function_name = spec['name']`,
#     and convert_openapi_to_tool_payload sets name = operationId. The
#     open-terminal 0.13.0 operationIds in the schema are run_command,
#     get_process_status, send_process_input, kill_process, list_processes,
#     replace_file_content, write_file, read_file, display_file, list_files,
#     grep_search, glob_search, match_files, search_files, get_info,
#     read_user_terminal, send_user_terminal_input. Only the ones no other
#     tool server is likely to share are listed; run_command is always
#     present when a terminal is attached.
#
# The pattern must match the WHOLE tool name (re.fullmatch), so it needs no
# `^...$` anchors -- and a `$` in .env would have to be written `$$`. The
# same string is the default in docker-compose.litellm.yml and .env.example;
# unit-tests/litellm/test_code_route.py fails if the three drift.
DEFAULT_CODE_ROUTE_TOOL_REGEX = (
    "sandbox-mcp_.+|run_command|get_process_status|send_process_input"
    "|kill_process|replace_file_content|read_user_terminal|send_user_terminal_input"
)

# A chat stays on local-code while its recent EXCHANGES (one per user message:
# the message plus every model round it started) keep looking like code, and
# drops back to local-general after CODE_ROUTE_COOLDOWN irrelevant exchanges in
# a row. An exchange is relevant when its user message matches the semantic
# router's own `code_kw` signal (read from the router's config.yaml, mounted
# read-only, so the two can never disagree) or when the model called a tool
# CODE_ROUTE_TOOL_REGEX matches during it. Everything is recomputed from the
# request's history: no per-chat state, so both swap copies and a restart agree.
DEFAULT_CODE_ROUTE_COOLDOWN = 3
DEFAULT_CODE_ROUTE_ROUTER_CONFIG = "/app/semantic-router-config.yaml"
CODE_ROUTE_SIGNAL = "code_kw"


# -- Pure helpers (unit-tested) -------------------------------------------------
def compile_code_route_regex(raw: Optional[str]) -> Tuple[Optional["re.Pattern[str]"], str]:
    """(compiled pattern or None, error). Unset / empty / whitespace disables
    the remap; an invalid pattern disables it too and returns the error to log."""
    if raw is None or not raw.strip():
        return None, ""
    try:
        return re.compile(raw.strip()), ""
    except re.error as exc:
        return None, f"invalid CODE_ROUTE_TOOL_REGEX {raw!r}: {exc}"


def code_tool_match(names: Any, pattern: Any) -> Optional[str]:
    """The first of `names` that `pattern` matches in FULL (re.fullmatch),
    else None. `pattern` may be a compiled regex, a string, or None (=
    disabled)."""
    if pattern is None or not isinstance(names, list):
        return None
    if isinstance(pattern, str):
        pattern, _ = compile_code_route_regex(pattern)
        if pattern is None:
            return None
    for name in names:
        if isinstance(name, str) and pattern.fullmatch(name):
            return name
    return None


def parse_code_route_cooldown(raw: Any) -> Tuple[int, str]:
    """(cooldown, error). Unset / empty -> the default; a non-negative int is
    used as is (0: only the current exchange counts); anything else -> the
    default plus an error to log."""
    if raw is None or not str(raw).strip():
        return DEFAULT_CODE_ROUTE_COOLDOWN, ""
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = -1
    if value < 0:
        return DEFAULT_CODE_ROUTE_COOLDOWN, (
            f"invalid CODE_ROUTE_COOLDOWN {raw!r}: expected an integer >= 0, using {DEFAULT_CODE_ROUTE_COOLDOWN}"
        )
    return value, ""


def load_code_text_signal(path: Optional[str], name: str = CODE_ROUTE_SIGNAL) -> Tuple[Optional[KeywordRule], str]:
    """(rule, error) from the router's config.yaml: the keyword rule `name`,
    compiled the way the Go router does (common.keywordmatch). Missing file,
    missing rule or a bad pattern -> (None, error): the cooldown then judges
    exchanges by tool calls only."""
    if not path:
        return None, "CODE_ROUTE_ROUTER_CONFIG is empty"
    try:
        import yaml  # LiteLLM's own dependency (the proxy loads config.yaml with it)

        with open(path, "r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
        rule = find_keyword_rule(config, name)
        if rule is None:
            return None, f"no keyword signal {name!r} in {path}"
        return compile_keyword_rule(rule), ""
    except Exception as exc:
        return None, f"cannot load the router's {name!r} signal from {path}: {exc}"


def exchange_relevance(text: str, rounds: List[dict], tool_pattern: Any,
                       text_signal: Optional[KeywordRule]) -> Optional[str]:
    """Why one exchange counts as code, or None: a matching tool call during
    it (the most recent one is named) or its user message matching code_kw."""
    tool = code_tool_match(called_tool_names(rounds), tool_pattern)
    if tool is not None:
        return f"called {tool}"
    if text_signal is not None and text_signal.matches(text):
        return f"message matches {CODE_ROUTE_SIGNAL}"
    return None


def code_route_decision(messages: Any, tool_pattern: Any, text_signal: Optional[KeywordRule],
                        cooldown: int) -> Tuple[bool, str]:
    """(stay on / move to local-code?, reason). The current exchange (the last
    user message and the rounds so far) is relevant -> True. Otherwise count
    the irrelevant exchanges in a row before it: True while there was a
    relevant one and fewer than `cooldown` irrelevant ones since; False once
    the cooldown has run out (the reason says so) or when the chat has never
    looked like code (empty reason)."""
    exchanges = split_exchanges(messages)
    if not exchanges:
        return False, ""
    current = exchange_relevance(*exchanges[-1], tool_pattern, text_signal)
    if current is not None:
        return True, f"{current} in this exchange"
    irrelevant = 0
    for text, rounds in reversed(exchanges[:-1]):
        why = exchange_relevance(text, rounds, tool_pattern, text_signal)
        if why is not None:
            if irrelevant < cooldown:
                return True, f"{why} {irrelevant + 1} exchange(s) ago; {irrelevant}/{cooldown} irrelevant since"
            return False, f"cooldown: {irrelevant}/{cooldown} irrelevant exchanges since the last one that {why}"
        irrelevant += 1
    return False, ""


# -- The step -------------------------------------------------------------------
class CodeRoute:
    """local-general -> local-code while the chat looks like code.

    `regex` None disables the remap; `text_signal` None judges exchanges by
    tool calls only."""

    def __init__(
        self,
        regex: Any,
        cooldown: int,
        text_signal: Optional[KeywordRule],
        stats: Dict[str, int],
        log: ThrottledLog,
    ) -> None:
        self.regex = regex
        self.cooldown = cooldown
        self.text_signal = text_signal
        self.stats = stats
        self._log = log

    def apply(self, data: Any) -> bool:
        """local-general -> local-code while the chat's recent exchanges look
        like code (code_route_decision: a CODE_ROUTE_TOOL_REGEX tool call or a
        code_kw message, with a cooldown of CODE_ROUTE_COOLDOWN irrelevant
        exchanges). True when data["model"] was rewritten. Never raises: on
        any error `data` is left unchanged and the error is logged."""
        try:
            if self.regex is None or not isinstance(data, dict):
                return False
            if data.get("model") != CODE_ROUTE_FROM:
                return False
            remap, reason = code_route_decision(data.get("messages"), self.regex, self.text_signal, self.cooldown)
            if not remap:
                if reason:  # the chat WAS code; the cooldown has run out
                    self.stats["code_route_cooldowns"] += 1
                    self._log(CODE_ROUTE_FROM, "cooldown", "route", "[route] %s stays (%s)",
                              CODE_ROUTE_FROM, reason)
                return False
        except Exception as exc:
            try:
                self.stats["code_route_errors"] += 1
                logger.warning("[route] remap skipped after an error, request unchanged: %r", exc)
            except Exception:
                pass
            return False
        # The rewrite is the last thing that can fail, so an error above
        # always leaves the request exactly as it came in.
        data["model"] = CODE_ROUTE_TO
        self.stats["code_routes"] += 1
        try:
            kind = "current" if reason.endswith("in this exchange") else "earlier"
            self._log(CODE_ROUTE_FROM, kind, "route", "[route] %s -> %s (%s)",
                      CODE_ROUTE_FROM, CODE_ROUTE_TO, reason)
        except Exception:
            pass
        return True
