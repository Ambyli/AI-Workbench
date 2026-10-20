"""Reading and writing the benchmark's JSONL data files.

Every data file is one JSON object per line, UTF-8, ``\\n`` line ends.
Writers use ``ensure_ascii=True``: a conversation can carry a lone UTF-16
surrogate (it round-trips through JSON as ``\\ud83d``), which would make a
UTF-8 encoder raise half-way through a file; escaped, it is plain ASCII and
every reader (Python, the label page's ``JSON.parse``) restores it.

``results.jsonl`` is appended to while ``run.py`` works, so a killed run can
leave a half-written last line. :class:`Appender` starts a fresh line when
the file does not end with one, and :func:`read_jsonl` with ``skip_bad``
counts and skips a line that does not parse instead of failing the whole
read -- the resume and the report then simply redo / ignore that one result.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

PathLike = Union[str, pathlib.Path]


def dumps(record: Any) -> str:
    """One record as one line (no trailing newline)."""
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"))


def read_jsonl(path: PathLike, *, skip_bad: bool = False) -> Tuple[List[Dict[str, Any]], int]:
    """(records, bad line count). Blank lines are ignored. A line that is not
    a JSON object raises ``ValueError`` naming the file and line -- or, with
    ``skip_bad``, is counted and skipped."""
    records: List[Dict[str, Any]] = []
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("not a JSON object")
            except ValueError as exc:
                if not skip_bad:
                    raise ValueError(f"{path}:{number}: {exc}") from None
                bad += 1
                continue
            records.append(record)
    return records, bad


def write_jsonl(path: PathLike, records: Iterable[Dict[str, Any]]) -> int:
    """Write ``records`` to ``path`` (replacing it); returns how many."""
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(dumps(record) + "\n")
            count += 1
    return count


class Appender:
    """Appends records to a JSONL file one line at a time, flushed after each,
    so an interrupted run keeps every finished line."""

    def __init__(self, path: PathLike) -> None:
        self.path = pathlib.Path(path)
        needs_newline = False
        if self.path.exists() and self.path.stat().st_size > 0:
            with open(self.path, "rb") as fh:
                fh.seek(-1, 2)
                needs_newline = fh.read(1) != b"\n"
        self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
        if needs_newline:
            self._fh.write("\n")
            self._fh.flush()

    def write(self, record: Dict[str, Any]) -> None:
        self._fh.write(dumps(record) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "Appender":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def warn(message: str, stream: Optional[Any] = None) -> None:
    """One line on stderr (the data files never get diagnostics)."""
    print(message, file=stream if stream is not None else sys.stderr, flush=True)
