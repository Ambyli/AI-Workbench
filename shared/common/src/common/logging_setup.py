"""Reusable logging setup.

Two things live here:

``setup_logging(name, log_dir=None, debug=False)`` — configures the stdlib
``logging`` module for an app. File + console handlers with sensible defaults.

``CsvLogger`` — a structured audit-trail writer. Every ``.log(...)`` call
appends one row to a CSV file (schema declared at construction) and, if a
stdlib logger is attached, also emits a compact one-line summary through it.
Useful when a service wants a machine-analyzable event log alongside the
human-readable text logs stdlib produces.

DEBUG_LOGGING semantics for ``setup_logging``:
- File: DEBUG when debug=True, else INFO
- Console: DEBUG when debug=True, else WARNING
"""

from __future__ import annotations

import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


def setup_logging(
    name: str,
    log_dir: Optional[str | Path] = None,
    debug: bool = False,
) -> logging.Logger:
    """Configure the root logger and return a named child logger.

    Parameters
    ----------
    name : str
        Name for the returned child logger. Also used as the log-file stem
        (``<log_dir>/<name>.log``) when ``log_dir`` is given.
    log_dir : str | Path | None
        Directory for the log file. Created if missing. If None, no file
        handler is installed.
    debug : bool
        Verbose mode — see module docstring for level implications.
    """
    file_level = logging.DEBUG if debug else logging.INFO
    console_level = logging.DEBUG if debug else logging.WARNING

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_dir is not None:
        log_path = Path(log_dir) / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Recreate on each call — matches widget's behavior of a fresh log
        # per session.
        #
        # Belt AND suspenders: unlink drops the file (freeing any stale
        # inode a crashed prior process might still hold open), then
        # ``mode="w"`` truncates on the FileHandler's own ``open()`` so
        # even when unlink silently no-ops (permission surprise, docker
        # volume remount race, a Windows editor tail on the log, etc.)
        # the new handler starts from byte 0. Without the explicit mode,
        # FileHandler defaults to ``mode="a"``, which would append to
        # whatever bytes survived unlink and make the previous run's
        # tail look like part of this run's boot log — exactly the
        # "log doesn't clear on subsequent deploy" bug we shipped once.
        log_path.unlink(missing_ok=True)
        handlers.insert(
            0, logging.FileHandler(log_path, mode="w", encoding="utf-8")
        )

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(funcName)s: %(message)s",
        handlers=handlers,
        force=True,  # override any prior basicConfig call
    )

    root = logging.getLogger()
    # Handler order matches insertion above: file (if any) first, console last.
    if log_dir is not None:
        root.handlers[0].setLevel(file_level)
        root.handlers[1].setLevel(console_level)
    else:
        root.handlers[0].setLevel(console_level)

    return logging.getLogger(name)


class CsvLogger:
    """Structured audit-trail CSV writer.

    Each ``.log(...)`` call appends one row to ``path``. If a stdlib ``logger``
    is attached, a compact one-line summary is also emitted through it (level
    WARNING when a column named ``ok`` is False, otherwise INFO). This lets a
    service keep a machine-analyzable event log alongside the human-readable
    text log the stdlib logger produces.

    Column model
    ------------
    ``columns`` declares the DATA columns. A ``timestamp`` column is
    auto-prepended to the CSV header and auto-filled with a UTC ISO-8601
    stamp on every row — do NOT include it in ``columns`` yourself.

    Positional args in ``.log()`` map to ``columns`` in declared order (they
    do NOT need to know about the implicit timestamp). Keyword args bind by
    name and must match a declared column.

    Not thread-safe — wrap ``.log(...)`` in a lock if you have concurrent
    writers into a single CsvLogger.

    Example
    -------
    >>> from common.logging_setup import CsvLogger, setup_logging
    >>> stdlib_log = setup_logging("myapp", log_dir="/var/log/myapp")
    >>> audit = CsvLogger(
    ...     path="/var/log/myapp/events.csv",
    ...     columns=["stage", "action", "ok", "detail"],
    ...     logger=stdlib_log,
    ... )
    >>> audit.log("parser", "parsed", True, "ok")               # positional
    >>> audit.log(stage="brain", action="decide", ok=False)     # keyword
    """

    TIMESTAMP_COLUMN = "timestamp"

    def __init__(
        self,
        path: str | Path,
        columns: Sequence[str],
        *,
        logger: Optional[logging.Logger] = None,
        max_detail_chars: int = 500,
        auto_timestamp: bool = True,
    ) -> None:
        if auto_timestamp and self.TIMESTAMP_COLUMN in columns:
            raise ValueError(
                f"columns should NOT include {self.TIMESTAMP_COLUMN!r} — it "
                "is auto-prepended when auto_timestamp=True. Set "
                "auto_timestamp=False to declare timestamp manually."
            )
        self.path = Path(path)
        self.columns: list[str] = list(columns)
        self._logger = logger
        self._max_detail_chars = max_detail_chars
        self._auto_timestamp = auto_timestamp
        self._has_ok = "ok" in self.columns
        self._file_columns: list[str] = (
            [self.TIMESTAMP_COLUMN] + self.columns if auto_timestamp else self.columns
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self._file_columns).writeheader()

    def log(self, *args, **kwargs) -> None:
        """Append one row.

        Positional args fill the first ``len(args)`` DATA columns in order
        (the implicit ``timestamp`` column does not count).
        Keyword args must match a declared column name.
        """
        if len(args) > len(self.columns):
            raise ValueError(
                f"CsvLogger got {len(args)} positional args but only "
                f"{len(self.columns)} data columns"
            )
        unknown = set(kwargs) - set(self.columns)
        if unknown:
            raise ValueError(
                f"CsvLogger got unknown column(s): {sorted(unknown)}. "
                f"Known: {self.columns}"
            )

        row: dict[str, object] = {c: "" for c in self._file_columns}
        for i, v in enumerate(args):
            row[self.columns[i]] = v
        row.update(kwargs)

        if self._auto_timestamp:
            row[self.TIMESTAMP_COLUMN] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )

        # Truncate arbitrarily-long "detail"-like fields to keep CSV rows sane.
        for col in ("detail", "message"):
            if col in row and isinstance(row[col], str) and len(row[col]) > self._max_detail_chars:
                row[col] = row[col][: self._max_detail_chars]

        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self._file_columns).writerow(row)

        if self._logger is not None:
            self._emit_stdlib(row)

    def _emit_stdlib(self, row: dict) -> None:
        """Render a compact logfmt-ish line through the attached stdlib logger.

        Uses WARNING level when an `ok` column exists and is falsy, otherwise
        INFO — so grep-for-failures on the text log works the same way as it
        does on the CSV.
        """
        parts = [f"{k}={_render_field(v)}" for k, v in row.items()
                 if k != self.TIMESTAMP_COLUMN and v not in ("", None)]
        msg = " ".join(parts)
        if self._has_ok and row.get("ok") is False:
            self._logger.warning(msg)
        else:
            self._logger.info(msg)


def _render_field(v: object) -> str:
    """Compact rendering of a single CSV field for the stdlib logger line.

    Quote strings that contain spaces so logfmt parsers still parse the pair.
    Booleans render as 'true' / 'false' so they read the same as CSV.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    return f'"{s}"' if any(c.isspace() for c in s) else s
