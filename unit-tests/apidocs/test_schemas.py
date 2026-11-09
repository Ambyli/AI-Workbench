"""Every Postman collection against its endpoint schemas (root ``CLAUDE.md``
§ Postman collections).

For each ``ai/<stack>/<stem>.postman_collection.json``:

  1. **coverage, both ways** -- every endpoint the collection calls (method +
     path; each MCP tool / JSON-RPC method) has an entry in
     ``<stem>.schemas.json`` and every entry names one; an entry has a request
     schema exactly when the endpoint's items send a body (``arguments`` for
     a ``tools/call``, ``params`` when a JSON-RPC item sends them), at least
     one 2xx response, and its ``invalid_examples`` name its own items;
  2. **valid schemas** -- every schema passes the draft 2020-12 meta-schema
     and its ``$ref``s resolve;
  3. **closed objects** in our services -- every object schema states
     ``additionalProperties`` (the upstream servers' files, open subsets,
     are exempt);
  4. **every request body validates** against its endpoint's request schema
     (``{{variables}}`` resolved from the collection's ``variable``
     defaults); an item listed in ``invalid_examples`` must FAIL instead;
  5. **the rendered copies are current** -- ``scripts/api_schemas.py render
     --check``: the ``#### Schemas`` sections in the collection and the
     endpoint doc's appendix;
  6. **the rollout ratchet** -- a collection in ``PENDING`` skips 1 and 4,
     and the test fails once a pending collection is complete, so the set
     only shrinks (it goes when it is empty).

Plus: every collection is in the canonical JSON form (``normalise``) and its
description names the generic error bodies. Imports no service, needs no
Postgres:

    UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/apidocs -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from collections import OrderedDict
from typing import Dict, List

import pytest

from common.apidocs import postman, render, schemas
from common.apidocs.schemas import SchemaFileError

ROOT = pathlib.Path(__file__).resolve().parents[2]
COLLECTIONS = sorted(ROOT.glob(f"ai/*/*{schemas.COLLECTION_SUFFIX}"))
SCHEMA_FILES = sorted(ROOT.glob(f"ai/*/*{schemas.SUFFIX}"))

# Collections whose schemas file is not written yet (phases 4-5 of the
# rollout). Checks 1 and 4 skip them; test_pending_only_shrinks fails once
# one is complete. Delete the set (and that test) when it is empty.
PENDING = {
    "classifier",
    "detector",
    "interceptor",
    "kokoro",
    "litellm",
    "llama",
    "madlad",
    "open-terminal",
    "roofix",
    "sandbox-runner",
    "searxng",
    "signals",
    "supabase",
    "vllm",
}

# Upstream servers: their schemas are an open subset of the upstream API
# (decision 5 of the plan), so check 3 does not apply.
UPSTREAM = {"vllm", "llama", "litellm", "searxng", "supabase", "open-terminal"}

ERROR_NOTE = "**Error bodies.**"


def _stem(path: pathlib.Path) -> str:
    for suffix in (schemas.COLLECTION_SUFFIX, schemas.SUFFIX):
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    raise ValueError(path)


def _ids(paths: List[pathlib.Path]) -> List[str]:
    return [_stem(p) for p in paths]


def _by_endpoint(collection) -> "OrderedDict[str, List[postman.Request]]":
    out: "OrderedDict[str, List[postman.Request]]" = OrderedDict()
    for req in postman.requests(collection):
        out.setdefault(postman.endpoint_key(req), []).append(req)
    return out


def _sends_body(req: postman.Request, values: Dict[str, str]) -> bool:
    try:
        return postman.body(req, values) is not None
    except (KeyError, ValueError):      # a body that does not resolve or parse is still a body (check 4 says why)
        return True


def coverage_problems(collection, schema_file: schemas.SchemaFile) -> List[str]:
    """Check 1."""
    problems: List[str] = []
    values = postman.variables(collection)
    endpoints = _by_endpoint(collection)
    for key in endpoints:
        if key not in schema_file.endpoints:
            problems.append(f"{key}: no entry in {schema_file.path.name}")
    for key in schema_file.endpoints:
        if key not in endpoints:
            problems.append(f"{key}: an entry for an endpoint the collection does not call")
    for key, reqs in endpoints.items():
        endpoint = schema_file.endpoints.get(key)
        if endpoint is None:
            continue
        names = {r.name for r in reqs} | {r.label for r in reqs}
        for name in endpoint.invalid_examples:
            if name not in names:
                problems.append(f"{key}: invalid_examples names {name!r}, not one of its items")
        if endpoint.is_mcp:
            messages = [postman.json_rpc(r) or {} for r in reqs]
            tool_call = any(m.get("method") == "tools/call" for m in messages)
            if tool_call != ("arguments" in endpoint.entry):
                problems.append(f"{key}: `arguments` goes with a tools/call entry, and only there")
            sends_params = (not tool_call) and any("params" in m for m in messages)
            if sends_params != ("params" in endpoint.entry):
                problems.append(f"{key}: `params` is {'missing' if sends_params else 'there, but no item sends params'}")
            continue
        sends = any(_sends_body(r, values) for r in reqs)
        if sends != bool(endpoint.requests()):
            problems.append(f"{key}: {'its items send a body but the entry has no request' if sends else 'a request schema, but no item sends a body'}")
        if not any(status.startswith("2") for status in endpoint.responses()):
            problems.append(f"{key}: no 2xx response")
    return problems


def body_problems(collection, schema_file: schemas.SchemaFile) -> List[str]:
    """Check 4."""
    problems: List[str] = []
    values = postman.variables(collection)
    for req in postman.requests(collection):
        key = postman.endpoint_key(req)
        endpoint = schema_file.endpoints.get(key)
        if endpoint is None:
            continue
        where = f"{req.label} ({key})"
        try:
            if endpoint.is_mcp:
                part = "arguments" if "arguments" in endpoint.entry else "params"
                if part not in endpoint.entry:
                    continue
                instance, schema = postman.mcp_instance(req, values), endpoint.entry[part]
            else:
                parsed = postman.body(req, values)
                if parsed is None:
                    continue
                spec = endpoint.request_for(parsed.content_type)
                if spec is None:
                    problems.append(f"{where}: sends {parsed.content_type}, which the entry has no request for")
                    continue
                if spec.schema is None:
                    continue
                instance, schema = parsed.value, spec.schema
        except KeyError as exc:
            problems.append(f"{where}: unknown variable {exc.args[0]!r}")
            continue
        except ValueError as exc:
            problems.append(f"{where}: the body is not JSON ({type(exc).__name__})")
            continue
        errors = schemas.validate(endpoint.standalone(schema), instance)
        expected_invalid = req.name in endpoint.invalid_examples or req.label in endpoint.invalid_examples
        if expected_invalid and not errors:
            problems.append(f"{where}: listed in invalid_examples, but its body validates")
        elif not expected_invalid and errors:
            problems.extend(f"{where}: {e}" for e in errors)
    return problems


# -- every collection ---------------------------------------------------------------------


@pytest.mark.parametrize("path", COLLECTIONS, ids=_ids(COLLECTIONS))
def test_collection_is_in_canonical_form(path):
    text = postman.read_text(path)
    assert text == postman.dumps(json.loads(text), postman.newline_of(text)), \
        f"run: uv run --no-sync python scripts/api_schemas.py normalise {path.relative_to(ROOT).as_posix()}"


@pytest.mark.parametrize("path", COLLECTIONS, ids=_ids(COLLECTIONS))
def test_collection_names_the_generic_error_bodies(path):
    assert ERROR_NOTE in postman.get_description(postman.load(path)["info"])


@pytest.mark.parametrize("path", COLLECTIONS, ids=_ids(COLLECTIONS))
def test_every_endpoint_has_an_entry_and_every_entry_an_endpoint(path):
    if _stem(path) in PENDING:
        pytest.skip("pending: its schemas file is not written yet")
    schema_path = schemas.schemas_path_for(path)
    assert schema_path.exists(), f"{schema_path.relative_to(ROOT).as_posix()} is missing"
    assert coverage_problems(postman.load(path), schemas.load_schemas(schema_path)) == []


@pytest.mark.parametrize("path", COLLECTIONS, ids=_ids(COLLECTIONS))
def test_every_request_body_validates(path):
    if _stem(path) in PENDING:
        pytest.skip("pending: its schemas file is not written yet")
    assert body_problems(postman.load(path), schemas.load_schemas(schemas.schemas_path_for(path))) == []


# -- every schemas file -----------------------------------------------------------------------


def test_every_schemas_file_sits_beside_its_collection():
    for path in SCHEMA_FILES:
        sf = schemas.load_schemas(path)
        assert sf.collection_path.exists(), f"{path.name}: no {sf.collection_path.name} beside it"
        if sf.doc_path is not None:
            assert sf.doc_path.exists(), f"{path.name}: doc {sf.doc_path.name} does not exist"


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=_ids(SCHEMA_FILES))
def test_schemas_are_valid_draft_2020_12(path):
    problems = []
    for label, schema in schemas.load_schemas(path).schemas():
        problems.extend(f"{label}: {p}" for p in schemas.check_schema(schema))
    assert problems == []


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=_ids(SCHEMA_FILES))
def test_our_objects_state_additional_properties(path):
    if _stem(path) in UPSTREAM:
        pytest.skip("an upstream server: an open subset")
    problems = []
    for label, schema in schemas.load_schemas(path).schemas():
        problems.extend(f"{label}: {where}" for where in schemas.open_objects(schema))
    assert problems == [], "every object schema states additionalProperties (false: closed)"


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=_ids(SCHEMA_FILES))
def test_rendered_copies_are_current(path):
    stale = [p.relative_to(ROOT).as_posix() for p in render.render_service(path, check=True)]
    assert stale == [], f"run: uv run --no-sync python scripts/api_schemas.py render {_stem(path)}"


def test_render_check_through_the_script():
    spec = importlib.util.spec_from_file_location("api_schemas", ROOT / "scripts" / "api_schemas.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["render", "--check"]) == 0


# -- the rollout ratchet ------------------------------------------------------------------------


def test_pending_only_shrinks():
    stems = {_stem(p) for p in COLLECTIONS}
    assert PENDING <= stems, f"PENDING names no collection: {sorted(PENDING - stems)}"
    complete = []
    for stem in sorted(PENDING):
        (path,) = [p for p in COLLECTIONS if _stem(p) == stem]
        schema_path = schemas.schemas_path_for(path)
        if not schema_path.exists():
            continue
        try:
            sf = schemas.load_schemas(schema_path)
        except SchemaFileError:
            continue
        collection = postman.load(path)
        if not coverage_problems(collection, sf) and not body_problems(collection, sf):
            complete.append(stem)
    assert complete == [], f"complete -- remove from PENDING: {complete}"
