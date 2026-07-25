"""Tests for common.logging_setup.CsvLogger."""

import csv
import logging
from datetime import datetime, timezone

import pytest

from common.logging_setup import CsvLogger


# Data columns only — timestamp is auto-prepended by CsvLogger.
COLUMNS = ["stage", "action", "ok", "detail"]
FILE_COLUMNS = ["timestamp"] + COLUMNS


def _read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_writes_header_with_timestamp_prepended(tmp_path):
    path = tmp_path / "audit.csv"
    CsvLogger(path=path, columns=COLUMNS)
    with open(path, encoding="utf-8") as f:
        header = f.readline().strip().split(",")
    assert header == FILE_COLUMNS


def test_declaring_timestamp_column_raises(tmp_path):
    """Users shouldn't declare `timestamp` when auto_timestamp is on."""
    path = tmp_path / "audit.csv"
    with pytest.raises(ValueError, match="auto-prepended"):
        CsvLogger(path=path, columns=["timestamp", "stage"])


def test_does_not_overwrite_existing_file(tmp_path):
    """Re-instantiating on an existing file preserves prior rows."""
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=COLUMNS)
    log.log("parser", "parsed", True, "first")

    log2 = CsvLogger(path=path, columns=COLUMNS)
    log2.log("brain", "decide", True, "second")

    rows = _read_rows(path)
    assert [r["stage"] for r in rows] == ["parser", "brain"]
    assert [r["detail"] for r in rows] == ["first", "second"]


def test_positional_args_map_to_data_columns_in_order(tmp_path):
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=COLUMNS)
    log.log("parser", "parsed", True, "ok")

    row = _read_rows(path)[0]
    assert row["stage"] == "parser"
    assert row["action"] == "parsed"
    assert row["ok"] == "True"
    assert row["detail"] == "ok"


def test_keyword_args_bind_by_name(tmp_path):
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=COLUMNS)
    log.log(stage="phoenix", action="write", ok=False, detail="failed")

    row = _read_rows(path)[0]
    assert row["stage"] == "phoenix"
    assert row["action"] == "write"
    assert row["ok"] == "False"
    assert row["detail"] == "failed"


def test_mixed_positional_and_keyword(tmp_path):
    """First positional fills stage; keyword sets ok/detail out of order."""
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=COLUMNS)
    log.log("orchestrator", "ignore", detail="not interested", ok=True)

    row = _read_rows(path)[0]
    assert row["stage"] == "orchestrator"
    assert row["action"] == "ignore"
    assert row["ok"] == "True"
    assert row["detail"] == "not interested"


def test_timestamp_auto_filled(tmp_path):
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=COLUMNS)
    log.log("parser", "parsed", True, "x")

    row = _read_rows(path)[0]
    assert row["timestamp"]  # non-empty
    parsed = datetime.fromisoformat(row["timestamp"])
    assert (datetime.now(timezone.utc) - parsed).total_seconds() < 5


def test_auto_timestamp_off(tmp_path):
    """With auto_timestamp=False users declare their own schema fully."""
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=["stage", "detail"], auto_timestamp=False)
    log.log("a", "b")

    row = _read_rows(path)[0]
    assert row == {"stage": "a", "detail": "b"}


def test_unknown_keyword_raises(tmp_path):
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=COLUMNS)
    with pytest.raises(ValueError, match="unknown column"):
        log.log("parser", ok=True, gibberish="bad")


def test_too_many_positional_raises(tmp_path):
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=["a", "b"])
    with pytest.raises(ValueError, match="positional"):
        log.log("x", "y", "z")


def test_detail_truncation(tmp_path):
    """Very long `detail` fields get clipped so a single blob can't blow up the CSV."""
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=COLUMNS, max_detail_chars=20)
    log.log("s", "a", True, "x" * 100)

    row = _read_rows(path)[0]
    assert len(row["detail"]) == 20


def test_emits_via_stdlib_logger(tmp_path, caplog):
    path = tmp_path / "audit.csv"
    stdlib_log = logging.getLogger("test.csvlogger.emit")
    stdlib_log.setLevel(logging.INFO)
    log = CsvLogger(path=path, columns=COLUMNS, logger=stdlib_log)

    with caplog.at_level(logging.INFO, logger="test.csvlogger.emit"):
        log.log("parser", "parsed", True, "ok")

    assert any("stage=parser" in r.message for r in caplog.records)
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_ok_false_emits_warning(tmp_path, caplog):
    """A False `ok` column bumps the stdlib emit level to WARNING."""
    path = tmp_path / "audit.csv"
    stdlib_log = logging.getLogger("test.csvlogger.warn")
    stdlib_log.setLevel(logging.WARNING)
    log = CsvLogger(path=path, columns=COLUMNS, logger=stdlib_log)

    with caplog.at_level(logging.WARNING, logger="test.csvlogger.warn"):
        log.log("phoenix", "write", False, "boom")

    assert any(r.levelno == logging.WARNING and "ok=false" in r.message
               for r in caplog.records)


def test_no_stdlib_logger_no_emit(tmp_path):
    """When no logger is attached the CSV write still succeeds silently."""
    path = tmp_path / "audit.csv"
    log = CsvLogger(path=path, columns=COLUMNS)
    log.log("parser", "parsed", True, "ok")
    assert len(_read_rows(path)) == 1
