"""Tests for common.processed_store.ProcessedStore."""

from datetime import datetime, timezone

import pytest

from common.processed_store import ProcessedRecord, ProcessedStore


def test_creates_schema_on_init(tmp_path):
    """A fresh store creates its file and schema without needing existing state."""
    store = ProcessedStore(tmp_path / "sub" / "processed.db")  # parent doesn't exist
    assert (tmp_path / "sub" / "processed.db").exists()
    assert store.count() == 0


def test_reinit_on_existing_file_is_safe(tmp_path):
    path = tmp_path / "processed.db"
    ProcessedStore(path).record("k1")
    ProcessedStore(path).record("k2")  # second init, same file
    assert ProcessedStore(path).count() == 2


def test_is_processed_only_true_for_ok(tmp_path):
    store = ProcessedStore(tmp_path / "processed.db")

    assert store.is_processed("nope") is False

    store.mark_pending("k")
    assert store.is_processed("k") is False
    assert store.get("k").status == "pending"

    store.mark_error("k", metadata={"error": "boom"})
    assert store.is_processed("k") is False
    assert store.get("k").status == "error"

    store.mark_ok("k")
    assert store.is_processed("k") is True


def test_metadata_round_trip(tmp_path):
    store = ProcessedStore(tmp_path / "processed.db")
    store.record(
        "msg-123",
        status="ok",
        metadata={"roofix_project_id": "1782x", "phoenix_project_id": 42},
    )
    rec = store.get("msg-123")
    assert rec is not None
    assert rec.metadata == {"roofix_project_id": "1782x", "phoenix_project_id": 42}


def test_record_upserts(tmp_path):
    """Recording the same key twice replaces the prior row (latest wins)."""
    store = ProcessedStore(tmp_path / "processed.db")
    store.record("k", status="pending", metadata={"attempt": 1})
    store.record("k", status="ok", metadata={"attempt": 2, "result": "yes"})

    rec = store.get("k")
    assert rec.status == "ok"
    assert rec.metadata == {"attempt": 2, "result": "yes"}
    assert store.count() == 1


def test_processed_at_is_utc_and_recent(tmp_path):
    store = ProcessedStore(tmp_path / "processed.db")
    store.record("k")
    rec = store.get("k")

    assert rec.processed_at.tzinfo is not None
    assert (datetime.now(timezone.utc) - rec.processed_at).total_seconds() < 5


def test_invalid_status_rejected(tmp_path):
    store = ProcessedStore(tmp_path / "processed.db")
    with pytest.raises(ValueError, match="status must be"):
        store.record("k", status="finished")


def test_list_by_status(tmp_path):
    store = ProcessedStore(tmp_path / "processed.db")
    store.mark_ok("a")
    store.mark_ok("b")
    store.mark_error("c", metadata={"error": "x"})
    store.mark_pending("d")

    ok_keys = sorted(r.key for r in store.list_by_status("ok"))
    err_keys = [r.key for r in store.list_by_status("error")]
    pending_keys = [r.key for r in store.list_by_status("pending")]

    assert ok_keys == ["a", "b"]
    assert err_keys == ["c"]
    assert pending_keys == ["d"]


def test_count_filters_by_status(tmp_path):
    store = ProcessedStore(tmp_path / "processed.db")
    store.mark_ok("a")
    store.mark_ok("b")
    store.mark_error("c")

    assert store.count() == 3
    assert store.count(status="ok") == 2
    assert store.count(status="error") == 1
    assert store.count(status="pending") == 0


def test_get_unknown_key_returns_none(tmp_path):
    store = ProcessedStore(tmp_path / "processed.db")
    assert store.get("never-recorded") is None


def test_record_returns_written_record(tmp_path):
    store = ProcessedStore(tmp_path / "processed.db")
    rec = store.record("k", status="ok", metadata={"foo": "bar"})
    assert isinstance(rec, ProcessedRecord)
    assert rec.key == "k"
    assert rec.status == "ok"
    assert rec.metadata == {"foo": "bar"}


def test_retry_workflow(tmp_path):
    """Mimic the caller pattern: pending → error → retry to ok."""
    store = ProcessedStore(tmp_path / "processed.db")
    msg_id = "gmail:abc123"

    # Attempt 1: starts pending, fails.
    store.mark_pending(msg_id, metadata={"attempt": 1})
    store.mark_error(msg_id, metadata={"attempt": 1, "error": "phoenix 500"})

    # is_processed says False → caller will retry.
    assert store.is_processed(msg_id) is False

    # Attempt 2: succeeds.
    store.mark_pending(msg_id, metadata={"attempt": 2})
    store.mark_ok(msg_id, metadata={"attempt": 2, "phoenix_project_id": 42})

    assert store.is_processed(msg_id) is True
    rec = store.get(msg_id)
    assert rec.metadata["phoenix_project_id"] == 42
