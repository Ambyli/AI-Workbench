"""
LOGGER — one consistent log shape across every stage (Contract G).

Each entry: timestamp, stage, event_type, project_ref, action, ok, detail.
Writes to a CSV under LOG_DIR (default /data, mounted as a volume in Docker) and
echoes a compact line to stdout.
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone

_FIELDS = ["timestamp", "stage", "event_type", "project_ref", "action", "ok", "detail"]

_DEFAULT_DIR = os.environ.get("LOG_DIR", "/data")


class Logger:
    def __init__(self, path: str | None = None, echo: bool = True):
        self.path = path or os.path.join(_DEFAULT_DIR, "agent_log.csv")
        self.echo = echo
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_FIELDS).writeheader()

    def log(self, stage, action="", ok=True, detail="",
            event_type="", project_ref=""):
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stage": stage, "event_type": event_type, "project_ref": str(project_ref),
            "action": action, "ok": ok, "detail": (detail or "")[:500],
        }
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDS).writerow(row)
        if self.echo:
            mark = "ok " if ok else "ERR"
            print(f"[{mark}] {stage:12s} {event_type:18s} {action:16s} {(detail or '')[:70]}",
                  file=sys.stderr)
