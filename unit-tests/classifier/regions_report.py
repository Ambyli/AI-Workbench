#!/usr/bin/env python3
"""Run the classifier's Postman collection as a suite and render the evidence.

The classifier already tells you *where* it found something. What it cannot
tell you is whether that place is right — and a reviewer reading
``regions.json`` cannot tell either. So this script takes the collection that
already documents every hand-test
(``ai/classifier/classifier.postman_collection.json``) and makes it runnable:
submit every request, poll the job, pull the layers, and then **re-draw the
geometry independently** onto the original fixture before putting the two
pictures side by side in one HTML page.

The independence is the point. The service's own ``p{n}.preview.jpg`` was
drawn by the same code that produced the regions, so it cannot disagree with
them. The annotated JPEG next to it is drawn from ``regions.json`` by
``common.vision.annotate`` onto the fixture as it exists on disk — if the two
differ, one of them is wrong, and that is visible without reading a single
coordinate.

A request may carry several documents, and every page of every document is
one ITEM (``n`` in ``p{n}``, the region's ``page``). Each item's regions are
drawn on that item's own page — page ``j`` of the fixture it came from, found
through the result's ``items`` / ``documents`` map — and the report shows
every criterion's per-item results beside the aggregated one.

    submit → poll → GET artifacts → download regions.json, the text layers,
                                    and the layers (rendered on first fetch)
                                  → re-draw on the original
                                  → compare against regions_expected.json
                                  → index.html + summary.json + exit code

Usage (from the repo root)::

    uv run --package classifier python unit-tests/classifier/regions_report.py
    uv run --package classifier python unit-tests/classifier/regions_report.py --local
    uv run --package classifier python unit-tests/classifier/regions_report.py \\
        --only photo_of_letter --folders "Documents + regions"

``--local`` mounts the FastAPI app in-process with ``TestClient`` instead of
talking to the box, so the whole pipeline — collection parsing, submission,
polling, artifact download, annotation, report — is verifiable with no vision
model anywhere. See REGIONS_REPORT.md for setup, how to read the output, and
how to add a case.

Adding a case is: add the Postman item, add an entry to
``regions_expected.json``. Nothing here needs to change — the collection IS
the suite.

**The amount-due locator** is the one case the collection cannot hold: a
request built for whatever bill is passed on the command line.
``--pipeline utility-bill`` makes ONE ``/assess`` call per bill, with a single
``llm`` criterion (``score: false``, ``options.boxes: true``) asking the vision
model where the amount due is. The report shows the page with every box the
model drew — accepted dotted and filled, rejected dashed and unfilled —
beside a zoomed view of the accepted one::

    uv run --package classifier python unit-tests/classifier/regions_report.py \\
        --pipeline utility-bill
    uv run --package classifier python unit-tests/classifier/regions_report.py \\
        --pipeline utility-bill --document ~/Downloads/some_other_bill.jpg

An ad-hoc ``--document`` (repeatable) replaces the fixture list and has no
expectations: its checks are recorded as skipped, and where the model put
the box is still reported. See § Pipelines below.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import datetime as dt
import fnmatch
import html
import io
import json
import os
import pathlib
import re
import sys
import time
import traceback
from typing import Any, Iterable, Optional

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# `common` is a uv workspace package, so `uv run --package classifier` already
# has it importable. A bare `python unit-tests/...` does not, and failing on an
# import when the source is right there in the repo helps nobody.
_COMMON_SRC = REPO_ROOT / "shared" / "common" / "src"
if _COMMON_SRC.is_dir() and str(_COMMON_SRC) not in sys.path:
    sys.path.insert(0, str(_COMMON_SRC))

from common.env import load_env  # noqa: E402
from common.vision import (  # noqa: E402
    PageGeometry,
    Region,
    annotate_to_jpeg,
    criterion_color,
    grid_to_pixels,
)

# ---------------------------------------------------------------------------
# Defaults and data
# ---------------------------------------------------------------------------

DEFAULT_COLLECTION = REPO_ROOT / "ai" / "classifier" / "classifier.postman_collection.json"
DEFAULT_EXPECTATIONS = REPO_ROOT / "unit-tests" / "classifier" / "regions_expected.json"
DEFAULT_REPORTS_DIR = REPO_ROOT / "unit-tests" / "classifier" / "reports"
DEFAULT_BASE_URL = "http://localhost:4001"

# The prefix the LiteLLM pass-through adds. Every collection URL carries it;
# a locally mounted app does not, because the pass-through is what supplies it.
PASSTHROUGH_PREFIX = ("v1", "classifier")

# Folders run by default. The Artifacts folder is deliberately absent: its
# items are parametrised GET/DELETE calls against a :jobId that only exists
# after a submission, and this script exercises those endpoints itself as part
# of every case.
DEFAULT_FOLDERS = ("Documents", "Regions", "Documents + regions")

# Collection `{{name_b64}}` variable → the fixture whose bytes it stands for.
#
# DATA, not derivation: a convention (``invoice_native_b64`` ↔
# ``documents/invoice_native.pdf``) is right until the first variable that
# crosses fixture folders. An unlisted ``*_b64`` variable is a hard error, not
# a guess. The JSON-body items are the only ones that use these.
B64_FIXTURES: dict[str, str] = {
    "invoice_native_b64": "unit-tests/classifier/documents/invoice_native.pdf",
}

# Files a job writes that are worth pulling down: the geometry, the manifest,
# and every text layer (the exact text each text criterion searched).
ARTIFACT_PATTERNS = (
    "regions.json",
    "manifest.json",
    "text.*.json",
)

# The layers are NOT on disk until something fetches them — the artifact
# endpoint renders them on first fetch. These two are requested explicitly,
# from the result's ``artifacts.layers``, whenever the page had an image.
LAYERS_TO_FETCH = ("svg", "preview")

POLL_INTERVAL_S = 1.5
TERMINAL_PHASES = ("completed", "failed", "cancelled")

# Suffixes we know how to re-render a page image from. Anything else (.txt,
# .docx) has no pixel space, which the report states rather than hides.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")


# ---------------------------------------------------------------------------
# Collection parsing
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Case:
    """One runnable Postman item, resolved against the fixtures on disk."""

    name: str
    folder: str
    index: int
    method: str
    path: str                      # "/v1/classifier/assess"
    description: str
    headers: dict[str, str]
    form: dict[str, str] = dataclasses.field(default_factory=dict)
    # Every file part, in collection order: (form field, fixture path). A
    # request may carry several documents; each is one or more items.
    uploads: list[tuple[str, pathlib.Path]] = dataclasses.field(default_factory=list)
    json_body: Optional[dict] = None
    # The fixtures the documents' pages are drawn from — the uploads, or the
    # `*_b64` variables of a JSON body, in order.
    subjects: list[pathlib.Path] = dataclasses.field(default_factory=list)
    # Inline `text` form fields, in order — each one is a document.
    texts: list[str] = dataclasses.field(default_factory=list)
    # Key into regions_expected.json when it is not the item name — a pipeline
    # stage retried on a second candidate keeps one expectations entry.
    expect_key: Optional[str] = None
    # An ad-hoc --document has no entry to be right or wrong against: a missing
    # expectation is then recorded as skipped instead of failing coverage.
    adhoc: bool = False

    @property
    def slug(self) -> str:
        return f"{self.index:02d}-{_slug(self.name)}"

    @property
    def upload(self) -> Optional[pathlib.Path]:
        """The first file part — the one document of most cases."""
        return self.uploads[0][1] if self.uploads else None

    @property
    def subject(self) -> Optional[pathlib.Path]:
        """The first document regions are drawn on."""
        return self.subjects[0] if self.subjects else None

    @property
    def endpoint(self) -> str:
        return "/" + "/".join(self.path.strip("/").split("/")[len(PASSTHROUGH_PREFIX):])

    @property
    def criteria(self) -> list[dict]:
        """The criteria list exactly as the request carries it (JSON or form)."""
        if self.json_body is not None:
            return list(self.json_body.get("criteria") or [])
        raw = self.form.get("criteria")
        return json.loads(raw) if raw else []

    def set_criteria(self, items: list) -> None:
        """Write a filtered criteria list back into whichever encoding holds it."""
        if self.json_body is not None:
            self.json_body["criteria"] = items
        else:
            self.form["criteria"] = json.dumps(items, separators=(",", ":"))


def _slug(name: str) -> str:
    """Item name → a short directory name. No hash: names are unique in a run
    and the index prefix keeps ordering, so readability wins."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return base[:56].strip("-") or "case"


def load_collection(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _substitute(text: str, base_url: str, api_key: str) -> str:
    """Resolve every ``{{variable}}`` in a raw request body.

    ``*_b64`` variables are filled from :data:`B64_FIXTURES`; anything else
    unknown raises, because a body that silently keeps a literal ``{{x}}`` in
    it produces a 400 fifty lines later with nothing pointing at the cause.
    """
    for name in sorted(set(re.findall(r"\{\{([^}]+)\}\}", text))):
        if name == "litellm":
            value = base_url
        elif name == "virtual master key":
            value = api_key
        elif name in B64_FIXTURES:
            fixture = REPO_ROOT / B64_FIXTURES[name]
            if not fixture.is_file():
                raise SystemExit(f"{name}: fixture not found: {fixture}")
            value = base64.b64encode(fixture.read_bytes()).decode("ascii")
        else:
            raise SystemExit(
                f"Unknown collection variable {{{{{name}}}}} in a request body. "
                "Add it to B64_FIXTURES in regions_report.py (variable name → "
                "repo-relative fixture path) if it is a new base64 fixture."
            )
        text = text.replace("{{" + name + "}}", value)
    return text


def build_cases(
    collection: dict,
    *,
    folders: Iterable[str],
    only: Optional[str],
    base_url: str,
    api_key: str,
) -> list[Case]:
    """Turn the collection's folders into runnable cases, in collection order.

    Only POSTs are kept. The GET/DELETE items — ``/health``, ``/jobs``, and the
    whole Artifacts folder — are either introspection or parametrised on a
    ``:jobId`` that does not exist until something has been submitted; every
    case here exercises the artifact endpoints itself after its own job lands.
    """
    wanted = list(folders)
    cases: list[Case] = []
    index = 0
    for folder in collection.get("item", []):
        if "item" not in folder or folder.get("name") not in wanted:
            continue
        for item in folder["item"]:
            request = item.get("request") or {}
            if (request.get("method") or "").upper() != "POST":
                continue
            if only and only.lower() not in item["name"].lower():
                continue
            index += 1
            cases.append(
                _build_case(item, folder["name"], index, base_url, api_key)
            )
    return cases


def _build_case(
    item: dict, folder: str, index: int, base_url: str, api_key: str
) -> Case:
    request = item["request"]
    url = request.get("url") or {}
    path = "/" + "/".join(url.get("path") or [])
    headers = {
        h["key"]: h["value"]
        for h in request.get("header") or []
        if not h.get("disabled")
    }

    case = Case(
        name=item["name"],
        folder=folder,
        index=index,
        method="POST",
        path=path,
        description=request.get("description") or "",
        headers=headers,
    )

    body = request.get("body") or {}
    if body.get("mode") == "formdata":
        for field in body.get("formdata") or []:
            if field.get("disabled"):
                continue
            if field.get("type") == "file":
                path = REPO_ROOT / field["src"]
                case.uploads.append((field["key"], path))
                case.subjects.append(path)
            elif field["key"] == "text":
                # A repeated inline-text field is one document each; kept in
                # a list so a second one does not overwrite the first.
                case.texts.append(_substitute(field.get("value") or "", base_url, api_key))
            else:
                case.form[field["key"]] = _substitute(
                    field.get("value") or "", base_url, api_key
                )
    elif body.get("mode") == "raw":
        raw = _substitute(body.get("raw") or "", base_url, api_key)
        case.json_body = json.loads(raw)
        # The documents are whichever fixtures the `*_b64` variables stood
        # for, in order — needed to draw the regions on something.
        for name in re.findall(r"\{\{(\w+_b64)\}\}", body.get("raw") or ""):
            case.subjects.append(REPO_ROOT / B64_FIXTURES[name])
        # Content-Type is set by the client; leaving the collection's copy in
        # place is harmless but duplicated.
        headers.pop("Content-Type", None)

    for _field, path in case.uploads:
        if not path.is_file():
            raise SystemExit(f"{case.name}: fixture not found: {path}")
    return case


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class HttpTransport:
    """Talk to a running classifier through LiteLLM's pass-through."""

    local = False

    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0):
        import requests

        self._session = requests.Session()
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout

    def __enter__(self) -> "HttpTransport":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._session.close()

    def url(self, path: str) -> str:
        return f"{self._base}{path}"

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", None) or {})
        kwargs.setdefault("timeout", self._timeout)
        return self._session.request(method, self.url(path), headers=headers, **kwargs)


class LocalTransport:
    """Mount ``ai/classifier/main.py`` in-process with ``TestClient``.

    The point is to be able to verify everything that is not the vision model
    — collection parsing, submission, the queue, OCR, the CV detectors, text
    matching, artifact writing, the annotation pass and the report — without a
    GPU anywhere. ``llm`` and ``detector`` criteria have no server to call;
    see :func:`_strip_remote_criteria` for what happens to them.

    Every path the collection produces carries LiteLLM's ``/v1/classifier``
    prefix, which the pass-through supplies and a mounted app does not, so it
    is stripped here rather than in the collection parser — the collection is
    right about how the service is reached in production.
    """

    local = True

    def __init__(self, workdir: pathlib.Path):
        workdir.mkdir(parents=True, exist_ok=True)
        # The classifier keeps its queue, references and usage rows in
        # Postgres and nowhere else — so a local run needs a server.
        # It gets a database of its own (classifier_local_<pid>_<hex>) on
        # TEST_POSTGRES_DSN (or an explicitly set CLASSIFIER_DB_HOST & co.),
        # dropped again in __exit__. Inside pytest the conftest has already
        # imported config against the SESSION database, so a second one here
        # would be ignored — the run uses the session's instead.
        self._db = None
        if "config" not in sys.modules:
            from pg_testdb import ThrowawayDatabase, admin_dsn  # noqa: PLC0415

            admin = admin_dsn()
            if not admin:
                raise SystemExit(
                    "--local needs a Postgres server for the classifier's job queue: set "
                    "TEST_POSTGRES_DSN (e.g. postgresql://postgres@localhost:5432/postgres). "
                    "A throwaway database is created on it for this run and dropped after."
                )
            self._db = ThrowawayDatabase(admin, prefix="classifier_local").create()
            os.environ.update(self._db.env())
        os.environ["CLASSIFIER_DATA_DIR"] = str(workdir)
        os.environ["PAYLOAD_DIR"] = str(workdir / "payloads")
        os.environ["CLASSIFIER_ARTIFACT_DIR"] = str(workdir / "artifacts")
        # References live in their own never-swept root; left at its default
        # (/data/references) a local run would write customer pages outside
        # the work directory.
        os.environ["CLASSIFIER_REFERENCE_DIR"] = str(workdir / "references")
        os.environ.setdefault("CLASSIFIER_OCR_ENGINE", "rapidocr")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        # A network dependency with nothing behind it here. Empty
        # DETECTOR_URL is "off": `detector` criteria are dropped by
        # _strip_remote_criteria, and a `cv` criterion's fallback resolves to
        # the llm.
        os.environ.setdefault("DETECTOR_URL", "")

        classifier_dir = REPO_ROOT / "ai" / "classifier"
        if str(classifier_dir) not in sys.path:
            sys.path.insert(0, str(classifier_dir))

        from fastapi.testclient import TestClient

        import main as classifier_main  # noqa: PLC0415 — after the env is set

        self._client = TestClient(classifier_main.app)

    def __enter__(self) -> "LocalTransport":
        self._client.__enter__()   # runs the lifespan: workers + sweeper
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._client.__exit__(*exc)   # the lifespan closes the pool first
        finally:
            if self._db is not None:
                self._db.drop()

    def url(self, path: str) -> str:
        return self._strip(path)

    @staticmethod
    def _strip(path: str) -> str:
        parts = [p for p in path.strip("/").split("/") if p]
        if tuple(parts[: len(PASSTHROUGH_PREFIX)]) == PASSTHROUGH_PREFIX:
            parts = parts[len(PASSTHROUGH_PREFIX):]
        return "/" + "/".join(parts)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        kwargs.pop("timeout", None)     # TestClient has no socket to time out
        return self._client.request(method, self._strip(path), **kwargs)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CaseResult:
    """Everything one case produced, ready for both report writers."""

    case: Case
    out_dir: pathlib.Path
    http_status: int = 0
    job_id: Optional[str] = None
    phase: str = "not-submitted"
    elapsed_s: float = 0.0
    result: dict = dataclasses.field(default_factory=dict)
    job: dict = dataclasses.field(default_factory=dict)
    regions_doc: dict = dataclasses.field(default_factory=dict)
    manifest: dict = dataclasses.field(default_factory=dict)
    files: list[str] = dataclasses.field(default_factory=list)
    annotated: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    dropped_criteria: list[str] = dataclasses.field(default_factory=list)
    error: Optional[str] = None
    checks: list[dict] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)


def _strip_remote_criteria(case: Case, dropped: list[str]) -> Optional[str]:
    """Drop the criteria a ``--local`` run has no server for, in place.

    ``llm`` criteria need the vision model and ``detector`` criteria the
    open-vocabulary detector; neither is reachable in-process. A failing
    criterion no longer fails the job (it comes back ``status: "error"``),
    but a ``detector`` criterion is refused at submit when DETECTOR_URL is
    empty, and every model call would be a connection error — so both are
    removed, and the checks can tell "absent because we dropped it" from
    "absent because the service lost it".

    A request whose criteria would ALL be dropped is sent unchanged instead:
    emptying the list is a 400, and a job whose criteria all come back
    ``status: "error"`` is a more honest local outcome than a request that
    was quietly rewritten into a different one.

    Returns a note for the report, or None when nothing was dropped.
    """
    criteria = case.criteria
    if not criteria:
        return None
    kept = [c for c in criteria if not _needs_remote(c)]
    if not kept:
        return (
            "local mode: every criterion needs the vision model or the detector, "
            "so the request was sent unchanged and each criterion is expected to "
            "come back status: error"
        )
    if len(kept) == len(criteria):
        return None

    dropped.extend(c["name"] for c in criteria if _needs_remote(c))
    case.set_criteria(kept)
    return (
        f"local mode: dropped {len(dropped)} criteri{'on' if len(dropped) == 1 else 'a'} "
        f"({', '.join(dropped)}) — no vision model or detector is reachable in-process"
    )


def _needs_remote(criterion: dict) -> bool:
    """True for a criterion that will certainly call the model or the detector.

    A ``cv`` criterion is not one even when no OpenCV detector matches its
    name: its fallback resolves to the llm locally and it comes back
    ``status: "error"``, which the checks record as skipped in local mode.
    """
    kind = criterion.get("type") or "llm"
    if kind in ("llm", "detector"):
        return True
    return kind == "cv" and (criterion.get("options") or {}).get("fallback") == "detector"


def submit(case: Case, transport: Any) -> Any:
    """POST the case, returning the raw response."""
    if case.json_body is not None:
        return transport.request(
            "POST", case.path, json=case.json_body, headers=case.headers
        )
    # A list of (field, file) tuples, so a repeated `file` part is sent as
    # many times as the collection lists it. (Both requests and httpx put the
    # plain fields — `criteria`, `text` — before the files; the service keeps
    # form order, and the documents map in the result says which is which.)
    files = [
        (field, (path.name, path.read_bytes(), "application/octet-stream"))
        for field, path in case.uploads
    ] or None
    data: dict[str, Any] = dict(case.form)
    if case.texts:
        data["text"] = case.texts if len(case.texts) > 1 else case.texts[0]
    return transport.request(
        "POST", case.path, data=data, files=files, headers=case.headers
    )


def poll(transport: Any, job_id: str, timeout_s: float) -> dict:
    """Poll ``GET /jobs/{id}`` until the phase is terminal or time runs out."""
    deadline = time.monotonic() + timeout_s
    job: dict = {}
    while time.monotonic() < deadline:
        response = transport.request("GET", f"/v1/classifier/jobs/{job_id}")
        if response.status_code != 200:
            raise RuntimeError(
                f"GET /jobs/{job_id} returned {response.status_code}: {response.text[:300]}"
            )
        job = response.json()
        if job.get("phase") in TERMINAL_PHASES:
            return job
        time.sleep(POLL_INTERVAL_S)
    job.setdefault("phase", "timeout")
    job["error"] = f"still {job.get('phase')} after {timeout_s:.0f}s"
    return job


def fetch_artifacts(
    transport: Any, job_id: str, out_dir: pathlib.Path, result: dict
) -> tuple[dict, list[str], list[str]]:
    """Download what the job wrote, then ask for the layers worth looking at.

    Every job writes a directory, so a 404 means the job never got far enough
    to write one (it failed), and a 410 that it had one and the sweeper or a
    DELETE took it. The layers are rendered on first fetch, so they are
    requested explicitly, per item, from ``result.artifacts.items[].layers``
    — that request is also what exercises the lazy renderer.
    """
    notes: list[str] = []
    response = transport.request("GET", f"/v1/classifier/jobs/{job_id}/artifacts")
    if response.status_code == 404:
        return {}, [], ["no artifact directory (the job did not get far enough to write one)"]
    if response.status_code == 410:
        return {}, [], ["artifact directory is gone (swept past the TTL, or deleted)"]
    if response.status_code != 200:
        return {}, [], [f"GET /artifacts returned {response.status_code}"]

    manifest = response.json()
    saved: list[str] = []

    def download(name: str) -> None:
        file_response = transport.request(
            "GET", f"/v1/classifier/jobs/{job_id}/artifacts/{name}"
        )
        if file_response.status_code != 200:
            notes.append(f"{name}: download returned {file_response.status_code}")
            return
        (out_dir / name).write_bytes(file_response.content)
        saved.append(name)

    for entry in manifest.get("files") or []:
        name = entry.get("name") or ""
        if any(fnmatch.fnmatch(name, pattern) for pattern in ARTIFACT_PATTERNS):
            download(name)

    for item in ((result.get("artifacts") or {}).get("items")) or []:
        layers = item.get("layers") or {}
        for fmt in LAYERS_TO_FETCH:
            link = layers.get(fmt)
            if link:
                download(link.rsplit("/", 1)[-1])
    return manifest, saved, notes


def run_case(
    case: Case,
    transport: Any,
    out_root: pathlib.Path,
    args: argparse.Namespace,
    *,
    keep_remote: bool = False,
) -> CaseResult:
    """Submit, poll, download, annotate — one case, start to finish.

    ``keep_remote`` sends the criteria unchanged in local mode too: for a
    caller whose subject IS the llm criterion, dropping it leaves nothing to
    report, and a ``status: "error"`` (or a scripted model) is the honest
    local outcome.
    """
    out_dir = out_root / case.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    result = CaseResult(case=case, out_dir=out_dir)

    if transport.local and not keep_remote:
        note = _strip_remote_criteria(case, result.dropped_criteria)
        if note:
            result.notes.append(note)

    started = time.monotonic()
    try:
        response = submit(case, transport)
    except Exception as exc:  # noqa: BLE001 — one bad case must not end the run
        result.error = f"submit failed: {exc}"
        result.notes.append(traceback.format_exc(limit=3))
        return result

    result.http_status = response.status_code
    if response.status_code >= 400:
        result.phase = "rejected"
        result.elapsed_s = time.monotonic() - started
        result.error = _short(response.text)
        (out_dir / "response.json").write_text(
            json.dumps(
                {"status": response.status_code, "body": _json_or_text(response)},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
            newline="\n",
        )
        return result

    job_id = response.json().get("job_id")
    result.job_id = job_id
    if not job_id:
        result.error = "202 with no job_id"
        return result

    try:
        job = poll(transport, job_id, args.timeout)
    except Exception as exc:  # noqa: BLE001
        result.error = f"poll failed: {exc}"
        return result

    result.elapsed_s = time.monotonic() - started
    result.job = job
    result.phase = job.get("phase") or "unknown"
    result.result = job.get("result") or {}
    if job.get("error"):
        result.error = _short(str(job["error"]))

    _write_json(out_dir / "job.json", job)

    manifest, files, notes = fetch_artifacts(transport, job_id, out_dir, result.result)
    result.manifest = manifest
    result.files = files
    result.notes.extend(notes)
    regions_path = out_dir / "regions.json"
    if regions_path.is_file():
        result.regions_doc = json.loads(regions_path.read_text(encoding="utf-8"))

    try:
        result.annotated = annotate_case(result)
    except Exception as exc:  # noqa: BLE001
        result.notes.append(f"annotation failed: {exc}")

    if not args.keep_jobs:
        transport.request("DELETE", f"/v1/classifier/jobs/{job_id}")

    return result


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------


def criterion_results(result: CaseResult) -> dict[str, dict]:
    """The per-criterion block of the result."""
    return dict(
        (result.result.get("assessment") or {}).get("per_criterion_scores") or {}
    )


def overall(result: CaseResult) -> tuple[Optional[str], Optional[float]]:
    """(verdict, score) for the job as a whole — both None when nothing was
    scored (every criterion ``score: false``) or the assessment is incomplete."""
    payload = result.result
    assessment = payload.get("assessment") or {}
    return (
        payload.get("verdict") or assessment.get("overall_verdict"),
        assessment.get("overall_score"),
    )


def page_geometries(result: CaseResult) -> list[PageGeometry]:
    """One frame per item with a page image (``page`` is the item index).

    The result's ``page_geometry`` has an entry for EVERY item — a .txt or
    .docx item's with null sizes — so those are left out; regions.json's
    ``pages`` is the fallback for a result that did not come back.
    """
    raw = result.result.get("page_geometry")
    if isinstance(raw, dict):  # a schema-2 result
        raw = [raw]
    entries = [e for e in (raw or []) if e.get("width")] or (result.regions_doc.get("pages") or [])
    return [PageGeometry.from_dict(entry) for entry in entries]


def item_sources(result: CaseResult) -> dict[int, tuple[Optional[pathlib.Path], int, str]]:
    """Item n → (the fixture its page comes from, the page within it, label).

    The result's ``items`` say which document and page each item is, and
    ``documents`` name the files. A document is matched to a fixture by file
    name — the upload order is not the document order when inline ``text``
    fields are sent too, since HTTP clients put plain fields before files —
    falling back to position among the case's fixtures.
    """
    docs = result.result.get("documents") or []
    by_name: dict[str, list[pathlib.Path]] = {}
    for path in result.case.subjects:
        by_name.setdefault(path.name, []).append(path)
    fixture_for: dict[int, Optional[pathlib.Path]] = {}
    unmatched = [p for p in result.case.subjects]
    for doc in docs:
        candidates = by_name.get(doc.get("filename") or "") or []
        path = candidates.pop(0) if candidates else None
        if path is not None and path in unmatched:
            unmatched.remove(path)
        fixture_for[int(doc.get("index", 0))] = path
    for index, path in list(fixture_for.items()):
        if path is None and docs[index].get("kind") not in ("txt", "docx") and unmatched:
            fixture_for[index] = unmatched.pop(0)

    out: dict[int, tuple[Optional[pathlib.Path], int, str]] = {}
    for entry in result.result.get("items") or []:
        n, d, page = int(entry["item"]), int(entry.get("document", 0)), int(entry.get("page", 0))
        out[n] = (fixture_for.get(d), page, f"item {n} — {entry.get('filename')} p{page + 1}")
    if not out and result.case.subject:  # no result: assume the one document
        for geometry in page_geometries(result):
            out[geometry.page] = (result.case.subject, geometry.page, f"page {geometry.page}")
    return out


def _caption(region: Region, scores: dict[str, dict]) -> str:
    """``"<criterion> · <score> <verdict> · <source>"``, plus the loop's state."""
    entry = scores.get(region.label) or {}
    bits = [region.label]
    score, verdict = entry.get("score"), entry.get("verdict")
    if score is not None and verdict:
        bits.append(f"{score} {verdict}")
    elif verdict:
        bits.append(str(verdict))
    elif region.score is not None:
        bits.append(f"{region.score:.3g}")
    bits.append(region.source)

    attempt = region.attrs.get("attempt")
    if attempt is not None:
        if region.attrs.get("accepted"):
            verify = region.attrs.get("verify_score")
            bits.append(f"attempt {attempt} verify={verify}" if verify is not None
                        else f"attempt {attempt} accepted")
        else:
            bits.append(f"attempt {attempt} ✗")
        if region.attrs.get("refined"):
            bits.append("refined")
    return " · ".join(bits)


def collect_regions(result: CaseResult) -> dict[int, list[Region]]:
    """Every region the job produced, by page, with the LLM attempts added.

    Two sources are merged, and the merge is the interesting part:

      * ``regions.json`` carries the stored regions, including the rejected
        LLM boxes — but already **clamped** to the page;
      * ``localization.attempts[*].bbox_grid`` is what the model literally
        said, before clamping.

    Drawing the raw grid box is what makes "the model answered [0,0,1000,1000]"
    look like the full-frame claim it was, so the stored ``llm`` regions that
    carry an ``attempt`` are dropped in favour of the attempt list. Non-attempt
    ``llm`` regions (there should be none) are kept, because silently losing a
    region would defeat the purpose of the picture.

    The ACCEPTED attempt is the exception: it is drawn from ``bbox_px``, the
    box the service stored and cropped for the verify call (original page
    pixels, after the refine pass mapped it back), so the picture shows
    exactly what was verified. A rejected attempt keeps the raw ``bbox_grid``.
    """
    by_page: dict[int, list[Region]] = {}

    for name, block in (result.regions_doc.get("criteria") or {}).items():
        for raw in block.get("regions") or []:
            region = Region.from_dict(raw)
            region.label = region.label or name
            if region.source == "llm" and "attempt" in region.attrs:
                continue
            by_page.setdefault(region.page, []).append(region)

    geometries = {geom.page: geom for geom in page_geometries(result)}

    for name, entry in criterion_results(result).items():
        # Each unit (item) runs its own loop, so its attempts belong to its
        # own page; `items[].localization` carries them per item.
        per_item = [
            (unit.get("item"), unit.get("localization") or {})
            for unit in entry.get("items") or []
            if unit.get("item") is not None and unit.get("localization")
        ]
        if not per_item and entry.get("localization"):
            per_item = [(0, entry["localization"])]  # a schema-2 result
        for item, localization in per_item:
            for attempt in localization.get("attempts") or []:
                bbox = attempt.get("bbox_grid")
                geometry = geometries.get(item)
                if geometry is None:
                    continue
                stored = attempt.get("bbox_px")
                if attempt.get("accepted") and stored and len(stored) >= 4:
                    points = [(stored[0], stored[1]), (stored[2], stored[3])]
                elif bbox and len(bbox) >= 4:
                    points = grid_to_pixels(bbox, geometry)
                else:
                    continue
                by_page.setdefault(item, []).append(
                    Region(
                        page=item,
                        kind="box",
                        points=points,
                        label=name,
                        score=attempt.get("verify_score"),
                        source="llm",
                        attrs={
                            "attempt": attempt.get("attempt"),
                            "accepted": bool(attempt.get("accepted")),
                            "verify_score": attempt.get("verify_score"),
                            "reject": attempt.get("reject"),
                            "refined": bool(attempt.get("refined")),
                        },
                    )
                )

    return by_page


def page_image(source: pathlib.Path, page: int, geometry: PageGeometry) -> Optional[Any]:
    """The ORIGINAL fixture's page ``page`` (within that file), at the size
    the service reported for the item.

    A PDF (or an SVG — MuPDF opens both) is re-rendered locally rather than
    read back from the job's ``p{n}.base.jpg``: the base image is the
    service's own render, so drawing on it would reintroduce exactly the
    shared-source problem this script exists to avoid. The zoom is taken from
    ``page_geometry`` rather than from ``CLASSIFIER_PDF_RENDER_DPI`` so a
    container with a different DPI (or an SVG whose render was capped) still
    lines up.
    """
    from PIL import Image, ImageOps

    suffix = source.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        if page != 0:
            return None
        image = Image.open(source)
        image.load()
        return ImageOps.exif_transpose(image).convert("RGB")

    if suffix in (".pdf", ".svg"):
        import pymupdf

        with pymupdf.open(source) as doc:
            if page >= doc.page_count:
                return None
            pdf_page = doc.load_page(page)
            zoom = (
                geometry.width / pdf_page.rect.width
                if geometry.width and pdf_page.rect.width
                else 1.0
            )
            pixmap = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            return Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")

    return None


def annotate_case(result: CaseResult) -> list[str]:
    """Write one ``p{n}.annotated.jpg`` per item that has regions — each
    item's regions on its OWN page of its own fixture."""
    if not result.case.subjects:
        return []
    regions_by_page = collect_regions(result)
    if not regions_by_page:
        return []

    scores = criterion_results(result)
    sources = item_sources(result)
    written: list[str] = []
    for geometry in page_geometries(result):
        regions = regions_by_page.get(geometry.page) or []
        if not regions:
            continue
        source, page, label = sources.get(geometry.page, (None, 0, f"item {geometry.page}"))
        base = page_image(source, page, geometry) if source else None
        if base is None:
            result.notes.append(
                f"{label}: no local render for "
                f"{(source.suffix if source else '') or 'this kind'}, so no annotated image"
            )
            continue
        jpeg = annotate_to_jpeg(
            base,
            regions,
            geometry=geometry,
            labels=[_caption(r, scores) for r in regions],
        )
        name = f"p{geometry.page}.annotated.jpg"
        (result.out_dir / name).write_bytes(jpeg)
        written.append(name)
    return written


# ---------------------------------------------------------------------------
# Pipelines — the amount-due locator
# ---------------------------------------------------------------------------
#
# A Postman item is one fixed request. ``--pipeline utility-bill`` is the one
# case the collection cannot hold: a request built for whatever bill is
# passed on the command line, with a result the report reads more closely
# than a generic case. It is ONE call:
#
#   POST /assess   criteria = [{the amount-due line, type "llm", score: false,
#                               options: {hint "presence", boxes true}}]
#
# `score: false` locates without judging. The service still makes the one
# scoring call, because the enforcement loop gates on its presence score,
# then clears the judgement from the result. The loop runs — ask on the gridded page, refine on a
# zoomed crop, verify the crop — and returns every attempt. No judgement
# is reported, only geometry: which attempt was accepted, its box, and every
# rejected one. The report draws all of them on the original page (accepted
# dotted and filled — the stroke every `llm` region gets — rejected dashed and
# unfilled, straight from what the model said), plus a zoomed
# view of the accepted box so a reader can see whether it sits on the words.
#
# The expectation is a localisation check, not a model-judgement one: the
# accepted box must overlap at least one hand-measured amount-due line on
# the committed fixture (IoU >= BOX_MATCH_IOU). A bill prints the amount due
# more than once, so every copy is listed and any of them counts.
#
# Under --local there is no vision model: the one criterion is `llm`, the
# request is sent unchanged, and the criterion comes back status "error"
# naming the model — recorded as skipped, like every other all-`llm` case.

UTILITY_BILL_PIPELINE = "utility-bill"
PIPELINES = (UTILITY_BILL_PIPELINE,)
PIPELINE_FOLDER = "Pipeline: utility bill"
PIPELINE_EXPECT_KEY = "pipeline: utility bill"

# The bills the pipeline runs on when --document is not given.
UTILITY_BILL_FIXTURES = (
    REPO_ROOT / "unit-tests" / "classifier" / "documents" / "utility_bill.jpeg",
    REPO_ROOT / "unit-tests" / "classifier" / "documents" / "utility_bill_2.jpeg",
)

# The case name, suffixed with the document's file name, is the key into
# regions_expected.json — the same question on a different bill has a
# different right answer.
LOCATE_NAME = "utility bill — where is the amount due?"
AMOUNT_DUE_FEATURE = "the amount due line: the words 'Amount Due' next to the dollar figure owed"
LOCATE_CRITERIA: list[dict] = [
    {
        "name": AMOUNT_DUE_FEATURE,
        "type": "llm",
        "score": False,
        "options": {"hint": "presence", "boxes": True},
    },
]

# How much the accepted box has to overlap one of the expected boxes. 0.25 is
# the line the grounding measurements used for a "hit": a box on the right
# line that is a little wide or a little short clears it; a box on the line
# above does not.
BOX_MATCH_IOU = 0.25

# The zoomed view: the accepted box plus this fraction of the page on every
# side, so the line above and below are visible for context.
ZOOM_PAD = 0.04


def _locate_name(document: pathlib.Path) -> str:
    return f"{LOCATE_NAME} — {document.name}"


@dataclasses.dataclass
class PipelineStage:
    role: str                 # "locate"
    result: CaseResult
    decided: str = ""         # one line: what the call concluded


@dataclasses.dataclass
class PipelineResult:
    """What the one /assess call found, read out of its CaseResult."""

    name: str
    document: pathlib.Path
    stages: list[PipelineStage] = dataclasses.field(default_factory=list)
    attempts: list[dict] = dataclasses.field(default_factory=list)
    accepted: Optional[dict] = None        # the accepted attempt, as the service returned it
    calls: int = 0                          # model calls the loop made (ask/refine/verify)
    scoring_calls: int = 0                  # one scoring call per item, before the loop
    zoom: Optional[str] = None              # file name of the zoomed view
    zoom_window_px: Optional[list] = None   # [left, top, right, bottom] of the zoom, upright page px
    best_iou: Optional[float] = None        # accepted box vs the nearest expected box
    stopped_at: Optional[str] = None
    adhoc: bool = False
    notes: list[str] = dataclasses.field(default_factory=list)
    checks: list[dict] = dataclasses.field(default_factory=list)

    @property
    def expect_key(self) -> str:
        return f"{PIPELINE_EXPECT_KEY} — {self.document.name}"

    @property
    def results(self) -> list[CaseResult]:
        return [stage.result for stage in self.stages]

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)


def _compact(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"))


def run_utility_bill_pipeline(
    document: pathlib.Path,
    transport: Any,
    out_root: pathlib.Path,
    args: argparse.Namespace,
    *,
    first_index: int,
    adhoc: bool = False,
) -> PipelineResult:
    """Ask the model where the amount due is, and read back every attempt."""
    pipe = PipelineResult(name=UTILITY_BILL_PIPELINE, document=document, adhoc=adhoc)
    case = Case(
        name=_locate_name(document),
        folder=PIPELINE_FOLDER,
        index=first_index,
        method="POST",
        path="/v1/classifier/assess",
        description=(
            "One /assess call with one `llm` criterion, score: false and "
            "options.boxes on. The service makes the scoring call it gates on, "
            "clears the judgement, and runs the enforcement loop: ask on the page with a labelled 0-1000 grid drawn on it, refine on "
            "a zoomed crop of the original, verify the crop alone. Every attempt comes "
            "back; the accepted one is dotted and filled in the picture, rejected ones "
            "dashed and unfilled. "
            "Expect the accepted box to sit on one of the bill's 'Amount Due' lines."
        ),
        headers={},
        form={"criteria": _compact(LOCATE_CRITERIA)},
        uploads=[("file", document)],
        subjects=[document],
        adhoc=adhoc,
    )
    result = run_case(case, transport, out_root, args)

    if result.phase != "completed":
        pipe.stopped_at = f"the /assess job {result.phase}: {result.error or 'no error reported'}"
        pipe.stages.append(PipelineStage("locate", result, pipe.stopped_at))
        return pipe

    entry = criterion_results(result).get(AMOUNT_DUE_FEATURE) or {}
    if entry.get("status") == "error":
        pipe.stopped_at = f"the criterion errored: {entry.get('error') or entry.get('reason')}"
        pipe.stages.append(PipelineStage("locate", result, pipe.stopped_at))
        return pipe
    # Each item (page) ran its own loop, and its record is on its own unit —
    # one unit for a photo, whose record the criterion also carries verbatim
    # (an aggregate of one passes straight through); several for a PDF
    # --document, where the criterion's merged record has no single
    # accepted_attempt. Reading the units covers both the same way. Every
    # attempt is tagged with its item so the zoom knows which page to cut.
    units = [u for u in entry.get("items") or [] if u.get("item") is not None]
    per_item = [(int(u["item"]), u.get("localization") or {}) for u in units]
    if not per_item and entry.get("localization"):
        per_item = [(0, entry["localization"])]  # a pre-schema-3 result
    pipe.scoring_calls = len(units) or 1
    for item, localization in per_item:
        pipe.calls += int(localization.get("calls") or 0)
        accepted_n = localization.get("accepted_attempt")
        for attempt in localization.get("attempts") or []:
            tagged = {"item": item, **attempt}
            pipe.attempts.append(tagged)
            if (
                pipe.accepted is None
                and attempt.get("accepted")
                and attempt.get("attempt") == accepted_n
            ):
                pipe.accepted = tagged

    if pipe.accepted is None:
        pipe.stopped_at = (
            f"no box accepted after {len(pipe.attempts)} attempt(s)"
            if pipe.attempts
            else (entry.get("reason") or "the loop did not run for the feature")
        )
        pipe.stages.append(PipelineStage("locate", result, pipe.stopped_at))
        return pipe

    a = pipe.accepted
    pipe.stages.append(
        PipelineStage(
            "locate", result,
            f"attempt {a.get('attempt')} accepted"
            + (" (refined)" if a.get("refined") else "")
            + f", verify {a.get('verify_score')}, box {_fmt_box(a.get('bbox_grid') or [])} on the 0-1000 grid",
        )
    )
    try:
        zoomed = _zoom_view(result, a)
        if zoomed:
            pipe.zoom, pipe.zoom_window_px = zoomed
    except Exception as exc:  # noqa: BLE001 — a missing picture must not fail the run
        pipe.notes.append(f"zoomed view failed: {exc}")
    return pipe


def _fmt_box(box: list) -> str:
    return "[" + ", ".join(str(int(round(v))) for v in box) + "]"


def _attempt_boxes(attempt: dict) -> tuple[Optional[list], Optional[list]]:
    """(the model's first answer on the gridded page, the final box), both on
    the page's 0-1000 grid.

    With a refine pass, ``coarse_bbox_grid`` is the first answer and
    ``bbox_grid`` the final one (the refined box mapped back, or the coarse
    box again when the refine answer was unusable). Without one —
    refine off, or the first answer failed validation so there was nothing to
    refine — ``bbox_grid`` IS the first answer and is also the final box.
    """
    final = attempt.get("bbox_grid")
    first = attempt.get("coarse_bbox_grid") or final
    return first, final


def _box_iou(a: list, b: list) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _zoom_view(result: CaseResult, attempt: dict) -> Optional[tuple[str, list[int]]]:
    """Crop the ORIGINAL page around the accepted box and draw it, so the
    reader can see the words inside it — on a phone photo of a whole bill the
    full-page picture is too small to tell a line from the one above it."""
    from PIL import ImageDraw

    # The accepted box's own item: page `page` of the fixture it came from,
    # read upright (EXIF applied) — the frame `bbox_px` is in.
    item = int(attempt.get("item") or 0)
    geometry = next((g for g in page_geometries(result) if g.page == item), None)
    source, page, _label = item_sources(result).get(item, (result.case.subject, 0, ""))
    box = attempt.get("bbox_px")
    if geometry is None or not box or source is None:
        return None
    image = page_image(source, page, geometry)
    if image is None:
        return None
    sx = image.width / geometry.width if geometry.width else 1.0
    sy = image.height / geometry.height if geometry.height else 1.0
    x1, y1, x2, y2 = box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy
    pad_x, pad_y = image.width * ZOOM_PAD, image.height * ZOOM_PAD
    left, top = int(max(0, x1 - pad_x)), int(max(0, y1 - pad_y))
    right, bottom = int(min(image.width, x2 + pad_x)), int(min(image.height, y2 + pad_y))
    crop = image.crop((left, top, right, bottom))
    draw = ImageDraw.Draw(crop)
    stroke = max(3, round(min(crop.size) / 120))
    draw.rectangle((x1 - left, y1 - top, x2 - left, y2 - top), outline=(0, 170, 60), width=stroke)
    name = "amount_due_zoom.jpg"
    crop.save(result.out_dir / name, format="JPEG", quality=90)
    return name, [left, top, right, bottom]


def check_pipeline(pipe: PipelineResult, expectations: dict, *, local: bool = False) -> None:
    """The localisation checks; the case's own checks come from check().

    An ad-hoc ``--document`` has no entry to be right or wrong against, so
    its missing entry is recorded as skipped — with what WAS found, so the
    run still says something — rather than failed or silently omitted.
    """
    result = pipe.stages[0].result if pipe.stages else None
    entry = criterion_results(result).get(AMOUNT_DUE_FEATURE) or {} if result else {}
    if local and (
        (result is not None and result.phase == "failed")
        or (
            entry.get("status") == "error"
            and "vision model" in str(entry.get("error") or "").lower()
        )
    ):
        pipe.checks.append(
            {
                "kind": "pipeline",
                "target": "amount-due box",
                "ok": True,
                "skipped": True,
                "expected": "an accepted box",
                "actual": "no vision model is reachable in --local mode",
            }
        )
        return

    expected = expectations.get(pipe.expect_key)
    if expected is None:
        pipe.checks.append(
            {
                "kind": "coverage",
                "target": pipe.expect_key,
                "ok": pipe.adhoc,
                "skipped": pipe.adhoc,
                "expected": "nothing (ad-hoc --document)" if pipe.adhoc
                else "an entry in regions_expected.json",
                "actual": (
                    f"accepted {_fmt_box(pipe.accepted.get('bbox_grid') or [])}"
                    if pipe.accepted else f"nothing accepted — {pipe.stopped_at}"
                ) if pipe.adhoc else "none",
            }
        )
        return

    if expected.get("box_accepted"):
        pipe.checks.append(
            {
                "kind": "pipeline",
                "target": "a box was accepted",
                "ok": pipe.accepted is not None,
                "expected": True,
                "actual": True if pipe.accepted else pipe.stopped_at,
            }
        )

    boxes = expected.get("amount_due_boxes_grid") or []
    if boxes:
        if pipe.accepted is None or not pipe.accepted.get("bbox_grid"):
            pipe.checks.append(
                {
                    "kind": "pipeline",
                    "target": "the box sits on an amount-due line",
                    "ok": False,
                    "expected": f"IoU >= {BOX_MATCH_IOU} with one of {len(boxes)} expected box(es)",
                    "actual": pipe.stopped_at or "no accepted box",
                }
            )
        else:
            got = pipe.accepted["bbox_grid"]
            scored = sorted(((_box_iou(got, b), b) for b in boxes), key=lambda t: -t[0])
            pipe.best_iou = round(scored[0][0], 3)
            pipe.checks.append(
                {
                    "kind": "pipeline",
                    "target": "the box sits on an amount-due line",
                    "ok": scored[0][0] >= BOX_MATCH_IOU,
                    "expected": f"IoU >= {BOX_MATCH_IOU} with one of {len(boxes)} expected box(es)",
                    "actual": f"IoU {pipe.best_iou} with {_fmt_box(scored[0][1])} (box {_fmt_box(got)})",
                }
            )


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------


def check(result: CaseResult, expectations: dict, *, local: bool = False) -> None:
    """Compare the case against its expectations entry, filling ``checks``.

    Three assertions, and a deliberate hole in the middle of them:

      * ``http_status`` — for the cases whose whole point is a 400;
      * ``verdict`` — the overall verdict, when it is deterministic;
      * per criterion ``status`` (ok | error | skipped), ``verdict`` and
        ``min_regions`` — plus, for a request with several items,
        ``item_verdicts`` (one verdict, or status, per unit in order; ``null``
        asserts nothing) and ``region_pages`` (the items its regions landed
        on).

    A criterion expectation of ``"verdict": null`` asserts nothing. That is
    how every `llm` row is written, because its score depends on a model and a
    suite that claims to verify a model's judgement against a fixed answer is
    lying. ``min_regions`` is still asserted for those, but only as ``0`` —
    "you may produce boxes, and if you do they will be drawn" — so nothing
    false is claimed either way.
    """
    expected = expectations.get(result.case.expect_key or result.case.name)
    if expected is None and result.case.adhoc:
        result.checks.append(
            {
                "kind": "coverage",
                "target": result.case.name,
                "ok": True,
                "skipped": True,
                "expected": "nothing (ad-hoc --document)",
                "actual": f"{result.phase}; see the pipeline section for what was read",
            }
        )
        return
    if expected is None:
        result.checks.append(
            {
                "kind": "coverage",
                "target": result.case.name,
                "ok": False,
                "expected": "an entry in regions_expected.json",
                "actual": "none",
            }
        )
        return

    if "http_status" in expected:
        result.checks.append(
            {
                "kind": "http",
                "target": "status",
                "ok": result.http_status == expected["http_status"],
                "expected": expected["http_status"],
                "actual": result.http_status,
            }
        )
        return

    if result.phase != "completed":
        result.checks.append(
            {
                "kind": "phase",
                "target": "job",
                "ok": False,
                "expected": "completed",
                "actual": f"{result.phase}: {result.error or 'no error reported'}",
            }
        )
        return

    # A local run has no vision model. A criterion that still reaches it — a
    # request whose criteria are ALL `llm`, or a `cv` name with no OpenCV
    # detector falling back to the model — comes back status: "error", and
    # the assessment is then incomplete (overall verdict null). That is the
    # expected local outcome, not a regression in what this suite measures,
    # so those checks are recorded as SKIPPED. The guard is narrow on purpose:
    # only in `--local`, and only for criteria whose error names the model.
    # The same failure against the box is a real failure.
    scores = criterion_results(result)
    model_down = {
        name for name, entry in scores.items()
        if local and entry.get("status") == "error"
        and "vision model" in str(entry.get("error") or "").lower()
    }

    verdict, _score = overall(result)
    if expected.get("verdict") and model_down:
        result.checks.append(
            {
                "kind": "verdict",
                "target": "overall",
                "ok": True,
                "skipped": True,
                "expected": expected["verdict"],
                "actual": f"incomplete: {sorted(model_down)} could not reach a vision model in --local mode",
            }
        )
    elif expected.get("verdict"):
        result.checks.append(
            {
                "kind": "verdict",
                "target": "overall",
                "ok": verdict == expected["verdict"],
                "expected": expected["verdict"],
                "actual": verdict,
            }
        )

    for name, rule in (expected.get("criteria") or {}).items():
        entry = scores.get(name)
        if entry is None:
            skipped = name in result.dropped_criteria
            result.checks.append(
                {
                    "kind": "criterion",
                    "target": name,
                    "ok": True if skipped else False,
                    "skipped": skipped,
                    "expected": rule,
                    "actual": "dropped by --local mode (no vision model)"
                    if skipped
                    else "missing from the result",
                }
            )
            continue

        if name in model_down:
            result.checks.append(
                {
                    "kind": "criterion",
                    "target": name,
                    "ok": True,
                    "skipped": True,
                    "expected": rule,
                    "actual": "status: error — no vision model is reachable in --local mode",
                }
            )
            continue
        if rule.get("status"):
            result.checks.append(
                {
                    "kind": "criterion",
                    "target": f"{name} · status",
                    "ok": entry.get("status") == rule["status"],
                    "expected": rule["status"],
                    "actual": entry.get("status"),
                }
            )
        if rule.get("verdict"):
            result.checks.append(
                {
                    "kind": "criterion",
                    "target": f"{name} · verdict",
                    "ok": entry.get("verdict") == rule["verdict"],
                    "expected": rule["verdict"],
                    "actual": entry.get("verdict"),
                }
            )
        if "min_regions" in rule:
            count = len(entry.get("regions") or [])
            result.checks.append(
                {
                    "kind": "criterion",
                    "target": f"{name} · regions",
                    "ok": count >= rule["min_regions"],
                    "expected": f">= {rule['min_regions']}",
                    "actual": count,
                }
            )
        if "item_verdicts" in rule:
            # One entry per unit, in order: a verdict, or a status for a unit
            # that did not answer ("skipped" / "error"); null asserts nothing.
            got = [
                unit.get("verdict") if unit.get("status") == "ok" else unit.get("status")
                for unit in entry.get("items") or []
            ]
            want = list(rule["item_verdicts"])
            ok = len(got) == len(want) and all(w is None or w == g for w, g in zip(want, got))
            result.checks.append(
                {
                    "kind": "criterion",
                    "target": f"{name} · per item",
                    "ok": ok,
                    "expected": want,
                    "actual": got,
                }
            )
        if "region_pages" in rule:
            pages = sorted({r.get("page", 0) for r in entry.get("regions") or []})
            result.checks.append(
                {
                    "kind": "criterion",
                    "target": f"{name} · region pages",
                    "ok": pages == sorted(rule["region_pages"]),
                    "expected": sorted(rule["region_pages"]),
                    "actual": pages,
                }
            )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def region_stats(result: CaseResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for block in (result.regions_doc.get("criteria") or {}).values():
        for raw in block.get("regions") or []:
            counts[raw.get("source", "?")] = counts.get(raw.get("source", "?"), 0) + 1
    return dict(sorted(counts.items()))


def llm_stats(result: CaseResult) -> tuple[int, int]:
    """(attempts, accepted) across every criterion and item — a criterion's
    localization is already the merge of its items'."""
    attempts = accepted = 0
    for entry in criterion_results(result).values():
        for attempt in (entry.get("localization") or {}).get("attempts") or []:
            attempts += 1
            accepted += 1 if attempt.get("accepted") else 0
    return attempts, accepted


def detector_calls(result: CaseResult) -> int:
    return int((result.result.get("detector") or {}).get("calls") or 0)


def text_links(entry: dict) -> list[dict]:
    """A criterion's text-layer links — a list (one per unit) in schema 3."""
    text = (entry.get("artifacts") or {}).get("text") or []
    return [text] if isinstance(text, dict) else list(text)


def link_file(link: dict) -> str:
    """The artifact file name a text link points at (``text.p0.auto.json``)."""
    url = link.get("url") or ""
    return url.rsplit("/", 1)[-1] if url else f"text.{link.get('key')}.json"


def write_summary_json(
    path: pathlib.Path,
    results: list[CaseResult],
    meta: dict,
    pipelines: Iterable[PipelineResult] = (),
) -> None:
    payload = {
        "generated_at": meta["generated_at"],
        "base_url": meta["base_url"],
        "mode": meta["mode"],
        "collection": meta["collection"],
        "expectations": meta["expectations"],
        "items": [],
        "pipelines": [_pipeline_summary(pipe) for pipe in pipelines],
    }
    for result in results:
        verdict, score = overall(result)
        payload["items"].append(
            {
                "name": result.case.name,
                "folder": result.case.folder,
                "endpoint": result.case.endpoint,
                "slug": result.case.slug,
                "phase": result.phase,
                "http_status": result.http_status,
                "elapsed_s": round(result.elapsed_s, 2),
                "overall": {"verdict": verdict, "score": score},
                "criteria": {
                    name: {
                        "status": entry.get("status"),
                        "method": entry.get("method"),
                        "score": entry.get("score"),
                        "verdict": entry.get("verdict"),
                        "confidence": entry.get("confidence"),
                        "regions": len(entry.get("regions") or []),
                        "pages": sorted(
                            {r.get("page", 0) for r in entry.get("regions") or []}
                        ),
                        "localization": _localization_summary(entry),
                        "text_layers": [link_file(link) for link in text_links(entry)],
                        "complete": entry.get("complete"),
                        "aggregate_used": entry.get("aggregate_used"),
                        "items": [
                            {
                                "item": unit.get("item"),
                                "document": unit.get("document"),
                                "page": unit.get("page"),
                                "status": unit.get("status"),
                                "score": unit.get("score"),
                                "verdict": unit.get("verdict"),
                                "regions": unit.get("regions"),
                            }
                            for unit in entry.get("items") or []
                        ],
                    }
                    for name, entry in criterion_results(result).items()
                },
                "documents": [
                    {k: d.get(k) for k in ("index", "filename", "kind", "pages", "items")}
                    for d in result.result.get("documents") or []
                ],
                "item_scores": result.result.get("items") or [],
                "regions_by_source": region_stats(result),
                "detector_calls": detector_calls(result),
                "files": result.files,
                "annotated": result.annotated,
                "notes": result.notes,
                "error": result.error,
                "checks": result.checks,
            }
        )
    _write_json(path, payload)


def _localization_summary(entry: dict) -> Optional[dict]:
    localization = entry.get("localization")
    if not localization:
        return None
    attempts = localization.get("attempts") or []
    accepted = localization.get("accepted_attempt")
    if accepted is None and localization.get("accepted"):
        # Several items merged: no single attempt number, one per item instead.
        accepted = ", ".join(
            f"item {a.get('item')} #{a.get('attempt')}" for a in localization["accepted"]
        )
    return {
        "attempts": len(attempts),
        "accepted_attempt": accepted,
        "calls": localization.get("calls"),
    }


def tally(
    results: list[CaseResult], pipelines: Iterable[PipelineResult] = ()
) -> tuple[int, int, int]:
    """(met, total, skipped) across every case's checks, plus the chain-level
    checks of any pipeline (its stages are already in ``results``).

    A skipped check counts as met but is reported separately — "18/18" with
    six of them silently skipped is the kind of green that hides a hole.
    """
    checks = [c for result in results for c in result.checks]
    checks += [c for pipe in pipelines for c in pipe.checks]
    met = sum(1 for c in checks if c["ok"])
    skipped = sum(1 for c in checks if c.get("skipped"))
    return met, len(checks), skipped


def _pipeline_summary(pipe: PipelineResult) -> dict:
    result = pipe.stages[0].result if pipe.stages else None
    return {
        "name": pipe.name,
        "document": _rel(pipe.document),
        "feature": AMOUNT_DUE_FEATURE,
        "slug": result.case.slug if result else None,
        "phase": result.phase if result else None,
        "elapsed_s": round(result.elapsed_s, 2) if result else None,
        "stopped_at": pipe.stopped_at,
        "accepted": pipe.accepted,
        "attempts": pipe.attempts,
        "calls": pipe.calls,
        "scoring_calls": pipe.scoring_calls,
        "best_iou": pipe.best_iou,
        "zoom": pipe.zoom,
        "zoom_window_px": pipe.zoom_window_px,
        "notes": pipe.notes,
        "checks": pipe.checks,
    }


def print_pipelines(pipelines: Iterable[PipelineResult]) -> None:
    for pipe in pipelines:
        print()
        print(f"pipeline {pipe.name} — {pipe.document.name}")
        multi = len({a.get("item") for a in pipe.attempts}) > 1
        for attempt in pipe.attempts:
            first, final = _attempt_boxes(attempt)
            where = f"item {attempt.get('item')} " if multi else ""
            print(
                f"  {where}attempt {attempt.get('attempt')}: "
                + (f"{_fmt_box(first)}" if first else "no box")
                + (f" -> refined {_fmt_box(final)}" if attempt.get("refined") and final else "")
                + f"  verify={attempt.get('verify_score')}"
                + f"  {'ACCEPTED' if attempt.get('accepted') else 'rejected'}"
            )
        if pipe.accepted:
            print(
                f"  amount due located: {_fmt_box(pipe.accepted.get('bbox_grid') or [])} "
                f"(attempt {pipe.accepted.get('attempt')}, "
                f"{pipe.scoring_calls} scoring + {pipe.calls} loop model call(s)"
                + (f", IoU {pipe.best_iou}" if pipe.best_iou is not None else "")
                + ")"
            )
        else:
            print(f"  amount due not located — {pipe.stopped_at}")
        for check_row in (c for c in pipe.checks if not c["ok"]):
            print(
                f"      ! {check_row['target']}: expected {check_row['expected']!r}, "
                f"got {check_row['actual']!r}"
            )
        for note in pipe.notes:
            print(f"      - {note}")


def print_summary(
    results: list[CaseResult], pipelines: Iterable[PipelineResult] = ()
) -> tuple[int, int, int]:
    """Print the stdout table and the pipeline lines; return :func:`tally`."""
    header = ("item", "endpoint", "phase", "verdict", "regions", "llm a/ok", "elapsed", "checks")
    widths = (48, 17, 10, 9, 22, 9, 8, 9)
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths))
    print(line)
    print("-" * len(line))

    for result in results:
        verdict, _score = overall(result)
        attempts, accepted = llm_stats(result)
        stats = region_stats(result)
        failed = [c for c in result.checks if not c["ok"]]
        skipped = [c for c in result.checks if c.get("skipped")]
        if failed:
            state = f"{len(failed)} FAIL"
        elif skipped and len(skipped) == len(result.checks):
            state = "skipped"
        elif skipped:
            state = f"ok ({len(skipped)} skip)"
        else:
            state = "ok"
        cells = (
            _clip(result.case.name, widths[0]),
            _clip(result.case.endpoint, widths[1]),
            _clip(result.phase, widths[2]),
            _clip(str(verdict or "-"), widths[3]),
            _clip(
                ", ".join(f"{k}:{v}" for k, v in stats.items()) or "-", widths[4]
            ),
            _clip(f"{attempts}/{accepted}" if attempts else "-", widths[5]),
            _clip(f"{result.elapsed_s:.1f}s", widths[6]),
            _clip(state, widths[7]),
        )
        print("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
        for check_row in failed:
            print(
                f"      ! {check_row['target']}: expected {check_row['expected']!r}, "
                f"got {check_row['actual']!r}"
            )
        for note in result.notes:
            print(f"      - {note}")
    pipelines = list(pipelines)
    print_pipelines(pipelines)
    return tally(results, pipelines)


def _clip(text: str, width: int) -> str:
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 28px 64px; font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
       background: #fbfbfc; color: #16181d; }
h1 { font-size: 21px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 40px 0 6px; padding-top: 18px; border-top: 2px solid #d8dbe2; }
h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: #666c78;
     margin: 20px 0 6px; }
.meta { color: #666c78; font-size: 12.5px; margin-bottom: 20px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 14px; font-size: 12.5px; }
th, td { border: 1px solid #dde0e6; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #eef0f4; font-weight: 600; }
tbody tr:nth-child(even) { background: #f5f6f8; }
code, .mono { font-family: "Cascadia Mono", Consolas, ui-monospace, monospace; font-size: 12px; }
.pill { display: inline-block; padding: 1px 7px; border-radius: 9px; font-size: 11px;
        font-weight: 600; letter-spacing: .02em; }
.PASS { background: #d9f0dc; color: #14532d; }
.MARGINAL { background: #fdeccd; color: #7a4a03; }
.FAIL { background: #fadadd; color: #7d1220; }
.SKIPPED, .none { background: #e6e8ec; color: #52565f; }
.ok { color: #14532d; font-weight: 600; }
.bad { color: #a4162a; font-weight: 600; }
.skip { color: #6a6f79; }
.notes { background: #fff7e0; border-left: 3px solid #e0a800; padding: 7px 11px; margin: 8px 0;
         font-size: 12.5px; }
.err { background: #fdeaec; border-left: 3px solid #c23b4b; padding: 7px 11px; margin: 8px 0;
       font-size: 12.5px; }
.pages { display: flex; flex-wrap: wrap; gap: 18px; margin: 10px 0 4px; }
.pane { flex: 1 1 430px; min-width: 300px; }
.pane img { width: 100%; border: 1px solid #ccd0d8; border-radius: 3px; background: #fff; }
.pane .cap { font-size: 11.5px; color: #666c78; margin-bottom: 4px; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
          margin-right: 5px; vertical-align: -1px; }
.links a { margin-right: 12px; font-size: 12px; }
.desc { white-space: pre-wrap; font-size: 12.5px; color: #3c4149; background: #f2f3f6;
        border-radius: 4px; padding: 9px 12px; max-height: 190px; overflow: auto; }
.tally { font-size: 15px; font-weight: 600; margin: 14px 0 0; }
.answer { background: #e6f2ea; border-left: 3px solid #2f8f4e; padding: 9px 12px; margin: 10px 0;
          font-size: 14px; }
.answer strong { font-size: 17px; }
.flow { color: #666c78; font-size: 12.5px; margin: 2px 0 10px; }
@media (prefers-color-scheme: dark) {
  .answer { background: #14311f; border-left-color: #3fae63; }
  body { background: #14161a; color: #e6e8ec; }
  h2 { border-top-color: #2c3038; }
  th { background: #22262e; } tbody tr:nth-child(even) { background: #1a1d23; }
  th, td { border-color: #2c3038; }
  .desc { background: #1a1d23; color: #c2c7d0; }
  .pane img { background: #22262e; border-color: #2c3038; }
  .PASS { background: #14311f; color: #8fe0a8; } .FAIL { background: #3a1419; color: #f0a2ad; }
  .MARGINAL { background: #3a2c10; color: #f0cc84; } .SKIPPED, .none { background: #262a31; color: #a2a8b3; }
  .notes { background: #2a2413; border-left-color: #a08000; }
  .err { background: #2c1519; border-left-color: #a4162a; }
  .ok { color: #8fe0a8; } .bad { color: #f0a2ad; } .skip { color: #9aa0ab; }
}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _verdict_pill(verdict: Any) -> str:
    text = str(verdict or "—")
    css = text if text in ("PASS", "MARGINAL", "FAIL", "SKIPPED") else "none"
    return f'<span class="pill {css}">{_e(text)}</span>'


def _criteria_request_table(case: Case) -> str:
    rows = []
    for criterion in case.criteria:
        options = criterion.get("options") or {}
        rows.append(
            "<tr>"
            f"<td>{_e(criterion.get('name'))}</td>"
            f"<td>{_e(criterion.get('type') or 'llm')}</td>"
            f"<td>{'' if criterion.get('score', True) else 'false'}</td>"
            f"<td class='mono'>{_e(json.dumps(options, ensure_ascii=False) if options else '')}</td>"
            f"<td>{_e(criterion.get('weight') or '')}</td>"
            f"<td>{_e(criterion.get('depends_on') or '')}</td>"
            "</tr>"
        )
    if not rows:
        return "<p class='skip'>No criteria in this request (the service's defaults apply).</p>"
    return (
        "<table><thead><tr><th>criterion</th><th>type</th><th>score</th><th>options</th>"
        "<th>weight</th><th>depends_on</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _criteria_result_table(result: CaseResult, expectations: dict) -> str:
    expected = (expectations.get(result.case.name) or {}).get("criteria") or {}
    scores = criterion_results(result)
    if not scores:
        return "<p class='skip'>No per-criterion results (the job did not complete).</p>"

    rows = []
    for name, entry in scores.items():
        regions = entry.get("regions") or []
        pages = sorted({r.get("page", 0) for r in regions})
        localization = _localization_summary(entry)
        loc_text = (
            f"{localization['attempts']} attempt(s), accepted "
            f"{localization['accepted_attempt']}, {localization['calls']} call(s)"
            if localization
            else ""
        )
        rule = expected.get(name) or {}
        want = rule.get("verdict")
        if rule.get("status") and want is None:
            want_status = rule["status"]
            check_cell = (
                f"<span class='ok'>✓ {_e(want_status)}</span>"
                if entry.get("status") == want_status
                else f"<span class='bad'>✗ want {_e(want_status)}</span>"
            )
        elif want is None:
            check_cell = "<span class='skip'>not asserted</span>"
        elif entry.get("verdict") == want:
            check_cell = f"<span class='ok'>✓ {_e(want)}</span>"
        else:
            check_cell = f"<span class='bad'>✗ want {_e(want)}</span>"

        swatch = (
            f'<span class="swatch" style="background:{criterion_color(name)}"></span>'
            if regions
            else ""
        )
        cells = []
        for link in text_links(entry):
            text_file = link_file(link)
            label = text_file.removeprefix("text.").removesuffix(".json")
            cells.append(
                f'<a href="{_e(result.case.slug)}/{_e(text_file)}">{_e(label)}</a> '
                f"<span class='skip'>{_e(link.get('source'))}, {_e(link.get('chars'))} chars</span>"
                if text_file in result.files
                else _e(label)
            )
        text_cell = "<br>".join(cells)
        status = entry.get("status") or "ok"
        verdict_cell = (
            _verdict_pill(entry.get("verdict"))
            if status == "ok"
            else f'<span class="pill {"SKIPPED" if status == "skipped" else "FAIL"}">{_e(status)}</span>'
        )
        rows.append(
            "<tr>"
            f"<td>{swatch}{_e(name)}</td>"
            f"<td>{_e(entry.get('method'))}</td>"
            f"<td>{_e(entry.get('score'))}</td>"
            f"<td>{verdict_cell}</td>"
            f"<td>{_e(entry.get('confidence'))}</td>"
            f"<td>{_e(_short(_reason(entry), 220))}</td>"
            f"<td>{len(regions)}</td>"
            f"<td>{_e(', '.join(str(p) for p in pages))}</td>"
            f"<td>{_e(loc_text)}</td>"
            f"<td>{text_cell}</td>"
            f"<td>{check_cell}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>criterion</th><th>method</th><th>score</th><th>verdict</th>"
        "<th>conf</th><th>reason / detail / error</th><th>regions</th><th>pages</th>"
        "<th>localization</th><th>text searched</th><th>expected</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _items_table(result: CaseResult) -> str:
    """Every criterion's per-item results, one row per item (one column per
    criterion), with the item's own weighted score — so a multi-page or
    multi-document case shows WHICH page answered, not only the aggregate."""
    items = result.result.get("items") or []
    scores = criterion_results(result)
    if len(items) < 2 and not any(
        len(entry.get("items") or []) > 1 for entry in scores.values()
    ):
        return ""  # one item: the Result table above already is the item
    names = list(scores)
    head = "".join(f"<th>{_e(name)}</th>" for name in names)
    rows = []

    def cell(unit: Optional[dict]) -> str:
        if unit is None:
            return "<td class='skip'>—</td>"
        status = unit.get("status")
        if status != "ok":
            pill = f'<span class="pill {"SKIPPED" if status == "skipped" else "FAIL"}">{_e(status)}</span>'
        else:
            pill = _verdict_pill(unit.get("verdict")) + f" {_e(unit.get('score'))}"
        regions = unit.get("regions")
        return f"<td>{pill}{f' · {regions} region(s)' if regions else ''}</td>"

    for item in items:
        n = item.get("item")
        units = []
        for name in names:
            entry = scores[name]
            unit = next((u for u in entry.get("items") or [] if u.get("item") == n), None)
            if unit is None:  # a document-scope criterion: its unit covers this item
                unit = next((u for u in entry.get("items") or []
                             if u.get("item") is None and n in (u.get("items") or [])), None)
            units.append(cell(unit))
        rows.append(
            "<tr>"
            f"<td>{_e(n)}</td><td>{_e(item.get('filename'))} p{_e((item.get('page') or 0) + 1)}</td>"
            f"<td>{_verdict_pill(item.get('overall_verdict'))} {_e(item.get('overall_score') if item.get('overall_score') is not None else '')}"
            f"{'' if item.get('complete', True) else ' <span class=bad>incomplete</span>'}</td>"
            + "".join(units) + "</tr>"
        )
    used = "; ".join(
        f"{_e(name)}: pages {_e((entry.get('aggregate_used') or {}).get('pages'))}, "
        f"documents {_e((entry.get('aggregate_used') or {}).get('documents'))}"
        for name, entry in scores.items()
    )
    return (
        "<table><thead><tr><th>item</th><th>document · page</th><th>item score</th>"
        + head + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        + f"<p class='meta'>aggregate_used — {used}. A document-scope criterion's cell "
          "is its one unit for the whole document.</p>"
    )


def _reason(entry: dict) -> str:
    if entry.get("error"):
        return f"error: {entry['error']}"
    reason = entry.get("reason")
    if reason:
        return str(reason)
    detail = entry.get("detail")
    if isinstance(detail, dict):
        return json.dumps(detail, ensure_ascii=False)
    return str(detail or "")


def _pages_block(result: CaseResult) -> str:
    """Annotated page beside the service's own preview, per item."""
    if not result.annotated:
        return ""
    panes = []
    sources = item_sources(result)
    for name in sorted(result.annotated, key=lambda n: int(n.split(".")[0][1:])):
        page = name.split(".")[0]
        slug = result.case.slug
        service = f"{page}.preview.jpg"
        label = sources.get(int(page[1:]), (None, 0, page))[2]
        panes.append(f"<h3>{_e(label)}</h3>")
        right = (
            f'<div class="pane"><div class="cap">the service\'s own '
            f'<code>{_e(service)}</code></div>'
            f'<img src="{_e(slug)}/{_e(service)}" alt="{_e(service)}"></div>'
            if service in result.files
            else '<div class="pane"><div class="cap">the <code>preview</code> layer could '
            "not be fetched for this job</div></div>"
        )
        panes.append(
            f'<div class="pages"><div class="pane">'
            f'<div class="cap">re-drawn here from <code>regions.json</code> onto the '
            f"original fixture</div>"
            f'<img src="{_e(slug)}/{_e(name)}" alt="{_e(name)}"></div>{right}</div>'
        )
    return "".join(panes)


def _links_block(result: CaseResult) -> str:
    slug = result.case.slug
    links = []
    for name in sorted(result.files):
        if name.endswith(".svg") or name.endswith(".json"):
            links.append(f'<a href="{_e(slug)}/{_e(name)}">{_e(name)}</a>')
    if (result.out_dir / "job.json").is_file():
        links.append(f'<a href="{_e(slug)}/job.json">job.json</a>')
    if (result.out_dir / "response.json").is_file():
        links.append(f'<a href="{_e(slug)}/response.json">response.json</a>')
    return f'<div class="links">{"".join(links)}</div>' if links else ""


def _checks_table(checks: list[dict]) -> str:
    if not checks:
        return ""
    rows = []
    for check_row in checks:
        if check_row.get("skipped"):
            state = "<span class='skip'>skipped</span>"
        elif check_row["ok"]:
            state = "<span class='ok'>✓</span>"
        else:
            state = "<span class='bad'>✗</span>"
        rows.append(
            "<tr>"
            f"<td>{state}</td><td>{_e(check_row.get('target'))}</td>"
            f"<td>{_e(check_row.get('expected'))}</td><td>{_e(check_row.get('actual'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th></th><th>check</th><th>expected</th><th>actual</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _pipeline_block(pipe: PipelineResult) -> str:
    """The locator as one section: where the model put the amount due, every
    attempt it made, the page with all of them drawn, and a zoomed view of
    the accepted box. The call itself is an ordinary case section below."""
    result = pipe.stages[0].result if pipe.stages else None
    parts = [
        f'<h2 id="pipeline-{_e(pipe.name)}-{_e(_slug(pipe.document.name))}">'
        f"Where is the amount due? — {_e(pipe.document.name)}</h2>",
        f'<p class="meta">document <code>{_e(_rel(pipe.document))}</code> · one '
        f"<code>POST /assess</code>, one <code>llm</code> criterion with <code>score: false</code> and <code>boxes</code> on · "
        f"{pipe.scoring_calls} scoring + {pipe.calls} loop model call(s)"
        + (f' · <a href="#{_e(result.case.slug)}">the call</a>' if result else "")
        + "</p>",
    ]

    if pipe.accepted:
        a = pipe.accepted
        iou_text = f" · IoU {pipe.best_iou} with the nearest expected line" if pipe.best_iou is not None else ""
        parts.append(
            f'<div class="answer">located at <strong>{_e(_fmt_box(a.get("bbox_grid") or []))}</strong> '
            f"on the 0-1000 grid · attempt {_e(a.get('attempt'))}"
            f"{' · refined' if a.get('refined') else ''} · verify score {_e(a.get('verify_score'))}"
            f"{_e(iou_text)}</div>"
        )
        if a.get("verify_reason"):
            parts.append(f'<p class="meta">the verifier saw: {_e(a.get("verify_reason"))}</p>')
    else:
        parts.append(
            f'<div class="err"><strong>no box was accepted</strong> — {_e(pipe.stopped_at)}</div>'
        )
    for note in pipe.notes:
        parts.append(f'<div class="notes">{_e(note)}</div>')

    panes = []
    if result and result.annotated:
        name = sorted(result.annotated)[0]
        panes.append(
            f'<div class="pane"><div class="cap">every attempt, drawn on the original page — '
            f"accepted dotted and filled, rejected dashed and unfilled</div>"
            f'<img src="{_e(result.case.slug)}/{_e(name)}" alt="{_e(name)}"></div>'
        )
    if result and pipe.zoom:
        panes.append(
            f'<div class="pane"><div class="cap">the accepted box, zoomed on the original</div>'
            f'<img src="{_e(result.case.slug)}/{_e(pipe.zoom)}" alt="{_e(pipe.zoom)}"></div>'
        )
    if panes:
        parts.append('<div class="pages">' + "".join(panes) + "</div>")

    if pipe.attempts:
        parts.append("<h3>Attempts</h3>")
        multi = len({a.get("item") for a in pipe.attempts}) > 1
        rows = []
        for attempt in pipe.attempts:
            first, final = _attempt_boxes(attempt)
            state = (
                "<span class='ok'>accepted</span>" if attempt.get("accepted")
                else "<span class='bad'>rejected</span>"
            )
            if attempt.get("refined"):
                refined = "yes"
            elif attempt.get("refine_reject"):
                refined = f"no — {_e(_short(str(attempt['refine_reject']), 120))}"
            else:
                refined = ""  # no refine pass ran (refine off, or nothing valid to refine)
            why = attempt.get("reject") or attempt.get("verify_reason") or ""
            rows.append(
                "<tr>"
                + (f"<td>{_e(attempt.get('item'))}</td>" if multi else "")
                + f"<td>{_e(attempt.get('attempt'))}</td>"
                f"<td class='mono'>{_e(_fmt_box(first)) if first else '—'}</td>"
                f"<td class='mono'>{_e(_fmt_box(final)) if final else '—'}</td>"
                f"<td>{refined}</td>"
                f"<td>{_e(attempt.get('verify_score'))}</td>"
                f"<td>{state}</td>"
                f"<td>{_e(_short(str(why), 200))}</td>"
                "</tr>"
            )
        parts.append(
            "<table><thead><tr>" + ("<th>item</th>" if multi else "")
            + "<th>#</th><th>first answer</th><th>final box</th><th>refined</th>"
            "<th>verify</th><th></th><th>reason</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>"
            "<p class='meta'>Boxes are on the 0-1000 grid of the page. The first answer is what "
            "the model said on the gridded page; the final box is what was verified — its answer "
            "on the zoomed crop, mapped back, or the first answer again when no refine was used. "
            "The reason is why an attempt was rejected, or what the verifier saw in the crop.</p>"
        )

    if pipe.checks:
        parts.append("<h3>Checks</h3>")
        parts.append(_checks_table(pipe.checks))
    return "\n".join(parts)


def write_html(
    path: pathlib.Path,
    results: list[CaseResult],
    expectations: dict,
    meta: dict,
    pipelines: Iterable[PipelineResult] = (),
) -> None:
    pipelines = list(pipelines)
    met, total, skipped = tally(results, pipelines)

    head = [
        "<!-- generated by unit-tests/classifier/regions_report.py -->",
        f"<title>Classifier region accuracy — {_e(meta['generated_at'])}</title>",
        f"<style>{CSS}</style>",
        "<h1>Classifier region accuracy</h1>",
        f'<p class="meta">{_e(meta["generated_at"])} · mode <strong>{_e(meta["mode"])}</strong>'
        f' · {_e(meta["base_url"])} · collection <code>{_e(meta["collection"])}</code>'
        f' · expectations <code>{_e(meta["expectations"])}</code></p>',
    ]

    rows = []
    for result in results:
        verdict, score = overall(result)
        attempts, accepted = llm_stats(result)
        failed = [c for c in result.checks if not c["ok"]]
        stats = region_stats(result)
        rows.append(
            "<tr>"
            f'<td><a href="#{_e(result.case.slug)}">{_e(result.case.name)}</a><br>'
            f'<span class="skip">{_e(result.case.folder)}</span></td>'
            f"<td class='mono'>{_e(result.case.endpoint)}</td>"
            f"<td>{_e(result.phase)}{'' if result.http_status < 400 else f' ({result.http_status})'}</td>"
            f"<td>{_verdict_pill(verdict)}</td>"
            f"<td>{_e(score)}</td>"
            f"<td>{result.elapsed_s:.1f}s</td>"
            f"<td>{_e(', '.join(f'{k}:{v}' for k, v in stats.items()) or '—')}</td>"
            f"<td>{attempts}/{accepted}</td>"
            f"<td>{detector_calls(result)}</td>"
            + (
                f"<td class='ok'>{len(result.checks)} ok</td>"
                if not failed
                else f"<td class='bad'>{len(failed)} of {len(result.checks)} failed</td>"
            )
            + f"<td>{_e('; '.join(result.notes)[:200])}</td>"
            "</tr>"
        )
    head.append(
        "<table><thead><tr><th>item</th><th>endpoint</th><th>phase</th><th>verdict</th>"
        "<th>score</th><th>elapsed</th><th>regions by source</th><th>llm att/acc</th>"
        "<th>detector</th><th>expectations</th><th>notes</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    head.append(
        f'<p class="tally">{met}/{total} expectations met'
        + (f" ({skipped} skipped)" if skipped else "")
        + ("" if met == total else " — see the failing rows above")
        + "</p>"
    )

    body = [_pipeline_block(pipe) for pipe in pipelines]
    for result in results:
        verdict, score = overall(result)
        body.append(f'<h2 id="{_e(result.case.slug)}">{_e(result.case.name)}</h2>')
        body.append(
            f'<p class="meta">{_e(result.case.folder)} · '
            f"<code>POST {_e(result.case.endpoint)}</code> · "
            f"phase <strong>{_e(result.phase)}</strong> · "
            f"overall {_verdict_pill(verdict)} {_e(score if score is not None else '')} · "
            f"{result.elapsed_s:.1f}s"
            + (f' · job <code>{_e(result.job_id)}</code>' if result.job_id else "")
            + "</p>"
        )
        if result.error:
            body.append(f'<div class="err"><strong>error:</strong> {_e(result.error)}</div>')
        for note in result.notes:
            body.append(f'<div class="notes">{_e(note)}</div>')

        body.append("<h3>Request</h3>")
        documents = [f"<code>{_e(_rel(path))}</code>" for _field, path in result.case.uploads]
        documents += ["inline text"] * len(result.case.texts)
        if result.case.json_body is not None:
            documents = [f"<code>{_e(_rel(path))}</code>" for path in result.case.subjects] or documents
        body.append(
            "<p class='meta'>"
            + ("JSON body" if result.case.json_body is not None else "multipart")
            + (f" · {'document' if len(documents) == 1 else f'{len(documents)} documents'} "
               + ", ".join(documents) if documents else "")
            + "</p>"
        )
        body.append(_criteria_request_table(result.case))

        body.append("<h3>Result</h3>")
        body.append(_criteria_result_table(result, expectations))
        per_item = _items_table(result)
        if per_item:
            body.append("<h3>Per item</h3>")
            body.append(per_item)

        pages = _pages_block(result)
        if pages:
            body.append("<h3>Geometry, drawn twice</h3>")
            body.append(
                "<p class='meta'>Left: drawn by this script from <code>regions.json</code> "
                "onto the fixture on disk — per item, on that item's own page. Right: the "
                "service's own preview of the same item. They are produced by different "
                "code from the same numbers — a difference between them is itself the "
                "finding.</p>"
            )
            body.append(pages)
        body.append(_links_block(result))

        if result.case.description:
            body.append("<h3>What the collection says to look for</h3>")
            body.append(f'<div class="desc">{_e(result.case.description)}</div>')

    path.write_text("\n".join(head + body) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _write_json(path: pathlib.Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
        newline="\n",
    )


def _json_or_text(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return response.text


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rel(path: pathlib.Path) -> str:
    """Repo-relative when the path is inside the repo, else as given — a
    ``--document`` from Downloads or a crop under ``--out /tmp`` is neither
    an error nor something to hide."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See unit-tests/classifier/REGIONS_REPORT.md.",
    )
    parser.add_argument("--base-url", help="Classifier base URL, LiteLLM pass-through included "
                                           "(default: CLASSIFIER_BASE_URL, else "
                                           f"{DEFAULT_BASE_URL})")
    parser.add_argument("--api-key", help="Bearer token (default: CLASSIFIER_API_KEY, else "
                                          "DEFAULT_LITELLM_MASTER_KEY)")
    parser.add_argument("--collection", type=pathlib.Path, default=DEFAULT_COLLECTION,
                        help="Postman collection to run (default: the classifier's)")
    parser.add_argument("--folders", default=None,
                        help="Comma-separated collection folders to run (default: "
                             f"{','.join(DEFAULT_FOLDERS)} — or none at all when --pipeline "
                             "is given and this is not)")
    parser.add_argument("--only", help="Run only items whose name contains this substring")
    parser.add_argument("--pipeline", action="append", choices=PIPELINES, default=None,
                        help="Also run the amount-due locator (repeatable). `utility-bill`: one "
                             "/assess call per bill asking the vision model where the amount due "
                             "is, drawn on the page")
    parser.add_argument("--document", type=pathlib.Path, action="append", default=None,
                        help="Run --pipeline on this document instead of the committed "
                             "fixtures (repeatable). An ad-hoc document has no expectations: "
                             "its checks are recorded as skipped and its answer is reported")
    parser.add_argument("--out", type=pathlib.Path,
                        help="Output directory (default: "
                             "unit-tests/classifier/reports/<UTC timestamp>/)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="Seconds to wait for one job to reach a terminal phase")
    parser.add_argument("--expect", type=pathlib.Path, default=DEFAULT_EXPECTATIONS,
                        help="Expectations JSON")
    parser.add_argument("--parallel", type=int, default=1,
                        help="Cases in flight at once. Keep it at or below the box's "
                             "CLASSIFIER_MAX_CONCURRENT (4) — a deeper queue only "
                             "makes every elapsed number meaningless")
    parser.add_argument("--local", action="store_true",
                        help="Mount ai/classifier/main.py in-process instead of using "
                             "HTTP. No vision model or detector, so `llm` and "
                             "`detector` criteria are dropped")
    parser.add_argument("--keep-jobs", action="store_true",
                        help="Skip the DELETE /jobs/{id} cleanup at the end")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    # The summary table and several fixture names carry non-ASCII; a Windows
    # console defaults to cp1252 and would raise on the first em dash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)
    load_env()

    base_url = (args.base_url or os.environ.get("CLASSIFIER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    api_key = (
        args.api_key
        or os.environ.get("CLASSIFIER_API_KEY")
        or os.environ.get("DEFAULT_LITELLM_MASTER_KEY")
        or ""
    )

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_root = (args.out or (DEFAULT_REPORTS_DIR / stamp)).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    pipelines_wanted = list(dict.fromkeys(args.pipeline or []))
    if args.folders is None:
        folders = [] if pipelines_wanted else list(DEFAULT_FOLDERS)
    else:
        folders = [f.strip() for f in args.folders.split(",") if f.strip()]

    cases: list[Case] = []
    if folders:
        collection = load_collection(args.collection)
        cases = build_cases(
            collection,
            folders=folders,
            only=args.only,
            base_url=base_url,
            api_key=api_key,
        )
    if not cases and not pipelines_wanted:
        print("No matching items. Check --folders / --only against the collection.")
        return 2

    adhoc = args.document is not None
    documents = [p.resolve() for p in (args.document or UTILITY_BILL_FIXTURES)]
    if pipelines_wanted:
        for path in documents:
            if not path.is_file():
                raise SystemExit(f"--pipeline: document not found: {path}")

    expectations = (
        json.loads(args.expect.read_text(encoding="utf-8")) if args.expect.is_file() else {}
    )
    if not expectations:
        print(f"warning: no expectations loaded from {args.expect}")

    print(
        f"{len(cases)} case(s)"
        + (
            f" + pipeline {', '.join(pipelines_wanted)} on "
            f"{', '.join(d.name for d in documents)}{' (ad-hoc)' if adhoc else ''}"
            if pipelines_wanted else ""
        )
        + f" → {out_root}"
    )
    print(f"mode: {'local (in-process TestClient)' if args.local else base_url}\n")

    transport_factory = (
        (lambda: LocalTransport(out_root / "_service"))
        if args.local
        else (lambda: HttpTransport(base_url, api_key))
    )

    results: list[CaseResult] = []
    pipelines: list[PipelineResult] = []
    with transport_factory() as transport:
        if args.parallel > 1:
            # Keyed by position, not by Case: a dataclass with the default
            # ``eq=True`` has ``__hash__`` set to None, so a Case cannot be a
            # dict key. Position also keeps the report in collection order
            # regardless of which case finished first.
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
                futures = {
                    pool.submit(run_case, case, transport, out_root, args): i
                    for i, case in enumerate(cases)
                }
                done: dict[int, CaseResult] = {}
                for future in concurrent.futures.as_completed(futures):
                    index = futures[future]
                    done[index] = future.result()
                    print(f"  · {cases[index].name}", flush=True)
            results = [done[i] for i in range(len(cases))]
        else:
            for case in cases:
                print(f"  · {case.name}", flush=True)
                results.append(run_case(case, transport, out_root, args))

        # Pipelines run after the collection and strictly in sequence — each
        # stage's request is built from the last stage's answer.
        runners = {UTILITY_BILL_PIPELINE: run_utility_bill_pipeline}
        for name in pipelines_wanted:
            for document in documents:
                print(f"  · pipeline {name} ({document.name})", flush=True)
                pipe = runners[name](
                    document, transport, out_root, args,
                    first_index=len(results) + 1, adhoc=adhoc,
                )
                pipelines.append(pipe)
                results.extend(pipe.results)

    for result in results:
        check(result, expectations, local=args.local)
    for pipe in pipelines:
        check_pipeline(pipe, expectations, local=args.local)

    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "base_url": "in-process" if args.local else base_url,
        "mode": "local" if args.local else "http",
        "collection": args.collection.as_posix(),
        "expectations": args.expect.as_posix(),
    }
    write_html(out_root / "index.html", results, expectations, meta, pipelines)
    write_summary_json(out_root / "summary.json", results, meta, pipelines)

    print()
    met, total, skipped = print_summary(results, pipelines)
    print()
    print(
        f"{met}/{total} expectations met"
        + (f" ({skipped} skipped — see the notes above)" if skipped else "")
    )
    print(f"report: {(out_root / 'index.html').as_uri()}")
    return 0 if met == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
