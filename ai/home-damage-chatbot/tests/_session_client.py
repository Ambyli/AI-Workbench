"""TestClient for contract v2 (server-issued sessions).

The suites were written when the browser invented its own session id ("t1", "t2",
...). The server now issues sessions and rejects ids it didn't issue (S-M2). Rather
than threading real tokens through hundreds of test lines, this drop-in client keeps
the readable labels: the first use of a label starts a genuine server-issued session,
and every later call with that label is rewritten to the real token. Tests therefore
still exercise the production code path end to end.

Labels that fail the session-id format (e.g. "../evil") are passed through untouched
so the validation tests still see a 422.
"""
from __future__ import annotations

import io
import os
import tempfile
import re

from fastapi.testclient import TestClient

# One test process drives many conversations from a single "client IP".
os.environ.setdefault("MAX_SESSIONS_PER_CLIENT", "100000")

os.environ.setdefault("GEOCODER", "none")  # hermetic: no public geocoder calls
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="chatbot-test-"))  # never touch real backend/data

_SID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class SessionClient(TestClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sids: dict[str, str] = {}
        self._last: str | None = None

    # -- helpers -----------------------------------------------------------
    def real_sid(self, label: str) -> str | None:
        return self._sids.get(label)

    def forget(self, label: str) -> None:
        self._sids.pop(label, None)

    def _start(self, label: str, mode: str | None = None):
        body: dict = {"message": ""}
        if mode:
            body["mode"] = mode
        r = super().post("/api/chat", json=body)
        sid = r.json().get("session_id") if r.status_code == 200 else None
        if sid:
            self._sids[label] = sid
        return r

    def pointer_image(self, label: str) -> str:
        """Upload a real PNG as this session's damage-pointer map image; return its name."""
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (120, 140, 90)).save(buf, "PNG")
        r = self.post("/api/upload", data={"session_id": label},
                      files={"file": ("pin.png", buf.getvalue(), "image/png")})
        assert r.status_code == 200, r.text
        return r.json()["filename"]

    # -- request rewriting ---------------------------------------------------
    def post(self, url, *args, json=None, data=None, files=None, **kwargs):
        if url == "/api/chat" and isinstance(json, dict):
            label = json.get("session_id")
            if isinstance(label, str) and _SID_RE.match(label):
                self._last = label
                if label not in self._sids:
                    r = self._start(label, json.get("mode"))
                    is_greeting = json.get("message", "") == "" and not json.get("attachments")
                    if is_greeting or label not in self._sids:
                        return r  # the test's own greeting call (or start was refused)
                json = {**json, "session_id": self._sids[label]}
        elif url == "/api/queue/status" and isinstance(json, dict):
            label = json.get("session_id")
            if label in self._sids:
                json = {**json, "session_id": self._sids[label]}
        elif url == "/api/upload":
            data = dict(data or {})
            explicit = data.get("session_id")
            label = explicit or self._last
            if label not in self._sids:
                self._start(label or "__upload__")
                label = label or "__upload__"
            data["session_id"] = self._sids.get(label, "")
            r = super().post(url, *args, json=json, data=data, files=files, **kwargs)
            if r.status_code == 404 and not explicit:
                # The implicit (most recent) conversation was already submitted and
                # purged — give this standalone upload a fresh session of its own.
                self._start("__upload__")
                data["session_id"] = self._sids.get("__upload__", "")
                r = super().post(url, *args, json=json, data=data, files=files, **kwargs)
            return r
        return super().post(url, *args, json=json, data=data, files=files, **kwargs)
