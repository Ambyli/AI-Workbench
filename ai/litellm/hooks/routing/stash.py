"""The per-request record the pre-call hook leaves for the post-call hooks.

``zeo_route_policy`` in the proxy's own metadata dict, written by
PolicyClient.route on every request for model ``auto`` (and only there)::

    {"requested_model": "auto",
     "route":           "local-code",            # what data["model"] was rewritten to
     "source":          "engine" | "fallback:<why>",
     "decision_id":     "<uuid4>" | None,        # None: no /decide was sent
     "litellm_call_id": "<id>" | None,
     "observed":        False}                   # flipped once /observe went out

WHICH METADATA DICT. The proxy keeps its own per-request metadata (the
request ``headers``, the key's alias, ...) in ``data["metadata"]`` -- except
on the routes where ``metadata`` is an API parameter of the request itself
(``/v1/messages``, ``/v1/responses``, batches, files), where it uses
``data["litellm_metadata"]`` (litellm_pre_call_utils.py:133-139 and
_get_metadata_variable_name, 383-400 at v1.95.0). The stash goes in whichever
of the two carries the proxy's ``headers``, so it never becomes a provider
parameter on those routes, and so the failure path of ``/v1/messages`` --
which hands post_call_failure_hook the dict it read from the body, not the
proxy's working copy (anthropic_endpoints/endpoints.py:187-189) -- still sees
it: the proxy creates its metadata dict before ``function_setup`` copies the
top level (common_request_processing.py:1159 -> 1250), so both share it.

The footer reads the stash to know the request was ``auto`` (``data["model"]``
is the chain by then) and whether the route was a fallback; the /observe
paths read the decision id. A stash in the incoming request body is never
trusted: the pre-call hook drops it from every request before it writes its
own (the proxy keeps client metadata keys it does not know). Pure; never
raises.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..footer.render import AUTO_MODEL

STASH_KEY = "zeo_route_policy"
ENGINE = "engine"
FALLBACK_PREFIX = "fallback:"
# The proxy's metadata dict is one of these (see the module docstring).
METADATA_KEYS = ("litellm_metadata", "metadata")


def metadata_of(data: Any) -> Optional[Dict[str, Any]]:
    """The proxy's metadata dict for this request: the one of
    data["litellm_metadata"] / data["metadata"] that carries the request
    ``headers``, else data["metadata"] when it is a dict, else None."""
    if not isinstance(data, dict):
        return None
    for key in METADATA_KEYS:
        meta = data.get(key)
        if isinstance(meta, dict) and "headers" in meta:
            return meta
    meta = data.get("metadata")
    return meta if isinstance(meta, dict) else None


def stash_of(data: Any) -> Optional[Dict[str, Any]]:
    """The pre-call hook's stash, or None (the request was not for `auto`)."""
    if not isinstance(data, dict):
        return None
    for key in METADATA_KEYS:
        meta = data.get(key)
        stash = meta.get(STASH_KEY) if isinstance(meta, dict) else None
        if isinstance(stash, dict) and stash.get("requested_model") == AUTO_MODEL and isinstance(stash.get("route"), str):
            return stash
    return None


def drop_foreign_stash(data: Any) -> None:
    """Remove a stash that arrived in the request body (pre-call, every request)."""
    if not isinstance(data, dict):
        return
    for key in METADATA_KEYS:
        meta = data.get(key)
        if isinstance(meta, dict):
            meta.pop(STASH_KEY, None)


def requested_auto(data: Any) -> bool:
    """True when the CLIENT asked for `auto`: the stash says so, or the hook
    never rewrote the request (it is still `auto` -- the alias serves it)."""
    if stash_of(data) is not None:
        return True
    return isinstance(data, dict) and data.get("model") == AUTO_MODEL


def fell_back(data: Any) -> bool:
    """True when an `auto` request was NOT routed by the engine: a fallback
    source in the stash, or no stash at all on a request still for `auto`."""
    stash = stash_of(data)
    if stash is not None:
        return str(stash.get("source", "")).startswith(FALLBACK_PREFIX)
    return isinstance(data, dict) and data.get("model") == AUTO_MODEL
