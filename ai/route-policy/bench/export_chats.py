"""Export the current branch of every recent Open WebUI chat, read-only.

Runs INSIDE the ``openwebui`` container with its own interpreter -- stdlib
only, and nothing from the ``bench`` package -- and writes to STDOUT, so the
export lands on the host and never in the container's filesystem::

    docker exec -i openwebui python3 - --days 30 \\
        < ai/route-policy/bench/export_chats.py > ~/route-bench/chats.jsonl

(``python3 -`` reads the script from stdin; the arguments after ``-`` reach
``sys.argv``.) Progress goes to stderr. The output is CUSTOMER DATA: keep it
in a box directory outside the repo and delete it after use (BENCH.md).

**Read-only.** The database is opened with ``file:...?mode=ro`` and
``PRAGMA query_only``; the script runs SELECTs only. It never writes.

**What a chat's branch is** -- the same as Open WebUI 0.11.4's own replay
(``models/chats.py::get_messages_map_by_chat_id``, ``utils/misc.py::
get_message_list``):

* the message map comes from the normalised ``chat_message`` rows (ids
  ``<chat_id>-<message_id>``); when their parent links have gaps, the
  missing messages are taken from the legacy ``chat.chat.history.messages``
  JSON; with no rows at all, the legacy JSON is the map;
* the branch is the walk from the chat's current message
  (``chat.current_message_id``, else ``history.currentId``) up the
  ``parentId`` links, reversed -- root first. Other branches (regenerated
  answers, edited messages) are not exported.

**One line per chat** (``chats.jsonl``)::

    {"chat_id", "user_id", "created_at", "updated_at",    # seconds
     "current_message_id", "source": "chat_message" | "chat_message+history" | "history",
     "context_compaction": bool,       # Open WebUI's context compaction was on
     "function_calling": str | null,   # the chat's params.function_calling ("legacy" | "native" | null)
     "messages": [                     # the branch, root first
       {"id", "parentId", "role", "content", "output", "model", "timestamp",
        "done": bool, "error": bool, "files": [...], "context_summary": bool,
        "sources": int}]}

A message keeps exactly what the converter (``bench/convert.py``) needs to
rebuild what Open WebUI sent the model: ``content`` and the ``output`` items
(the assistant's text, tool calls and tool results), with every inline
``data:`` payload replaced by a short placeholder and every reasoning item
reduced to its type (reasoning is never replayed to the model); ``files``
keeps ``type`` / ``id`` / ``url`` / ``content_type`` / ``name`` (what Open
WebUI turns into image parts and ``<attached_files>`` tags);
``context_summary`` and ``sources`` are a flag and a count, not their text.
``error`` is whether the stored message carries an error.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

DEFAULT_DB = "/app/backend/data/webui.db"
# A data: URI shorter than this is left alone (it cannot be a real payload);
# the same floor LiteLLM's route-policy hook uses.
DATA_URI_MIN_CHARS = 128
FILE_KEYS = ("type", "id", "url", "content_type", "name")
# Output item keys that carry reasoning text; a reasoning item keeps its type only.
REASONING_KEYS_KEPT = ("type", "id")
MAX_BRANCH = 100000


def omitted(value):
    """A data: URI replaced by its header and its length."""
    head = value[: value.index(",") + 1] if "," in value[:256] else "data:"
    return f"{head}[omitted: {len(value)} chars]"


def strip_inline(obj):
    """``obj`` with every long ``data:`` string replaced (a deep copy)."""
    if isinstance(obj, str):
        return omitted(obj) if obj.startswith("data:") and len(obj) >= DATA_URI_MIN_CHARS else obj
    if isinstance(obj, list):
        return [strip_inline(v) for v in obj]
    if isinstance(obj, dict):
        return {k: strip_inline(v) for k, v in obj.items()}
    return obj


def clean_output(output):
    """The ``output`` items, inline payloads stripped, reasoning reduced to its type."""
    if not isinstance(output, list):
        return None
    items = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            items.append({k: item[k] for k in REASONING_KEYS_KEPT if k in item})
            continue
        items.append(strip_inline(item))
    return items


def clean_files(files):
    if not isinstance(files, list):
        return []
    out = []
    for f in files:
        if isinstance(f, dict):
            out.append(strip_inline({k: f[k] for k in FILE_KEYS if f.get(k) is not None}))
    return out


def loads(value, default=None):
    """A JSON column as Python (sqlite returns JSON columns as text)."""
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def columns(con, table):
    return [row[1] for row in con.execute(f"pragma table_info({table})")]


def compaction_enabled(con):
    """Whether Open WebUI's context compaction is on: the config row, else the
    env var the container started with (the row wins, as Config.get does --
    unless ENABLE_PERSISTENT_CONFIG is off, when only the env counts)."""
    persistent = os.environ.get("ENABLE_PERSISTENT_CONFIG", "True").lower() == "true"
    if persistent and "config" in tables(con) and {"key", "value"} <= set(columns(con, "config")):
        row = con.execute("select value from config where key = ?",
                          ("chat.context_compaction.enable",)).fetchone()
        if row is not None:
            value = loads(row[0])
            return value is True or (isinstance(value, str) and value.lower() == "true")
    return os.environ.get("ENABLE_CONTEXT_COMPACTION", "False").lower() == "true"


def tables(con):
    return {row[0] for row in con.execute("select name from sqlite_master where type = 'table'")}


ROW_KEYS = {  # chat_message column -> message key (Open WebUI's DB_TO_JSON_KEY_MAP)
    "role": "role", "parent_id": "parentId", "content": "content", "output": "output",
    "model_id": "model", "files": "files", "sources": "sources", "done": "done",
    "error": "error", "context_summary": "contextSummary", "created_at": "timestamp",
}
JSON_COLUMNS = {"content", "output", "files", "sources", "error"}


def row_messages(con, chat_id, available):
    """{message_id: message} from chat_message rows, or None when there are none."""
    wanted = [c for c in ROW_KEYS if c in available]
    rows = con.execute(f"select id, {', '.join(wanted)} from chat_message where chat_id = ?",
                       (chat_id,)).fetchall()
    if not rows:
        return None
    prefix = f"{chat_id}-"
    out = {}
    for row in rows:
        composite = row[0]
        msg_id = composite[len(prefix):] if composite.startswith(prefix) else composite
        msg = {"id": msg_id}
        for column, value in zip(wanted, row[1:]):
            if value is None:
                continue
            msg[ROW_KEYS[column]] = loads(value) if column in JSON_COLUMNS else value
        msg.setdefault("content", "")
        out[msg_id] = msg
    return out


def message_map(con, chat_id, chat, has_rows_table, available):
    """(map, source) the way Open WebUI 0.11.4 builds it."""
    history = (chat.get("history") or {}) if isinstance(chat, dict) else {}
    legacy = history.get("messages") or {}
    legacy = legacy if isinstance(legacy, dict) else {}
    rows = row_messages(con, chat_id, available) if has_rows_table else None
    if rows is None:
        return legacy, "history"
    missing = {m.get("parentId") for m in rows.values()
               if m.get("parentId") and m.get("parentId") not in rows}
    found = {mid: legacy[mid] for mid in missing if mid in legacy}
    if found:
        rows.update(found)
        return rows, "chat_message+history"
    return rows, "chat_message"


def branch(messages_map, current_id):
    """get_message_list: root .. current, following parentId, cycle-safe."""
    out, seen = [], set()
    msg_id = current_id
    while msg_id and msg_id not in seen and msg_id in messages_map and len(out) < MAX_BRANCH:
        seen.add(msg_id)
        msg = messages_map[msg_id]
        if not isinstance(msg, dict):
            break
        out.append((msg_id, msg))
        msg_id = msg.get("parentId")
    out.reverse()
    return out


def export_message(msg_id, msg):
    sources = msg.get("sources")
    return {
        "id": msg_id,
        "parentId": msg.get("parentId"),
        "role": msg.get("role"),
        "content": strip_inline(msg.get("content") if msg.get("content") is not None else ""),
        "output": clean_output(msg.get("output")),
        "model": msg.get("model"),
        "timestamp": msg.get("timestamp"),
        # A Boolean column comes back from sqlite as 0 / 1; unset = done.
        "done": msg.get("done") not in (False, 0),
        "error": bool(msg.get("error")),
        "files": clean_files(msg.get("files")) if msg.get("role") == "user" else [],
        "context_summary": bool(msg.get("contextSummary") or msg.get("context_summary")),
        "sources": len(sources) if isinstance(sources, list) else 0,
    }


def export(db_path, *, days, out, err, now=None, chat_ids=None):
    """Write one JSON line per chat updated in the last ``days`` days to ``out``
    (a binary stream); returns (chats, messages)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute("pragma query_only = on")
        names = tables(con)
        if "chat" not in names:
            raise SystemExit(f"{db_path}: no `chat` table -- not an Open WebUI database")
        chat_cols = set(columns(con, "chat"))
        has_rows_table = "chat_message" in names
        available = set(columns(con, "chat_message")) if has_rows_table else set()
        compaction = compaction_enabled(con)
        cutoff = int((now if now is not None else time.time()) - days * 86400)
        current_col = "current_message_id" if "current_message_id" in chat_cols else "null"
        query = f"select id, user_id, created_at, updated_at, {current_col}, chat from chat where "
        if chat_ids:  # named chats, whatever their age
            query += f"id in ({', '.join('?' for _ in chat_ids)})"
            params = list(chat_ids)
        else:
            query += "updated_at >= ?"
            params = [cutoff]
        query += " order by updated_at"
        chats = messages = skipped = 0
        # Iterated lazily (one chat's JSON in memory at a time); the per-chat
        # chat_message query runs on its own cursor.
        for chat_id, user_id, created_at, updated_at, current_col_id, raw in con.execute(query, params):
            chat = loads(raw, {})
            chat = chat if isinstance(chat, dict) else {}
            history = chat.get("history") or {}
            current_id = current_col_id or history.get("currentId") or chat.get("currentId")
            messages_map, source = message_map(con, chat_id, chat, has_rows_table, available)
            path = branch(messages_map, current_id)
            if not path:
                skipped += 1
                continue
            params_ = chat.get("params") if isinstance(chat.get("params"), dict) else {}
            record = {
                "chat_id": chat_id,
                "user_id": user_id,
                "created_at": created_at,
                "updated_at": updated_at,
                "current_message_id": current_id,
                "source": source,
                "context_compaction": compaction,
                "function_calling": params_.get("function_calling"),
                "messages": [export_message(mid, m) for mid, m in path],
            }
            out.write((json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii"))
            chats += 1
            messages += len(path)
            if chats % 100 == 0:
                print(f"export_chats: {chats} chats, {messages} messages so far", file=err, flush=True)
        window = f"{len(chat_ids)} named chat(s)" if chat_ids else f"chats updated in the last {days:g} days"
        print(f"export_chats: exported {chats} chats ({messages} messages) of {window}; "
              f"skipped {skipped} with an empty branch; context compaction {'on' if compaction else 'off'}",
              file=err, flush=True)
        return chats, messages
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="export_chats.py",
        description="Export the current branch of every recent Open WebUI chat as JSONL on stdout "
                    "(read-only; customer data -- write it outside the repo).")
    parser.add_argument("--days", type=float, default=30.0, help="chats updated in the last N days (default 30)")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Open WebUI sqlite database (default {DEFAULT_DB})")
    parser.add_argument("--chat-id", action="append", default=[],
                        help="only this chat, whatever its age (repeatable; --days is then ignored)")
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--days must be positive")
    export(args.db, days=args.days, out=sys.stdout.buffer, err=sys.stderr, chat_ids=args.chat_id or None)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
