"""Shared helpers for the route-policy tests: conversation builders, the
baked-in rulesets, a synchronous engine runner, the app with no database,
and a fake document classifier (``FakeClassifier``: an ``httpx.MockTransport``
answering GET /models, POST /assess, GET /jobs/{id}, POST /jobs/{id}/cancel
and DELETE /jobs/{id} the way ai/classifier does, recording every request).

Imported by the test modules (conftest.py puts this directory on sys.path).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import socket
import threading
from typing import Any, Callable, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "route-policy"
RULESETS_DIR = SERVICE_DIR / "rulesets"

# The API contract (ROUTE_POLICY_PLAN § 7): exact key sets.
DECIDE_KEYS = {"decision_id", "route", "mode", "apply", "ruleset", "votes", "scores", "latency_ms"}
VOTE_KEYS = {"evaluator", "type", "route", "weight", "confidence", "role", "abstained", "reason", "latency_ms"}


def user(text: Any) -> dict:
    return {"role": "user", "content": text}


def assistant(text: str = "ok") -> dict:
    return {"role": "assistant", "content": text}


def system(text: str = "You are helpful.") -> dict:
    return {"role": "system", "content": text}


def tool_round(name: str, call_id: str = "c1") -> List[dict]:
    """An assistant tool call to ``name`` and its result."""
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "done"},
    ]


def seed():
    from route_policy.rulesets.loader import load_seed

    return load_seed(RULESETS_DIR)


def default_ruleset():
    (ruleset,) = [r for r in seed().compiled if r.id == "default"]
    return ruleset


def compile_doc(doc: Dict[str, Any]):
    from route_policy.rulesets.loader import resolve_refs
    from route_policy.rulesets.validate import compile_ruleset

    return compile_ruleset(resolve_refs(doc, RULESETS_DIR))


def context(messages: List[Any], requested_model: str = "auto", tools=None,
            backends: Optional[Dict[str, Dict[str, Any]]] = None):
    from route_policy.engine.context import BackendView, RequestContext

    views = {base: BackendView(base, **b) for base, b in (backends or {}).items()}
    return RequestContext(requested_model=requested_model, messages=messages, tools=tools, backends=views)


def run(ruleset, messages: List[Any], **kw):
    from route_policy.engine.pipeline import run as run_pipeline

    return asyncio.run(run_pipeline(ruleset, context(messages, **kw)))


def evaluate(evaluator, messages: List[Any], **kw):
    """One evaluator's Outcome on a conversation."""
    return asyncio.run(evaluator.evaluate(context(messages, **kw)))


def dbless_app():
    """The app with the baked-in rulesets preloaded and NO database: the
    lifespan touches nothing, writes are skipped (decide) or 503 (observe)."""
    from route_policy.app import create_app
    from route_policy.rulesets.store import RulesetStore
    from route_policy.service import Service

    s = seed()
    store = RulesetStore(None)
    for compiled in s.compiled:
        store.add(compiled)
    store.set_bindings(s.bindings)
    return create_app(service=Service(store=store, database=None, rulesets_dir=RULESETS_DIR),
                      instrument=False)


# -- a fake document classifier ------------------------------------------------------

# What the fake resolver answers: Docker-DNS-like private addresses for the
# local model services, a public one for an external host; anything else
# does not resolve.
ADDRESSES = {
    "qwen3.5-4b": ["172.18.0.5"],
    "qwen3.8-solo": ["172.18.0.7", "fd00::7"],
    "muse-glimmer": ["10.0.3.4"],
    "api.example.com": ["93.184.216.34"],
    "mixed.example.com": ["172.18.0.9", "93.184.216.35"],
}


def fake_resolver(host: str) -> List[str]:
    if host not in ADDRESSES:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return list(ADDRESSES[host])


def model_entry(model_id: str, host: Optional[str] = None, *, authenticated: bool = False,
                available: bool = True) -> Dict[str, Any]:
    """One GET /models entry, shaped like ai/classifier's."""
    return {"id": model_id, "served_name": model_id,
            "endpoint": {"scheme": "http", "host": host or model_id, "port": 8000},
            "authenticated": authenticated, "available": available,
            "unavailable_reason": None if available else "QWEN_API_KEY is not set",
            "vision": False, "max_concurrent": 2, "in_flight": 0}


class FakeClassifier:
    """The document classifier's job API, in memory.

    * ``models``: the GET /models list (default: the three local models plus
      an authenticated ``claude-like`` entry on api.example.com);
    * ``score``: criterion name -> 1..10, or None (that criterion errors);
      ``default_score`` for a name not listed;
    * ``waiting_polls`` / ``running_polls``: GET /jobs/{id} answers ``pending``
      this many times, then ``processing``, then the terminal phase
      (``final``: completed | failed); ``never_finish`` keeps it running;
    * ``assess_status`` / ``assess_detail``: answer POST /assess with this
      instead of 202; ``down``: every request is a connection error;
      ``delay_s``: every answer waits this long; ``delete_status``: what
      DELETE answers for a job it knows (204).

    Records ``requests`` (method, path), ``submitted`` (the /assess bodies),
    ``deleted`` and ``cancelled`` job ids. Thread-safe: an app under
    TestClient runs it on its own loop thread.
    """

    def __init__(self, **kw: Any) -> None:
        models = kw.pop("models", None)
        self.models: List[Dict[str, Any]] = models if models is not None else [
            model_entry("qwen3.5-4b"), model_entry("qwen3.8-solo"), model_entry("muse-glimmer"),
            model_entry("claude-like", "api.example.com", authenticated=True)]
        self.score: Dict[str, Optional[int]] = kw.pop("score", {})
        self.default_score: Optional[int] = kw.pop("default_score", 2)
        self.waiting_polls: int = kw.pop("waiting_polls", 0)
        self.running_polls: int = kw.pop("running_polls", 1)
        self.final: str = kw.pop("final", "completed")
        self.never_finish: bool = kw.pop("never_finish", False)
        self.assess_status: int = kw.pop("assess_status", 202)
        self.assess_detail: Any = kw.pop("assess_detail", None)
        self.delete_status: int = kw.pop("delete_status", 204)
        self.down: bool = kw.pop("down", False)
        self.delay_s: float = kw.pop("delay_s", 0.0)
        if kw:
            raise TypeError(f"unknown options {sorted(kw)}")
        self.requests: List[tuple] = []
        self.submitted: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self.cancelled: List[str] = []
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def transport(self):
        import httpx

        return httpx.MockTransport(self._handle)

    def client(self, **kw: Any):
        """A ClassifierClient on this fake: fast timings, the fake resolver."""
        from route_policy.analysis.classifier_client import ClassifierClient

        kw.setdefault("timeout_s", 5.0)
        kw.setdefault("poll_s", 0.01)
        kw.setdefault("resolver", fake_resolver)
        return ClassifierClient("http://classifier:8000", transport=self.transport(), **kw)

    @staticmethod
    def _json(status: int, body: Any = None):
        import httpx

        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    async def _handle(self, request):
        import httpx

        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        path = request.url.path
        with self._lock:
            self.requests.append((request.method, path))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.method == "GET" and path == "/models":
            return self._json(200, {"default": "muse-glimmer", "models": self.models})
        if request.method == "POST" and path == "/assess":
            body = json.loads(request.content)
            with self._lock:
                self.submitted.append(body)
            if self.assess_status != 202:
                return self._json(self.assess_status, {"detail": self.assess_detail or "refused"})
            with self._lock:
                job_id = f"job{len(self.submitted):04d}"
                self.jobs[job_id] = {"body": body, "polls": 0}
            return self._json(202, {"job_id": job_id, "phase": "pending"})
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "jobs":
            job_id = parts[1]
            with self._lock:
                job = self.jobs.get(job_id)
            if request.method == "POST" and parts[2:] == ["cancel"]:
                with self._lock:
                    self.cancelled.append(job_id)
                return self._json(404, {"detail": "Not Found"})  # the classifier mounts no cancel route
            if request.method == "DELETE" and len(parts) == 2:
                with self._lock:
                    self.deleted.append(job_id)
                    existed = self.jobs.pop(job_id, None) is not None
                return self._json(self.delete_status if existed else 404,
                                  None if existed else {"detail": f"no job {job_id!r}"})
            if request.method == "GET" and len(parts) == 2:
                if job is None:
                    return self._json(404, {"detail": f"no job {job_id!r}"})
                return self._json(200, self._poll(job_id, job))
        return self._json(404, {"detail": "Not Found"})

    def _poll(self, job_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            job["polls"] += 1
            polls = job["polls"]
        out = {"job_id": job_id, "created_at": "2026-10-19T00:00:00+00:00",
               "updated_at": "2026-10-19T00:00:01+00:00", "elapsed_seconds": 1.0, "metadata": {},
               "result": None, "error": None}
        if polls <= self.waiting_polls:
            return {**out, "phase": "pending"}
        if self.never_finish or polls <= self.waiting_polls + self.running_polls:
            return {**out, "phase": "processing"}
        if self.final == "failed":
            return {**out, "phase": "failed", "error": "the payload could not be read"}
        return {**out, "phase": "completed", "result": self.result(job["body"])}

    def result(self, body: Dict[str, Any]) -> Dict[str, Any]:
        per = {}
        for criterion in body["criteria"]:
            name = criterion["name"]
            score = self.score.get(name, self.default_score)
            if score is None:
                per[name] = {"status": "error", "type": "llm", "score": None, "verdict": None,
                             "confidence": None, "reason": None, "error": "model HTTP 500"}
            else:
                per[name] = {"status": "ok", "type": "llm", "score": score,
                             "verdict": "PASS" if score >= 7 else ("MARGINAL" if score >= 4 else "FAIL"),
                             "confidence": 90, "reason": f"scored {score}: " + "x" * 300, "error": None}
        complete = all(v["status"] == "ok" for v in per.values())
        n = len(per)
        return {"schema_version": 3,
                "assessment": {"overall_score": None, "complete": complete, "per_criterion_scores": per},
                "usage": {"calls": n, "errors": 0, "seconds": 1.5 * n, "prompt_tokens": 700 * n,
                          "cached_tokens": None, "completion_tokens": 40 * n, "reasoning_tokens": None,
                          "total_tokens": 740 * n, "models": [body.get("model")]}}


def strip_shadows(bindings):
    """The bindings without their shadow slots (tests about the primary alone)."""
    import dataclasses

    return [dataclasses.replace(b, shadows=()) for b in bindings]


def wait_for(predicate: Callable[[], Any], timeout_s: float = 10.0, interval_s: float = 0.02) -> Any:
    """Poll ``predicate`` from a test thread until it is truthy; its value."""
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"not true within {timeout_s}s")
        time.sleep(interval_s)
