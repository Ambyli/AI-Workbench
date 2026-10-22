"""bench.export_chats: the read-only export, on a fabricated Open WebUI database.

The sqlite file is built here with the 0.11.4 tables the export reads
(``chat``, ``chat_message``, ``config``), JSON columns stored as JSON text the
way SQLAlchemy writes them. Pins the branch walk (current branch only), the
map Open WebUI builds (rows; rows with gaps filled from the legacy JSON; the
legacy JSON alone), what each message keeps and strips, the day window, and
that the database file is untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3

import pytest

from bench import export_chats
from bench import points as points_mod
from bench.baseline import RuleBaseline

NOW = 1_760_100_000
DAY = 86_400
LONG_DATA = "data:image/png;base64," + "A" * 400


def j(value):
    return json.dumps(value)


def make_db(path, *, with_rows_table=True, with_current_column=True, compaction=None):
    con = sqlite3.connect(path)
    current = ", current_message_id TEXT" if with_current_column else ""
    con.execute(f"CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, chat JSON, "
                f"created_at BIGINT, updated_at BIGINT{current})")
    if with_rows_table:
        con.execute("CREATE TABLE chat_message (id TEXT PRIMARY KEY, chat_id TEXT, user_id TEXT, role TEXT, "
                    "parent_id TEXT, content JSON, output JSON, model_id TEXT, files JSON, sources JSON, "
                    "embeds JSON, meta JSON, done BOOLEAN, status_history JSON, error JSON, usage JSON, "
                    "context_summary TEXT, created_at BIGINT, updated_at BIGINT)")
    con.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value JSON)")
    if compaction is not None:
        con.execute("INSERT INTO config VALUES (?, ?)", ("chat.context_compaction.enable", j(compaction)))
    con.commit()
    return con


def add_chat(con, chat_id, *, updated_at=NOW - DAY, current=None, history=None, params=None, with_current=True):
    blob = {"title": "t", "history": {"messages": history or {}, "currentId": current}}
    if params is not None:
        blob["params"] = params
    if with_current:
        con.execute("INSERT INTO chat (id, user_id, title, chat, created_at, updated_at, current_message_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (chat_id, "user-1", "t", j(blob), updated_at - DAY, updated_at,
                                                     current))
    else:
        con.execute("INSERT INTO chat (id, user_id, title, chat, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, "user-1", "t", j(blob), updated_at - DAY, updated_at))


def add_row(con, chat_id, msg_id, role, parent, content, *, output=None, files=None, done=True, error=None,
            sources=None, ts=NOW - DAY, summary=None):
    con.execute("INSERT INTO chat_message (id, chat_id, user_id, role, parent_id, content, output, model_id, files, "
                "sources, done, error, context_summary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{chat_id}-{msg_id}", chat_id, "user-1", role, parent, j(content),
                 j(output) if output is not None else None, "auto" if role == "assistant" else None,
                 j(files) if files is not None else None, j(sources) if sources is not None else None,
                 1 if done else 0, j(error) if error is not None else None, summary, ts, ts))


def run_export(path, **kw):
    out, err = io.BytesIO(), io.StringIO()
    export_chats.export(str(path), days=kw.pop("days", 30), out=out, err=err, now=NOW, **kw)
    text = out.getvalue().decode("ascii")
    return [json.loads(line) for line in text.splitlines() if line], err.getvalue()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "webui.db"
    con = make_db(path, compaction=True)
    # chat-a: rows; the last user message has two answers, the second is current.
    add_chat(con, "chat-a", current="a2b")
    add_row(con, "chat-a", "u1", "user", None, "look at these",
            files=[{"type": "image", "url": LONG_DATA, "collection_name": "x"},
                   {"type": "file", "id": "f1", "url": "/api/v1/files/f1", "name": "data.json",
                    "content_type": "application/json", "size": 10, "status": "uploaded"}])
    add_row(con, "chat-a", "a1", "assistant", "u1", "Here.", sources=[{"s": 1}, {"s": 2}], output=[
        {"type": "reasoning", "id": "r1", "summary": [{"type": "output_text", "text": "secret thoughts"}],
         "reasoning_details": [{"x": 1}]},
        {"type": "function_call", "call_id": "c1", "name": "sandbox-mcp_preview", "arguments": "{}",
         "status": "completed"},
        {"type": "function_call_output", "call_id": "c1",
         "output": [{"type": "input_text", "text": "ok"}, {"type": "input_image", "image_url": LONG_DATA}]},
        {"type": "message", "content": [{"type": "output_text", "text": "Here."}]},
    ])
    add_row(con, "chat-a", "u2", "user", "a1", "and now?")
    add_row(con, "chat-a", "a2a", "assistant", "u2", "old answer")
    add_row(con, "chat-a", "a2b", "assistant", "u2", "", done=False, error={"content": "boom"})
    # chat-b: legacy JSON only (no rows).
    add_chat(con, "chat-b", current="b2", history={
        "b1": {"id": "b1", "parentId": None, "role": "user", "content": "legacy hello", "timestamp": NOW - DAY},
        "b2": {"id": "b2", "parentId": "b1", "role": "assistant", "content": "legacy hi", "timestamp": NOW - DAY,
               "model": "auto", "contextSummary": "a summary"},
    })
    # chat-c: rows whose root is only in the legacy JSON.
    add_chat(con, "chat-c", current="c2", history={
        "c0": {"id": "c0", "parentId": None, "role": "user", "content": "root from history"}})
    add_row(con, "chat-c", "c1", "assistant", "c0", "answer")
    add_row(con, "chat-c", "c2", "user", "c1", "follow-up")
    # chat-old: outside the window; chat-empty: no messages at all.
    add_chat(con, "chat-old", updated_at=NOW - 90 * DAY, current="o1")
    add_row(con, "chat-old", "o1", "user", None, "ancient")
    add_chat(con, "chat-empty", current=None)
    con.commit()
    con.close()
    return path


def by_id(records):
    return {r["chat_id"]: r for r in records}


def test_exports_the_current_branch_of_recent_chats(db):
    records, err = run_export(db)
    chats = by_id(records)
    assert set(chats) == {"chat-a", "chat-b", "chat-c"}
    assert "exported 3 chats" in err and "skipped 1 with an empty branch" in err
    a = chats["chat-a"]
    assert [m["id"] for m in a["messages"]] == ["u1", "a1", "u2", "a2b"]
    assert a["source"] == "chat_message" and a["current_message_id"] == "a2b"
    assert a["context_compaction"] is True and a["function_calling"] is None
    assert set(a) == {"chat_id", "user_id", "created_at", "updated_at", "current_message_id", "source",
                      "context_compaction", "function_calling", "messages"}


def test_message_fields_and_what_is_stripped(db):
    a = by_id(run_export(db)[0])["chat-a"]
    u1, a1, _, a2b = a["messages"]
    assert set(u1) == {"id", "parentId", "role", "content", "output", "model", "timestamp", "done", "error",
                       "files", "context_summary", "sources"}
    assert u1["files"][0] == {"type": "image", "url": "data:image/png;base64,[omitted: 422 chars]"}
    assert u1["files"][1] == {"type": "file", "id": "f1", "url": "/api/v1/files/f1", "name": "data.json",
                              "content_type": "application/json"}
    assert a1["files"] == [] and a1["sources"] == 2 and a1["model"] == "auto"
    assert a1["output"][0] == {"type": "reasoning", "id": "r1"}          # reasoning text never exported
    assert a1["output"][2]["output"][1]["image_url"] == "data:image/png;base64,[omitted: 422 chars]"
    assert a1["output"][1]["name"] == "sandbox-mcp_preview"
    assert (a2b["done"], a2b["error"]) == (False, True)
    assert (u1["done"], u1["error"], u1["timestamp"]) == (True, False, NOW - DAY)


def test_legacy_and_gap_filled_maps(db):
    chats = by_id(run_export(db)[0])
    b, c = chats["chat-b"], chats["chat-c"]
    assert b["source"] == "history"
    assert [m["content"] for m in b["messages"]] == ["legacy hello", "legacy hi"]
    assert b["messages"][1]["context_summary"] is True
    assert c["source"] == "chat_message+history"
    assert [m["id"] for m in c["messages"]] == ["c0", "c1", "c2"]


def test_window_and_named_chats(db):
    assert {r["chat_id"] for r in run_export(db, days=0.5)[0]} == set()
    named = run_export(db, chat_ids=["chat-old"])[0]
    assert [r["chat_id"] for r in named] == ["chat-old"]


def test_the_database_is_never_written(db):
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    run_export(db)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_older_schema_without_rows_or_current_column(tmp_path):
    path = tmp_path / "old.db"
    con = make_db(path, with_rows_table=False, with_current_column=False)
    add_chat(con, "chat-x", current="x1", with_current=False, params={"function_calling": "legacy"},
             history={"x1": {"id": "x1", "parentId": None, "role": "user", "content": "hi"}})
    con.commit()
    con.close()
    (record,), _ = run_export(path)
    assert record["source"] == "history" and record["current_message_id"] == "x1"
    assert record["function_calling"] == "legacy" and record["context_compaction"] is False


def test_a_parent_cycle_ends_the_walk():
    assert [mid for mid, _ in export_chats.branch(
        {"a": {"parentId": "b"}, "b": {"parentId": "a"}}, "a")] == ["b", "a"]


def test_export_feeds_points(db):
    """The exported records are what points.py reads."""
    chats = by_id(run_export(db)[0])
    rules = RuleBaseline.from_file()
    pts = asyncio.run(points_mod.chat_points(chats["chat-a"], baseline=rules,
                                             decisions=points_mod.DecisionIndex()))
    assert [p["user_message_id"] for p in pts] == ["u1", "u2"]
    # data.json in the attached-files block -> the code keywords match.
    assert pts[0]["rules_route"] == "local-code"
    assert pts[0]["answer_tools"] == ["sandbox-mcp_preview"]
    assert pts[1]["answer_error"] is True and pts[1]["answer_message_id"] == "a2b"


def test_main_refuses_a_bad_window():
    with pytest.raises(SystemExit):
        export_chats.main(["--days", "0"])


def test_it_imports_nothing_outside_the_standard_library():
    import ast
    import sys
    tree = ast.parse(open(export_chats.__file__, encoding="utf-8").read())
    modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    modules |= {node.module.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0}
    modules.discard("__future__")
    assert modules <= set(sys.stdlib_module_names), modules - set(sys.stdlib_module_names)
    assert not any(isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree))
