"""Unit tests for Open WebUI patch 0005 (X-OpenWebUI-Message-Id to LiteLLM).

The patched code only exists inside
``ai/openwebui/patches/0005-forward-message-id-header.patch`` (it is applied to
upstream's ``routers/openai.py`` at image build time). The patch is generated
with four lines of context, so its second hunk carries the WHOLE
``if ENABLE_FORWARD_USER_INFO_HEADERS and user:`` block of
``get_headers_and_cookies``. These tests cut that block out of the hunk, check
where the new line sits, and run it — they always exercise exactly what ships.

    uv run --no-sync pytest unit-tests/openwebui -q
"""

from __future__ import annotations

import ast
import pathlib
import re
import textwrap

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_OWUI = _ROOT / "ai" / "openwebui"
_PATCH_DIR = _OWUI / "patches"
_PATCH = _PATCH_DIR / "0005-forward-message-id-header.patch"
_OPENAI_PATH = "backend/open_webui/routers/openai.py"
_GUARD = "    if ENABLE_FORWARD_USER_INFO_HEADERS and user:"

CHAT_HEADER = "X-OpenWebUI-Chat-Id"  # env.py FORWARD_SESSION_INFO_HEADER_CHAT_ID default
MESSAGE_HEADER = "X-OpenWebUI-Message-Id"  # env.py FORWARD_SESSION_INFO_HEADER_MESSAGE_ID default
USER_HEADER = "X-OpenWebUI-User-Id"


def _file_hunks(patch_text: str) -> dict[str, list[list[str]]]:
    """Map each ``+++ b/<path>`` of a unified diff to its hunks (lines after each ``@@``)."""
    files: dict[str, list[list[str]]] = {}
    current = None
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            current = files.setdefault(line[len("+++ b/") :].split("\t")[0], [])
        elif line.startswith("--- "):
            current = None
        elif current is not None and line.startswith("@@"):
            current.append([])
        elif current is not None and current:
            current[-1].append(line)
    return files


def _new_side(hunk: list[str]) -> list[str]:
    return [line[1:] for line in hunk if line[:1] in (" ", "+") or line == ""]


_PATCH_TEXT = _PATCH.read_text(encoding="utf-8")
_HUNKS = _file_hunks(_PATCH_TEXT)


def _block_hunk() -> list[str]:
    matches = [h for h in _HUNKS.get(_OPENAI_PATH, []) if any(line == " " + _GUARD for line in h)]
    assert len(matches) == 1, "expected exactly one hunk carrying the user-info guard as context"
    return matches[0]


def _guard_block() -> str:
    """The new side of the ``if ENABLE_FORWARD_USER_INFO_HEADERS and user:`` statement, dedented."""
    new = _new_side(_block_hunk())
    start = new.index(_GUARD)
    block = [new[start]]
    for line in new[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= 4:
            break
        block.append(line)
    while block and not block[-1].strip():
        block.pop()
    return textwrap.dedent("\n".join(block)) + "\n"


def _run(*, enabled=True, user="u-1", metadata=None) -> dict:
    """Execute the patched block the way get_headers_and_cookies does."""

    def include_user_info_headers(headers, user, request=None):
        return {**headers, USER_HEADER: user}

    namespace = {
        "ENABLE_FORWARD_USER_INFO_HEADERS": enabled,
        "FORWARD_SESSION_INFO_HEADER_CHAT_ID": CHAT_HEADER,
        "FORWARD_SESSION_INFO_HEADER_MESSAGE_ID": MESSAGE_HEADER,
        "include_user_info_headers": include_user_info_headers,
        "headers": {"Content-Type": "application/json"},
        "user": user,
        "metadata": metadata,
        "request": object(),
    }
    exec(compile(_guard_block(), str(_PATCH) + "::get_headers_and_cookies", "exec"), namespace)
    return namespace["headers"]


# --- the patch's shape --------------------------------------------------------


def test_patch_only_touches_routers_openai():
    assert set(_HUNKS) == {_OPENAI_PATH}
    assert "--- a/" + _OPENAI_PATH in _PATCH_TEXT.splitlines()


def test_import_hunk_adds_the_message_id_constant_to_the_env_import():
    added = [line for hunk in _HUNKS[_OPENAI_PATH] for line in hunk if line.startswith("+")]
    assert "+    FORWARD_SESSION_INFO_HEADER_MESSAGE_ID," in added
    import_hunk = next(h for h in _HUNKS[_OPENAI_PATH] if "+    FORWARD_SESSION_INFO_HEADER_MESSAGE_ID," in h)
    new = _new_side(import_hunk)
    i = new.index("    FORWARD_SESSION_INFO_HEADER_MESSAGE_ID,")
    # Same parenthesised `from open_webui.env import (...)` list as the chat-id
    # constant and the flag it is guarded by.
    assert new[i - 1] == "    FORWARD_SESSION_INFO_HEADER_CHAT_ID,"
    assert "    ENABLE_FORWARD_USER_INFO_HEADERS," in new[:i]
    assert ")" in new[i + 1 :]


def test_message_id_line_is_inside_the_user_info_guard():
    tree = ast.parse(_guard_block())
    (outer,) = tree.body
    assert isinstance(outer, ast.If)
    assert ast.unparse(outer.test) == "ENABLE_FORWARD_USER_INFO_HEADERS and user"
    inner = [node for node in outer.body if isinstance(node, ast.If)]
    tests = [ast.unparse(node.test) for node in inner]
    assert tests == ["metadata and metadata.get('chat_id')", "metadata and metadata.get('message_id')"]
    (assign,) = inner[1].body
    assert ast.unparse(assign) == "headers[FORWARD_SESSION_INFO_HEADER_MESSAGE_ID] = str(metadata['message_id'])"


def test_patched_line_is_the_one_the_dockerfile_greps_for():
    dockerfile = (_OWUI / "Dockerfile.openwebui").read_text(encoding="utf-8")
    m = re.search(r'grep -qF "([^"]+)" ' + re.escape(_OPENAI_PATH), dockerfile)
    assert m, "Dockerfile.openwebui must grep routers/openai.py for the 0005 line"
    hunk = _block_hunk()
    assert any(line.startswith("+") and m.group(1) in line for line in hunk)
    assert not any(line.startswith(" ") and m.group(1) in line for line in hunk), "the guard must not match upstream"


# --- what the patched block does ----------------------------------------------


def test_chat_turn_sends_both_ids():
    headers = _run(metadata={"chat_id": "chat-1", "message_id": "msg-1"})
    assert headers[USER_HEADER] == "u-1"
    assert headers[CHAT_HEADER] == "chat-1"
    assert headers[MESSAGE_HEADER] == "msg-1"


def test_task_call_has_a_chat_id_but_no_message_id():
    headers = _run(metadata={"chat_id": "chat-1", "task": "title_generation"})
    assert headers[CHAT_HEADER] == "chat-1"
    assert MESSAGE_HEADER not in headers


def test_direct_api_call_with_an_id_but_no_chat():
    headers = _run(metadata={"chat_id": "", "message_id": "req-7"})
    assert CHAT_HEADER not in headers
    assert headers[MESSAGE_HEADER] == "req-7"


@pytest.mark.parametrize("metadata", [None, {}, {"message_id": None}, {"message_id": ""}])
def test_no_metadata_or_no_message_id_sends_no_message_header(metadata):
    # get_headers_and_cookies is called without metadata for the model list,
    # embeddings, responses and the proxy route.
    headers = _run(metadata=metadata)
    assert headers[USER_HEADER] == "u-1"
    assert MESSAGE_HEADER not in headers and CHAT_HEADER not in headers


def test_non_string_message_id_is_sent_as_a_string():
    assert _run(metadata={"message_id": 123})[MESSAGE_HEADER] == "123"


@pytest.mark.parametrize("enabled, user", [(False, "u-1"), (True, None)])
def test_flag_off_or_no_user_sends_nothing(enabled, user):
    headers = _run(enabled=enabled, user=user, metadata={"chat_id": "chat-1", "message_id": "msg-1"})
    assert headers == {"Content-Type": "application/json"}


# --- wiring -------------------------------------------------------------------


def test_dockerfile_lists_every_patch():
    dockerfile = (_OWUI / "Dockerfile.openwebui").read_text(encoding="utf-8")
    stems = [p.stem for p in sorted(_PATCH_DIR.glob("*.patch"))]
    label = re.search(r'com\.zeoenergy\.openwebui\.patches="([^"]*)"', dockerfile)
    assert label and label.group(1).split(",") == stems
    for stem in stems:
        assert f"#   {stem}.patch" in dockerfile, f"{stem} missing from the Dockerfile header comment"
    assert f"        {_OPENAI_PATH}" in dockerfile, "routers/openai.py missing from the ast.parse list"


def test_compose_and_env_example_wire_the_flag():
    compose = (_OWUI / "docker-compose.openwebui.yml").read_text(encoding="utf-8")
    assert (
        'ENABLE_FORWARD_USER_INFO_HEADERS: "${OPENWEBUI_ENABLE_FORWARD_USER_INFO_HEADERS:-false}"' in compose
    )
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    values = re.findall(r"^OPENWEBUI_ENABLE_FORWARD_USER_INFO_HEADERS=(.*)$", env_example, re.M)
    # env.py turns the flag on only for the literal "true" (lower-cased).
    assert [v.strip() for v in values] in (["true"], ["false"])
