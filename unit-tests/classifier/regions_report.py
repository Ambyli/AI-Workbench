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

The independence is the point. The service's own ``p0.preview.jpg`` was drawn
by the same code that produced the regions, so it cannot disagree with them.
The annotated JPEG next to it is drawn from ``regions.json`` by
``common.vision.annotate`` onto the fixture as it exists on disk — if the two
differ, one of them is wrong, and that is visible without reading a single
coordinate.

    submit → poll → GET artifacts → download layers
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
# DATA, not derivation: the variable names follow a convention
# (``scene_before_b64`` ↔ ``regions/scene_before.png``) but two of them cross
# fixture folders, and a convention that is right four times out of six is a
# convention that will silently encode the wrong file the first time someone
# adds the fifth. An unlisted ``*_b64`` variable is a hard error, not a guess.
B64_FIXTURES: dict[str, str] = {
    "scene_before_b64": "unit-tests/classifier/regions/scene_before.png",
    "scene_after_b64": "unit-tests/classifier/regions/scene_after.png",
    "invoice_native_b64": "unit-tests/classifier/documents/invoice_native.pdf",
    "invoice_scanned_b64": "unit-tests/classifier/documents/invoice_scanned.pdf",
}

# Artifact files worth pulling down. Everything else in the directory
# (``p{n}.base.jpg``, per-criterion pre-renders) is derivable from these.
ARTIFACT_PATTERNS = (
    "regions.json",
    "manifest.json",
    "e*.regions.json",
    "p*.svg",
    "p*.preview.jpg",
    "e*.p*.svg",
    "e*.preview.jpg",
    "diff-*.svg",
    "diff-*.jpg",
)

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
    upload: Optional[pathlib.Path] = None
    upload_field: str = "file"
    json_body: Optional[dict] = None
    subject: Optional[pathlib.Path] = None   # the document regions belong to

    @property
    def slug(self) -> str:
        return f"{self.index:02d}-{_slug(self.name)}"

    @property
    def endpoint(self) -> str:
        return "/" + "/".join(self.path.strip("/").split("/")[len(PASSTHROUGH_PREFIX):])

    @property
    def criteria_field(self) -> str:
        """Which field this endpoint spells its criteria list in."""
        return "features" if "features" in self.form else "criteria"

    @property
    def raw_criteria(self) -> list:
        """The criteria / features list exactly as the request carries it.

        Entries may be **bare strings** on `/locate`, and that matters: a bare
        string has no type because the SERVICE resolves it (cv → detector →
        llm). Normalising it to ``{"type": "llm"}`` here would make the local
        run drop ``"has text"``, which an OpenCV detector answers for free.
        """
        if self.json_body is not None:
            return list(self.json_body.get("criteria") or [])
        raw = self.form.get("criteria") or self.form.get("features")
        return json.loads(raw) if raw else []

    @property
    def criteria(self) -> list[dict]:
        """:attr:`raw_criteria` as dicts, for display. Bare strings are marked
        ``(resolved)`` rather than given a type they do not have."""
        return [
            {"name": c, "type": "(resolved)"} if isinstance(c, str) else c
            for c in self.raw_criteria
        ]

    def set_criteria(self, items: list) -> None:
        """Write a filtered criteria list back into whichever field holds it."""
        if self.json_body is not None:
            self.json_body["criteria"] = items
        else:
            self.form[self.criteria_field] = json.dumps(items, separators=(",", ":"))

    @property
    def regions_option(self) -> Any:
        if self.json_body is not None:
            return self.json_body.get("regions")
        return self.form.get("regions")


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
                case.upload_field = field["key"]
                case.upload = REPO_ROOT / field["src"]
                case.subject = case.upload
            else:
                case.form[field["key"]] = _substitute(
                    field.get("value") or "", base_url, api_key
                )
    elif body.get("mode") == "raw":
        raw = _substitute(body.get("raw") or "", base_url, api_key)
        case.json_body = json.loads(raw)
        # The subject of a compare is whichever fixture the `image` variable
        # stood for — needed to draw the regions on something.
        match = re.search(r"\{\{(\w+_b64)\}\}", body.get("raw") or "")
        if match:
            case.subject = REPO_ROOT / B64_FIXTURES[match.group(1)]
        # Content-Type is set by the client; leaving the collection's copy in
        # place is harmless but duplicated.
        headers.pop("Content-Type", None)

    if case.upload and not case.upload.is_file():
        raise SystemExit(f"{case.name}: fixture not found: {case.upload}")
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
    GPU anywhere. ``llm`` criteria have no server to call; see
    :func:`_strip_llm_criteria` for what happens to them.

    Every path the collection produces carries LiteLLM's ``/v1/classifier``
    prefix, which the pass-through supplies and a mounted app does not, so it
    is stripped here rather than in the collection parser — the collection is
    right about how the service is reached in production.
    """

    local = True

    def __init__(self, workdir: pathlib.Path):
        workdir.mkdir(parents=True, exist_ok=True)
        os.environ["DB_PATH"] = str(workdir / "classifier.db")
        os.environ["PAYLOAD_DIR"] = str(workdir / "payloads")
        os.environ["CLASSIFIER_ARTIFACT_DIR"] = str(workdir / "artifacts")
        os.environ.setdefault("CLASSIFIER_OCR_ENGINE", "rapidocr")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        # Both are network dependencies with nothing behind them here. Empty
        # DETECTOR_URL is "off", which the service reports as a note.
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
        self._client.__exit__(*exc)

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

    @property
    def kind(self) -> str:
        if self.case.endpoint == "/assess/compare":
            return "compare"
        if self.case.endpoint == "/locate":
            return "locate"
        return "assess"


def _strip_llm_criteria(case: Case, dropped: list[str]) -> Optional[str]:
    """Drop ``type: "llm"`` criteria for a ``--local`` run, in place.

    There is no vision model behind a locally mounted app, and ``call_vllm``
    raises a 502 rather than degrading — which would fail the whole job and
    take the `cv` / `text` / OCR geometry down with it, i.e. exactly the part
    that CAN be checked without a GPU.

    A request whose criteria are ALL ``llm`` is left alone instead: emptying
    the list would be a 400 at submit time, and a failed job that says
    "vLLM call failed" is a more honest local outcome than a request that was
    quietly rewritten into a different request.

    Args:
        case:    The case to trim, modified in place.
        dropped: Filled with the names that were removed, so the check pass
                 can tell "absent because we dropped it" from "absent
                 because the service lost it" — by then the request itself
                 no longer mentions them.

    Returns a note for the report, or None when nothing was dropped.
    """
    criteria = case.raw_criteria
    if not criteria:
        return None
    kept = [c for c in criteria if not _declares_llm(c)]
    if not kept:
        return (
            "local mode: every criterion is `llm` and there is no vision model, "
            "so the request was sent unchanged and the job is expected to fail"
        )
    if len(kept) == len(criteria):
        return None

    dropped.extend(c["name"] for c in criteria if _declares_llm(c))
    case.set_criteria(kept)
    return (
        f"local mode: dropped {len(dropped)} `llm` criteri{'on' if len(dropped) == 1 else 'a'} "
        f"({', '.join(dropped)}) — no vision model is reachable in-process"
    )


def _declares_llm(criterion: Any) -> bool:
    """True for an entry that will certainly reach the vision model.

    A bare string is not one: `/locate` resolves it to an OpenCV detector or
    the open-vocabulary detector first, and only falls through to the model.
    Dropping it locally would remove geometry that costs nothing to produce.
    """
    if isinstance(criterion, str):
        return False
    return (criterion.get("type") or "llm") == "llm"


def submit(case: Case, transport: Any) -> Any:
    """POST the case, returning the raw response."""
    if case.json_body is not None:
        return transport.request(
            "POST", case.path, json=case.json_body, headers=case.headers
        )
    files = None
    if case.upload:
        files = {
            case.upload_field: (
                case.upload.name,
                case.upload.read_bytes(),
                "application/octet-stream",
            )
        }
    return transport.request(
        "POST", case.path, data=case.form, files=files, headers=case.headers
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
    transport: Any, job_id: str, out_dir: pathlib.Path
) -> tuple[dict, list[str], list[str]]:
    """Download the layers worth keeping. A job without regions has none.

    A 404 here is the normal answer for a request submitted without
    ``regions`` — it means "never had artifacts", which is different from the
    410 that means "had them, they are gone". Both are reported, neither is an
    error.
    """
    notes: list[str] = []
    response = transport.request("GET", f"/v1/classifier/jobs/{job_id}/artifacts")
    if response.status_code == 404:
        return {}, [], ["no artifact directory (the request did not ask for regions)"]
    if response.status_code == 410:
        return {}, [], ["artifact directory is gone (swept past the TTL, or deleted)"]
    if response.status_code != 200:
        return {}, [], [f"GET /artifacts returned {response.status_code}"]

    manifest = response.json()
    saved: list[str] = []
    for entry in manifest.get("files") or []:
        name = entry.get("name") or ""
        if not any(fnmatch.fnmatch(name, pattern) for pattern in ARTIFACT_PATTERNS):
            continue
        file_response = transport.request(
            "GET", f"/v1/classifier/jobs/{job_id}/artifacts/{name}"
        )
        if file_response.status_code != 200:
            notes.append(f"{name}: download returned {file_response.status_code}")
            continue
        (out_dir / name).write_bytes(file_response.content)
        saved.append(name)
    return manifest, saved, notes


def run_case(
    case: Case, transport: Any, out_root: pathlib.Path, args: argparse.Namespace
) -> CaseResult:
    """Submit, poll, download, annotate — one case, start to finish."""
    out_dir = out_root / case.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    result = CaseResult(case=case, out_dir=out_dir)

    if transport.local:
        note = _strip_llm_criteria(case, result.dropped_criteria)
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

    manifest, files, notes = fetch_artifacts(transport, job_id, out_dir)
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
    """The per-criterion block, wherever this endpoint keeps it."""
    payload = result.result
    if result.kind == "locate":
        return dict(payload.get("features") or {})
    if result.kind == "compare":
        analysis = payload.get("input_analysis") or {}
        return dict((analysis.get("assessment") or {}).get("per_criterion_scores") or {})
    return dict((payload.get("assessment") or {}).get("per_criterion_scores") or {})


def overall(result: CaseResult) -> tuple[Optional[str], Optional[float]]:
    """(verdict, score) for the job as a whole, or (None, None) for /locate."""
    payload = result.result
    if result.kind == "locate":
        return None, None
    if result.kind == "compare":
        aggregate = payload.get("aggregate") or {}
        return aggregate.get("combined_verdict"), aggregate.get("combined_score")
    assessment = payload.get("assessment") or {}
    return (
        payload.get("verdict") or assessment.get("overall_verdict"),
        assessment.get("overall_score"),
    )


def page_geometries(result: CaseResult) -> list[PageGeometry]:
    raw = result.result.get("page_geometry") or result.regions_doc.get("pages") or []
    return [PageGeometry.from_dict(entry) for entry in raw]


def _caption(region: Region, scores: dict[str, dict]) -> str:
    """``"<criterion> · <score> <verdict> · <source>"``, plus the loop's state.

    Diff regions have no criterion and no score — they are filed under a
    synthetic ``_diff:e{i}`` key — so they are captioned by what changed,
    which is the only thing about them a reviewer can check.
    """
    if region.source == "diff":
        change = region.attrs.get("change") or "changed"
        return f"{change} · diff · {region.label.replace('_diff:', '')}"

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
    llm_page = result.result.get("document_info", {}).get("llm_image_page")
    default_page = llm_page if isinstance(llm_page, int) else 0

    for name, entry in criterion_results(result).items():
        localization = entry.get("localization") or {}
        for attempt in localization.get("attempts") or []:
            bbox = attempt.get("bbox_grid")
            if not bbox or len(bbox) < 4:
                continue
            geometry = geometries.get(default_page)
            if geometry is None:
                continue
            points = grid_to_pixels(bbox, geometry)
            by_page.setdefault(default_page, []).append(
                Region(
                    page=default_page,
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
                    },
                )
            )

    return by_page


def page_image(source: pathlib.Path, page: int, geometry: PageGeometry) -> Optional[Any]:
    """The ORIGINAL fixture's page, at the size the service reported.

    A PDF is re-rendered locally rather than read back from the job's
    ``p{n}.base.jpg``: the base image is the service's own render, so drawing
    on it would reintroduce exactly the shared-source problem this script
    exists to avoid. The zoom is taken from ``page_geometry`` rather than from
    ``CLASSIFIER_PDF_RENDER_DPI`` so a container with a different DPI still
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

    if suffix == ".pdf":
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
    """Write one ``p{n}.annotated.jpg`` per page that has regions."""
    if not result.case.subject:
        return []
    regions_by_page = collect_regions(result)
    if not regions_by_page:
        return []

    scores = criterion_results(result)
    written: list[str] = []
    for geometry in page_geometries(result):
        regions = regions_by_page.get(geometry.page) or []
        if not regions:
            continue
        base = page_image(result.case.subject, geometry.page, geometry)
        if base is None:
            result.notes.append(
                f"page {geometry.page}: no local render for "
                f"{result.case.subject.suffix or 'this kind'}, so no annotated image"
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
# Expectations
# ---------------------------------------------------------------------------


def check(result: CaseResult, expectations: dict, *, local: bool = False) -> None:
    """Compare the case against its expectations entry, filling ``checks``.

    Three assertions, and a deliberate hole in the middle of them:

      * ``http_status`` — for the cases whose whole point is a 400;
      * ``verdict`` — the overall verdict, when it is deterministic;
      * per criterion ``verdict`` and ``min_regions``.

    A criterion expectation of ``"verdict": null`` asserts nothing. That is
    how every `llm` row is written, because its score depends on a model and a
    suite that claims to verify a model's judgement against a fixed answer is
    lying. ``min_regions`` is still asserted for those, but only as ``0`` —
    "you may produce boxes, and if you do they will be drawn" — so nothing
    false is claimed either way.
    """
    expected = expectations.get(result.case.name)
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

    # A local run has no vision model. A request whose criteria are ALL `llm`
    # cannot be trimmed (that would be a 400), and one `cv` criterion the
    # detector service would have answered falls back to the model too — so
    # the job fails with a 502 from `call_vllm`. That is the expected local
    # outcome, not a regression in the thing this suite measures, so it is
    # recorded as SKIPPED rather than counted against the run. The guard is
    # narrow on purpose: only in `--local`, and only when the error names the
    # model server. The same failure against the box is a real failure.
    if local and result.phase == "failed" and "vllm" in (result.error or "").lower():
        result.checks.append(
            {
                "kind": "phase",
                "target": "job",
                "ok": True,
                "skipped": True,
                "expected": "completed",
                "actual": "failed: no vision model is reachable in --local mode",
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

    verdict, _score = overall(result)
    if expected.get("verdict"):
        result.checks.append(
            {
                "kind": "verdict",
                "target": "overall",
                "ok": verdict == expected["verdict"],
                "expected": expected["verdict"],
                "actual": verdict,
            }
        )

    scores = criterion_results(result)
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
    attempts = accepted = 0
    for entry in criterion_results(result).values():
        for attempt in (entry.get("localization") or {}).get("attempts") or []:
            attempts += 1
            accepted += 1 if attempt.get("accepted") else 0
    return attempts, accepted


def detector_calls(result: CaseResult) -> int:
    info = result.result.get("document_info") or {}
    return int((info.get("detector") or {}).get("calls") or 0)


def write_summary_json(path: pathlib.Path, results: list[CaseResult], meta: dict) -> None:
    payload = {
        "generated_at": meta["generated_at"],
        "base_url": meta["base_url"],
        "mode": meta["mode"],
        "collection": meta["collection"],
        "expectations": meta["expectations"],
        "items": [],
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
                        "method": entry.get("method"),
                        "score": entry.get("score"),
                        "verdict": entry.get("verdict"),
                        "confidence": entry.get("confidence"),
                        "regions": len(entry.get("regions") or []),
                        "pages": sorted(
                            {r.get("page", 0) for r in entry.get("regions") or []}
                        ),
                        "localization": _localization_summary(entry),
                    }
                    for name, entry in criterion_results(result).items()
                },
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
    return {
        "attempts": len(attempts),
        "accepted_attempt": localization.get("accepted_attempt"),
        "calls": localization.get("calls"),
    }


def tally(results: list[CaseResult]) -> tuple[int, int, int]:
    """(met, total, skipped) across every case's checks.

    A skipped check counts as met but is reported separately — "18/18" with
    six of them silently skipped is the kind of green that hides a hole.
    """
    checks = [c for result in results for c in result.checks]
    met = sum(1 for c in checks if c["ok"])
    skipped = sum(1 for c in checks if c.get("skipped"))
    return met, len(checks), skipped


def print_summary(results: list[CaseResult]) -> tuple[int, int, int]:
    """Print the stdout table; return :func:`tally`."""
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
    return tally(results)


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
@media (prefers-color-scheme: dark) {
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
        rows.append(
            "<tr>"
            f"<td>{_e(criterion.get('name'))}</td>"
            f"<td>{_e(criterion.get('type') or 'llm')}</td>"
            f"<td>{_e(criterion.get('hint') or '')}</td>"
            f"<td>{_e(criterion.get('match') or '')}</td>"
            f"<td class='mono'>{_e(criterion.get('pattern') or '')}</td>"
            f"<td>{_e(criterion.get('weight') or '')}</td>"
            f"<td>{_e(criterion.get('depends_on') or '')}</td>"
            "</tr>"
        )
    if not rows:
        return "<p class='skip'>No criteria in this request.</p>"
    return (
        "<table><thead><tr><th>criterion</th><th>type</th><th>hint</th><th>match</th>"
        "<th>pattern</th><th>weight</th><th>depends_on</th></tr></thead><tbody>"
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
        if want is None:
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
        rows.append(
            "<tr>"
            f"<td>{swatch}{_e(name)}</td>"
            f"<td>{_e(entry.get('method'))}</td>"
            f"<td>{_e(entry.get('score'))}</td>"
            f"<td>{_verdict_pill(entry.get('verdict'))}</td>"
            f"<td>{_e(entry.get('confidence'))}</td>"
            f"<td>{_e(_short(_reason(entry), 220))}</td>"
            f"<td>{len(regions)}</td>"
            f"<td>{_e(', '.join(str(p) for p in pages))}</td>"
            f"<td>{_e(loc_text)}</td>"
            f"<td>{check_cell}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>criterion</th><th>method</th><th>score</th><th>verdict</th>"
        "<th>conf</th><th>reason / detail</th><th>regions</th><th>pages</th>"
        "<th>localization</th><th>expected</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _reason(entry: dict) -> str:
    reason = entry.get("reason")
    if reason:
        return str(reason)
    detail = entry.get("detail")
    if isinstance(detail, dict):
        return json.dumps(detail, ensure_ascii=False)
    return str(detail or "")


def _pages_block(result: CaseResult) -> str:
    """Annotated page beside the service's own preview, per page."""
    if not result.annotated:
        return ""
    panes = []
    for name in sorted(result.annotated):
        page = name.split(".")[0]
        slug = result.case.slug
        service = f"{page}.preview.jpg"
        right = (
            f'<div class="pane"><div class="cap">the service\'s own '
            f'<code>{_e(service)}</code></div>'
            f'<img src="{_e(slug)}/{_e(service)}" alt="{_e(service)}"></div>'
            if service in result.files
            else '<div class="pane"><div class="cap">no <code>preview</code> layer was '
            "requested for this job</div></div>"
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


def _examples_block(result: CaseResult) -> str:
    """Per-example results and the diff layer, for a compare job."""
    examples = result.result.get("example_results") or []
    if not examples:
        return ""
    rows = []
    for example in examples:
        diff = example.get("diff") or {}
        changes = diff.get("changes") or {}
        rows.append(
            "<tr>"
            f"<td>e{_e(example.get('index'))}</td>"
            f"<td>{_e(example.get('weight'))}</td>"
            f"<td>{_e(example.get('pre_generated'))}</td>"
            f"<td>{_e((example.get('similarity') or {}).get('overall_similarity'))}</td>"
            f"<td>{_e(example.get('combined_score'))}</td>"
            f"<td>{_verdict_pill(example.get('combined_verdict'))}</td>"
            f"<td>{_e(diff.get('aligned'))}</td>"
            f"<td>{_e(diff.get('inliers'))}</td>"
            f"<td>{_e(', '.join(f'{k}:{v}' for k, v in changes.items()))}</td>"
            f"<td>{_e(_short(str(diff.get('note') or ''), 140))}</td>"
            "</tr>"
        )
    table = (
        "<h3>Examples and change detection</h3>"
        "<table><thead><tr><th>#</th><th>weight</th><th>pre-generated</th>"
        "<th>similarity</th><th>combined</th><th>verdict</th><th>aligned</th>"
        "<th>inliers</th><th>changes</th><th>note</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )

    slug = result.case.slug
    layers = [
        n for n in sorted(result.files)
        if n.startswith("diff-") or n.startswith("e")
    ]
    if layers:
        table += (
            '<div class="links">'
            + "".join(f'<a href="{_e(slug)}/{_e(n)}">{_e(n)}</a>' for n in layers)
            + "</div>"
        )
    previews = [n for n in sorted(result.files) if n.endswith(".preview.jpg") and n.startswith(("e", "diff-"))]
    if previews:
        table += '<div class="pages">' + "".join(
            f'<div class="pane"><div class="cap"><code>{_e(n)}</code></div>'
            f'<img src="{_e(slug)}/{_e(n)}" alt="{_e(n)}"></div>'
            for n in previews
        ) + "</div>"
    return table


def write_html(path: pathlib.Path, results: list[CaseResult], expectations: dict, meta: dict) -> None:
    met, total, skipped = tally(results)

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

    body = []
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
        options = result.case.regions_option
        body.append(
            "<p class='meta'>regions option: <code>"
            + _e(json.dumps(options) if isinstance(options, dict) else (options or "(off)"))
            + "</code>"
            + (f" · ocr <code>{_e(result.case.form.get('ocr'))}</code>" if result.case.form.get("ocr") else "")
            + (f" · file <code>{_e(result.case.upload.relative_to(REPO_ROOT).as_posix())}</code>" if result.case.upload else "")
            + "</p>"
        )
        body.append(_criteria_request_table(result.case))

        body.append("<h3>Result</h3>")
        body.append(_criteria_result_table(result, expectations))
        body.append(_examples_block(result))

        pages = _pages_block(result)
        if pages:
            body.append("<h3>Geometry, drawn twice</h3>")
            body.append(
                "<p class='meta'>Left: drawn by this script from <code>regions.json</code> "
                "onto the fixture on disk. Right: the service's own preview. They are "
                "produced by different code from the same numbers — a difference between "
                "them is itself the finding.</p>"
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
    parser.add_argument("--folders", default=",".join(DEFAULT_FOLDERS),
                        help="Comma-separated collection folders to run")
    parser.add_argument("--only", help="Run only items whose name contains this substring")
    parser.add_argument("--out", type=pathlib.Path,
                        help="Output directory (default: "
                             "unit-tests/classifier/reports/<UTC timestamp>/)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="Seconds to wait for one job to reach a terminal phase")
    parser.add_argument("--expect", type=pathlib.Path, default=DEFAULT_EXPECTATIONS,
                        help="Expectations JSON")
    parser.add_argument("--parallel", type=int, default=1,
                        help="Cases in flight at once. Keep it at or below the box's "
                             "CLASSIFIER_MAX_CONCURRENT (2) — a deeper queue only "
                             "makes every elapsed number meaningless")
    parser.add_argument("--local", action="store_true",
                        help="Mount ai/classifier/main.py in-process instead of using "
                             "HTTP. No vision model, so `llm` criteria are dropped")
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

    collection = load_collection(args.collection)
    cases = build_cases(
        collection,
        folders=[f.strip() for f in args.folders.split(",") if f.strip()],
        only=args.only,
        base_url=base_url,
        api_key=api_key,
    )
    if not cases:
        print("No matching items. Check --folders / --only against the collection.")
        return 2

    expectations = (
        json.loads(args.expect.read_text(encoding="utf-8")) if args.expect.is_file() else {}
    )
    if not expectations:
        print(f"warning: no expectations loaded from {args.expect}")

    print(f"{len(cases)} case(s) → {out_root}")
    print(f"mode: {'local (in-process TestClient)' if args.local else base_url}\n")

    transport_factory = (
        (lambda: LocalTransport(out_root / "_service"))
        if args.local
        else (lambda: HttpTransport(base_url, api_key))
    )

    results: list[CaseResult] = []
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

    for result in results:
        check(result, expectations, local=args.local)

    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "base_url": "in-process" if args.local else base_url,
        "mode": "local" if args.local else "http",
        "collection": args.collection.as_posix(),
        "expectations": args.expect.as_posix(),
    }
    write_html(out_root / "index.html", results, expectations, meta)
    write_summary_json(out_root / "summary.json", results, meta)

    print()
    met, total, skipped = print_summary(results)
    print()
    print(
        f"{met}/{total} expectations met"
        + (f" ({skipped} skipped — see the notes above)" if skipped else "")
    )
    print(f"report: {(out_root / 'index.html').as_uri()}")
    return 0 if met == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
