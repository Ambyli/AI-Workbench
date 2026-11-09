"""common.apidocs — Postman collections and their endpoint JSON Schemas.

Built on small in-memory collections and schemas files in ``tmp_path``
(laid out like ``ai/<stack>/``); the repo's real collections are
``unit-tests/apidocs``'s.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from common.apidocs import generate, postman, render, schemas
from common.apidocs.schemas import SchemaFileError

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _item(name: str, method: str, path: str, *, body: Any = None, description: str = "",
          host: str = "{{base}}", query: str = "") -> Dict[str, Any]:
    segments = [s for s in path.strip("/").split("/") if s]
    req: Dict[str, Any] = {"method": method, "header": [],
                           "url": {"raw": f"{host}{path}{query}", "host": [host], "path": segments}}
    if body is not None:
        req["header"] = [{"key": "Content-Type", "value": "application/json"}]
        req["body"] = {"mode": "raw", "raw": body if isinstance(body, str) else json.dumps(body),
                       "options": {"raw": {"language": "json"}}}
    if description:
        req["description"] = description
    return {"name": name, "request": req, "response": []}


def _collection(*items: Dict[str, Any], variables: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {"info": {"name": "t", "description": "A test collection.",
                     "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "variable": [{"key": k, "value": v} for k, v in (variables or {"base": "http://x"}).items()],
            "item": list(items)}


def _rpc(method: str, params: Any = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        out["params"] = params
    return out


OBJ = {"type": "object", "additionalProperties": False}


def _service(root: pathlib.Path, stem: str, collection: Dict[str, Any], endpoints: Dict[str, Any], *,
             defs: Optional[Dict[str, Any]] = None, doc: Optional[str] = None, newline: str = "\n") -> pathlib.Path:
    """Write ai/<stem>/<stem>.postman_collection.json + .schemas.json; return the schemas path."""
    folder = root / "ai" / stem
    folder.mkdir(parents=True, exist_ok=True)
    postman.write_text(folder / f"{stem}.postman_collection.json", postman.dumps(collection, newline))
    data: Dict[str, Any] = {"$comment": "test", "service": stem}
    if doc:
        data["doc"] = doc
    if defs:
        data["$defs"] = defs
    data["endpoints"] = endpoints
    path = folder / f"{stem}.schemas.json"
    schemas.dump_schemas(data, path)
    return path


# ---------------------------------------------------------------------------
# postman
# ---------------------------------------------------------------------------


def test_dumps_is_canonical_and_keeps_the_newline_style(tmp_path):
    doc = {"b": "é — ✓", "a": [1, {"x": None}]}
    assert postman.dumps(doc) == json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    assert postman.dumps(doc, "\r\n").count("\r\n") == postman.dumps(doc).count("\n")
    path = tmp_path / "c.json"
    path.write_bytes(b'{"b":"\\u00e9 \\u2014 \\u2713","a":[1,{"x":null}]}\r\n')
    assert postman.dump(json.loads(path.read_text("utf-8")), path) is True
    written = path.read_bytes()
    assert b"\r\n" in written and written.replace(b"\r\n", b"").count(b"\n") == 0
    assert "é — ✓" in written.decode("utf-8")
    assert postman.dump(json.loads(written.decode("utf-8")), path) is False      # already canonical
    with pytest.raises(ValueError):
        postman.dumps(doc, "\r")


def test_requests_walk_folders_in_order_with_labels():
    collection = _collection(
        _item("health", "GET", "/health"),
        {"name": "MCP", "item": [_item("list", "POST", "/mcp/", body=_rpc("tools/list")),
                                 {"name": "deep", "item": [_item("call", "POST", "/mcp/", body=_rpc("ping"))]}]},
    )
    found = list(postman.requests(collection))
    assert [r.label for r in found] == ["health", "MCP / list", "MCP / deep / call"]
    assert [r.method for r in found] == ["GET", "POST", "POST"]


@pytest.mark.parametrize("item, key", [
    (_item("a", "get", "/v1/x/:id/items", query="?limit=1"), "GET /v1/x/:id/items"),
    (_item("b", "POST", "/v1/x/run", body={"a": 1}), "POST /v1/x/run"),
    (_item("c", "POST", "/v1/x/mcp/", body=_rpc("tools/call", {"name": "create", "arguments": {}})), "MCP create"),
    (_item("d", "POST", "/v1/x/mcp/", body=_rpc("initialize", {"protocolVersion": "2025-06-18"})),
     "MCP initialize"),
    (_item("e", "POST", "/v1/x/mcp/", body=_rpc("tools/list")), "MCP tools/list"),
    (_item("f", "POST", "/mcp/", body='{"jsonrpc": "2.0", "id": {{n}}, "method": "tools/call", '
                                      '"params": {"name": "{{tool}}x"}}'), "MCP {{tool}}x"),
    ({"name": "g", "request": "http://host:8000/health?verbose=1"}, "GET /health"),
    ({"name": "h", "request": {"method": "DELETE", "url": "{{base}}/jobs/:id"}}, "DELETE /jobs/:id"),
])
def test_endpoint_key(item, key):
    assert postman.endpoint_key(item) == key


def test_the_host_is_not_part_of_the_key():
    a = _item("a", "POST", "/v1/audio/speech", body={"x": 1}, host="{{kokoro}}")
    b = _item("b", "POST", "/v1/audio/speech", body={"x": 1}, host="{{litellm}}")
    assert postman.endpoint_key(a) == postman.endpoint_key(b) == "POST /v1/audio/speech"


def test_resolve():
    values = {"virtual master key": "sk-1", "id": "42"}
    assert postman.resolve("{{virtual master key}}/{{id}}", values) == "sk-1/42"
    assert postman.resolve("{{$guid}}", {}) == postman.DYNAMIC_VARIABLES["$guid"]
    with pytest.raises(KeyError, match="nope"):
        postman.resolve("{{nope}}", values)
    assert postman.resolve("{{nope}}", values, strict=False) == "{{nope}}"
    # JSX inside a body's source code is not a variable, strict or not.
    jsx = "style={{padding: '2rem', fontFamily: 'system-ui'}}"
    assert postman.resolve(jsx, values) == jsx


def test_variables_skip_disabled_and_stringify():
    collection = _collection()
    collection["variable"] = [{"key": "a", "value": "x"}, {"key": "b", "value": 3},
                              {"key": "c", "value": "y", "disabled": True}]
    assert postman.variables(collection) == {"a": "x", "b": "3"}


def test_body_raw_json_text_and_forms():
    raw = _item("a", "POST", "/x", body='{"id": "{{id}}", "n": {{n}}}')
    parsed = postman.body(postman.Request(raw), {"id": "abc", "n": "3"})
    assert (parsed.content_type, parsed.value) == ("application/json", {"id": "abc", "n": 3})
    assert postman.body(postman.Request(_item("b", "GET", "/x"))) is None
    text = {"name": "t", "request": {"method": "POST", "url": "{{base}}/t",
                                     "body": {"mode": "raw", "raw": "hello {{who}}"}}}
    assert postman.body(text, {"who": "you"}) == postman.Body("text/plain", "hello you")
    form = {"name": "f", "request": {"method": "POST", "url": "{{base}}/f", "body": {"mode": "formdata", "formdata": [
        {"key": "file", "type": "file", "src": "docs/a.pdf"},
        {"key": "file", "type": "file", "src": ["b.png", "c.png"]},
        {"key": "model", "type": "text", "value": "{{m}}"},
        {"key": "off", "type": "text", "value": "x", "disabled": True},
    ]}}}
    parsed = postman.body(form, {"m": "qwen"})
    assert parsed.content_type == "multipart/form-data"
    assert parsed.value == {"file": ["docs/a.pdf", "b.png c.png"], "model": "qwen"}
    encoded = {"name": "u", "request": {"method": "POST", "url": "{{base}}/u",
                                        "body": {"mode": "urlencoded", "urlencoded": [{"key": "q", "value": "1"}]}}}
    assert postman.body(encoded) == postman.Body("application/x-www-form-urlencoded", {"q": "1"})
    with pytest.raises(ValueError):
        postman.body(_item("bad", "POST", "/x", body='{"a": '))


def test_mcp_instance():
    call = _item("c", "POST", "/mcp/", body=_rpc("tools/call", {"name": "run", "arguments": {"code": "{{c}}"}}))
    assert postman.mcp_instance(call, {"c": "print(1)"}) == {"code": "print(1)"}
    bare = _item("c", "POST", "/mcp/", body=_rpc("tools/call", {"name": "list_jobs"}))
    assert postman.mcp_instance(bare) == {}
    assert postman.mcp_instance(_item("l", "POST", "/mcp/", body=_rpc("tools/list"))) is None
    init = _item("i", "POST", "/mcp/", body=_rpc("initialize", {"protocolVersion": "1"}))
    assert postman.mcp_instance(init) == {"protocolVersion": "1"}


def test_descriptions_in_both_forms():
    plain: Dict[str, Any] = {}
    postman.set_description(plain, "a")
    assert plain == {"description": "a"} and postman.get_description(plain) == "a"
    rich = {"description": {"content": "x", "type": "text/markdown"}}
    postman.set_description(rich, "b")
    assert rich["description"] == {"content": "b", "type": "text/markdown"}


# ---------------------------------------------------------------------------
# schemas: structure, same_as, standalone
# ---------------------------------------------------------------------------


def _endpoints_ok() -> Dict[str, Any]:
    return {
        "GET /health": {"responses": {"200": {"content_type": "application/json",
                                              "schema": {"$ref": "#/$defs/status"}}}},
        "POST /run": {"request": {"content_type": "application/json",
                                  "schema": {**OBJ, "required": ["code"], "properties": {"code": {"type": "string"}}}},
                      "responses": {"200": {"content_type": "application/json", "schema": OBJ},
                                    "204": {"description": "nothing to run"},
                                    "503": {"content_type": "text/plain"}},
                      "invalid_examples": ["run (bad)"]},
        "MCP run": {"arguments": {**OBJ, "properties": {}}, "result": OBJ},
    }


DEFS = {"status": {**OBJ, "required": ["status"], "properties": {"status": {"const": "ok"}}}}


def test_load_schemas_reads_entries_and_bodies(tmp_path):
    path = _service(tmp_path, "svc", _collection(), _endpoints_ok(), defs=DEFS)
    sf = schemas.load_schemas(path)
    assert sf.service == "svc" and list(sf.endpoints) == ["GET /health", "POST /run", "MCP run"]
    assert sf.collection_path.name == "svc.postman_collection.json"
    run = sf.endpoints["POST /run"]
    assert [b.content_type for b in run.requests()] == ["application/json"]
    assert run.request_for("application/json") is not None and run.request_for("text/plain") is None
    responses = run.responses()
    assert responses["204"].content_type is None and responses["503"].schema is None
    assert run.invalid_examples == ("run (bad)",)
    assert sf.endpoints["MCP run"].is_mcp
    labels = [label for label, _ in sf.schemas()]
    assert labels == ["$defs/status", "GET /health response 200", "POST /run request application/json",
                      "POST /run response 200", "MCP run arguments", "MCP run result"]


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(service="other"), "`service` must be 'svc'"),
    (lambda d: d.update(extra=1), "unknown top-level keys"),
    (lambda d: d["endpoints"]["GET /health"].update(oops=1), "unknown keys"),
    (lambda d: d["endpoints"]["GET /health"]["responses"].update({"2xx": {}}), "3-digit"),
    (lambda d: d["endpoints"]["GET /health"]["responses"].update({"201": {"content_type": "application/json"}}),
     "needs a `schema`"),
    (lambda d: d["endpoints"]["POST /run"]["request"].pop("content_type"), "needs `content_type`"),
    (lambda d: d["endpoints"]["MCP run"].pop("result"), "needs `result`"),
    (lambda d: d["endpoints"].update({"health": {"responses": {"200": {}}}}), "'<METHOD> /<path>'"),
    (lambda d: d["endpoints"]["GET /health"].update(responses={}), "non-empty"),
])
def test_load_schemas_refuses_a_bad_structure(tmp_path, mutate, message):
    path = _service(tmp_path, "svc", _collection(), _endpoints_ok(), defs=DEFS)
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    schemas.dump_schemas(data, path)
    with pytest.raises(SchemaFileError, match=message):
        schemas.load_schemas(path)


def test_same_as_within_and_across_files(tmp_path):
    _service(tmp_path, "base", _collection(), _endpoints_ok(), defs=DEFS)
    path = _service(tmp_path, "proxy", _collection(), {
        "GET /v1/base/health": {"same_as": "base: GET /health"},
        "GET /again": {"same_as": "GET /v1/base/health"},
    })
    sf = schemas.load_schemas(path)
    borrowed = sf.endpoints["GET /v1/base/health"]
    assert borrowed.same_as == "base: GET /health" and borrowed.source == "base: GET /health"
    assert "status" in borrowed.defs            # the target file's $defs travel with it
    again = sf.endpoints["GET /again"]
    assert again.source == "base: GET /health"
    (label, schema), = list(borrowed.schemas())
    assert schema["$defs"] == DEFS
    assert list(sf.schemas()) == []           # borrowed schemas are checked in their own file


def test_same_as_errors(tmp_path):
    path = _service(tmp_path, "loop", _collection(), {"GET /a": {"same_as": "GET /b"},
                                                     "GET /b": {"same_as": "GET /a"}})
    with pytest.raises(SchemaFileError, match="cycle"):
        schemas.load_schemas(path)
    path = _service(tmp_path, "lost", _collection(), {"GET /a": {"same_as": "nowhere: GET /a"}})
    with pytest.raises(SchemaFileError, match="0 files named nowhere"):
        schemas.load_schemas(path)
    path = _service(tmp_path, "gone", _collection(), {"GET /a": {"same_as": "GET /missing"}})
    with pytest.raises(SchemaFileError, match="no such entry"):
        schemas.load_schemas(path)


def test_standalone_flattens_what_it_reaches():
    defs = {"a": {"$ref": "#/$defs/b"}, "b": {"type": "string"}, "unused": {"type": "null"},
            "gen": {"type": "object", "additionalProperties": False,
                    "properties": {"x": {"$ref": "#/$defs/Inner"}},
                    "$defs": {"Inner": {"type": "integer"}}}}
    out = schemas.standalone({"properties": {"p": {"$ref": "#/$defs/a"}, "q": {"$ref": "#/$defs/gen"}}}, defs)
    assert set(out["$defs"]) == {"a", "b", "gen", "Inner"}
    assert "$defs" not in out["$defs"]["gen"]
    own = schemas.standalone({"$defs": {"x": {"type": "string"}}, "$ref": "#/$defs/x"}, {"x": {"type": "number"}})
    assert own["$defs"] == {"x": {"type": "string"}}                       # its own definition wins
    with pytest.raises(SchemaFileError, match="names no definition"):
        schemas.standalone({"$ref": "#/$defs/missing"}, defs)
    with pytest.raises(SchemaFileError, match="unsupported"):
        schemas.standalone({"$ref": "other.json#/x"}, defs)
    clash = {"g1": {"$defs": {"I": {"type": "string"}}}, "g2": {"$defs": {"I": {"type": "number"}}}}
    with pytest.raises(SchemaFileError, match="defined twice"):
        schemas.standalone({"allOf": [{"$ref": "#/$defs/g1"}, {"$ref": "#/$defs/g2"}]}, clash)


def test_walk_never_treats_a_property_name_as_a_keyword():
    schema = {"type": "object", "title": "T", "properties": {"title": {"type": "string", "title": "Title"},
                                                             "type": {"enum": ["object"]}},
              "anyOf": [{"type": "object", "properties": {}}], "enum": [{"type": "object"}]}
    paths = [p for p, _ in schemas.walk(schema)]
    assert ("properties", "title") in paths and ("anyOf", 0) in paths
    assert not any(p and p[0] == "enum" for p in paths)
    assert schemas.open_objects(schema) == ["/", "/anyOf/0"]


# ---------------------------------------------------------------------------
# schemas: check_schema / validate (jsonschema)
# ---------------------------------------------------------------------------


def test_check_schema():
    assert schemas.check_schema({"type": "object", "additionalProperties": False}) == []
    assert schemas.check_schema({"type": "frobnicate"})
    assert schemas.check_schema({"$ref": "#/$defs/x"}) == ["$ref '#/$defs/x' does not resolve inside the schema"]


SECRET = "my SSN is 523-45-6789"


def test_validate_reports_paths_and_rules_never_values():
    schema = {"type": "object", "additionalProperties": False, "required": ["id", "messages"],
              "properties": {"id": {"type": "string", "format": "uuid"},
                             "messages": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                     "properties": {"content": {"type": "integer"}}}},
                             "route": {"anyOf": [{"enum": ["a", "b"]}, {"type": "null"}]}}}
    errors = schemas.validate(schema, {"id": SECRET, "messages": [{"content": SECRET, SECRET[:6]: 1}],
                                       "route": SECRET})
    text = "\n".join(errors)
    assert "523-45-6789" not in text
    assert "$.id: is not a valid uuid" in errors
    assert "$.messages[0].content: is string, expected \"integer\"" in errors
    assert "$.messages[0]: has undeclared properties: 'my SSN'" in errors
    assert any(e.startswith("$.route: matches no anyOf branch (closest:") for e in errors)
    assert schemas.validate(schema, {"id": "6f0e8c1e-8f7e-4c55-9a39-0f3f7ab5d001", "messages": []}) == []
    assert schemas.validate(schema, {"messages": []}) == ["$: missing required 'id'"]


def test_json_path():
    assert schemas.json_path(["votes", 0, "detail", "a-b"]) == '$.votes[0].detail["a-b"]'


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_display_schema_inlines_refs_and_drops_title_keywords_only():
    schema = {"title": "Root", "type": "object", "properties": {
        "title": {"type": "string", "title": "Title"},
        "child": {"$ref": "#/$defs/Child", "description": "the child"},
        "tree": {"$ref": "#/$defs/Node"}},
        "$defs": {"Child": {"title": "Child", "type": "integer", "description": "an int"},
                  "Node": {"type": "object", "properties": {"kids": {"type": "array", "items": {"$ref": "#/$defs/Node"}}}}}}
    out = render.display_schema(schema)
    assert "title" not in out and out["properties"]["title"] == {"type": "string"}
    assert out["properties"]["child"] == {"type": "integer", "description": "the child"}
    # A recursive definition stays a reference, kept once under $defs.
    assert out["properties"]["tree"]["properties"]["kids"]["items"] == {"$ref": "#/$defs/Node"}
    assert set(out["$defs"]) == {"Node"}


def _collection_for_render() -> Dict[str, Any]:
    return _collection(
        _item("health", "GET", "/health", description="Liveness."),
        _item("run (ok)", "POST", "/run", body={"code": "1"}, description="Run code.\n\nMore prose."),
        _item("run (bad)", "POST", "/run", body={"nope": 1}),
        _item("unlisted", "GET", "/other", description="Untouched."),
        {"name": "MCP", "item": [_item("run tool", "POST", "/mcp/", body=_rpc("tools/call", {"name": "run",
                                                                                          "arguments": {}}))]},
    )


def test_apply_to_collection_is_idempotent_and_touches_only_the_section(tmp_path):
    path = _service(tmp_path, "svc", _collection_for_render(), _endpoints_ok(), defs=DEFS)
    sf = schemas.load_schemas(path)
    collection = _collection_for_render()
    assert render.apply_to_collection(collection, sf) == 4
    reqs = {r.label: postman.get_description(r.request) for r in postman.requests(collection)}
    assert reqs["health"].startswith("Liveness.\n\n#### Schemas\n\nResponse `200` (`application/json`):\n\n```json")
    assert '"const": "ok"' in reqs["health"]
    first = reqs["run (ok)"]
    assert first.startswith("Run code.\n\nMore prose.\n\n#### Schemas\n\nRequest body (`application/json`):")
    assert "Response `204`: no body — nothing to run." in first and "Response `503` (`text/plain`)." in first
    assert reqs["run (bad)"] == "#### Schemas\n\nSee *run (ok)*, the first request of this endpoint."
    assert reqs["unlisted"] == "Untouched."
    assert "Arguments (`params.arguments` of `tools/call`):" in reqs["MCP / run tool"]
    assert "Result (`result.structuredContent`):" in reqs["MCP / run tool"]
    again = json.loads(json.dumps(collection))
    assert render.apply_to_collection(again, sf) == 0 and again == collection
    # An entry that goes away takes its section with it, and nothing else.
    data = json.loads(path.read_text("utf-8"))
    del data["endpoints"]["GET /health"]
    schemas.dump_schemas(data, path)
    assert render.apply_to_collection(collection, schemas.load_schemas(path)) == 1
    assert postman.get_description(next(postman.requests(collection)).request) == "Liveness."


def test_apply_to_markdown_appends_then_replaces_between_markers(tmp_path):
    path = _service(tmp_path, "svc", _collection(), _endpoints_ok(), defs=DEFS)
    sf = schemas.load_schemas(path)
    doc = "# Svc\r\n\r\nProse.\r\n"
    once = render.apply_to_markdown(doc, sf)
    assert once.startswith("# Svc\r\n\r\nProse.\r\n\r\n<!-- api-schemas:begin -->\r\n## Request and response schemas")
    assert once.endswith("<!-- api-schemas:end -->\r\n")
    assert once.replace("\r\n", "").count("\n") == 0                       # CRLF kept throughout
    assert "### `POST /run`" in once and "### `MCP run`" in once
    assert render.apply_to_markdown(once, sf) == once
    edited = once.replace("Prose.", "Prose, edited.") + "\r\nAfter.\r\n"
    data = json.loads(path.read_text("utf-8"))
    del data["endpoints"]["MCP run"]
    schemas.dump_schemas(data, path)
    replaced = render.apply_to_markdown(edited, schemas.load_schemas(path))
    assert "Prose, edited." in replaced and replaced.endswith("<!-- api-schemas:end -->\r\n\r\nAfter.\r\n")
    assert "### `MCP run`" not in replaced
    with pytest.raises(ValueError):
        render.apply_to_markdown("<!-- api-schemas:end -->\n<!-- api-schemas:begin -->\n", sf)


def test_render_service_check_then_write(tmp_path):
    path = _service(tmp_path, "svc", _collection_for_render(), _endpoints_ok(), defs=DEFS, doc="SVC.md",
                    newline="\r\n")
    (path.parent / "SVC.md").write_bytes(b"# Svc\n")
    collection_path = path.parent / "svc.postman_collection.json"
    before = collection_path.read_bytes()
    stale = render.render_service(path, check=True)
    assert sorted(p.name for p in stale) == ["SVC.md", "svc.postman_collection.json"]
    assert collection_path.read_bytes() == before                         # check writes nothing
    assert len(render.render_service(path)) == 2
    assert render.render_service(path, check=True) == []
    written = collection_path.read_bytes()
    assert b"\r\n" in written and written.replace(b"\r\n", b"").count(b"\n") == 0
    assert "## Request and response schemas" in (path.parent / "SVC.md").read_text("utf-8")


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def test_from_pydantic_states_additional_properties_per_mode():
    from pydantic import BaseModel, ConfigDict

    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        n: int

    class Outer(BaseModel):
        name: str
        inner: Inner
        extra: Dict[str, Any] = {}

    request = generate.from_pydantic(Outer)
    assert request["$comment"] == f"generated from {Outer.__module__}.{Outer.__qualname__} — do not edit"
    assert request["additionalProperties"] is True                       # extra="ignore": accepted on input
    assert request["$defs"]["Inner"]["additionalProperties"] is False
    assert request["properties"]["extra"]["additionalProperties"] is True
    response = generate.from_pydantic(Outer, mode="serialization", source="pkg.Outer")
    assert response["$comment"] == "generated from pkg.Outer — do not edit"
    assert response["additionalProperties"] is False                     # never emitted on output
    assert schemas.open_objects(request) == [] and schemas.open_objects(response) == []
    assert schemas.check_schema(request) == []
    with pytest.raises(ValueError):
        generate.from_pydantic(Outer, mode="both")


@dataclass(frozen=True)
class _Field:
    description: str
    kind: str
    nullable: bool = False
    when: Optional[str] = None


@dataclass(frozen=True)
class _Spec:
    type: str
    summary: str
    fields: Dict[str, _Field] = field(default_factory=dict)


def test_from_output_spec():
    spec = _Spec("tool_calls", "Tool calls of the exchange.", {
        "called": _Field("Tool names called", "array"),
        "matched": _Field("The first match; null when none", "string", nullable=True),
        "extra": _Field("Context", "object", when="a tool was called"),
    })
    out = generate.from_output_spec(spec, source="pkg.ToolCalls.output")
    assert out["$comment"] == "generated from pkg.ToolCalls.output — do not edit"
    assert out["description"] == "Tool calls of the exchange." and out["additionalProperties"] is False
    assert out["required"] == ["called", "matched"]
    assert out["properties"]["matched"] == {"type": ["string", "null"], "description": "The first match; null when none"}
    assert out["properties"]["extra"] == {"type": "object", "additionalProperties": True,
                                          "description": "Context (present only when a tool was called)"}
    assert schemas.validate(out, {"called": [], "matched": None}) == []
    assert schemas.validate(out, {"called": [], "matched": 3, "x": 1}) == [
        "$.matched: is integer, expected [\"string\", \"null\"]", "$: has undeclared properties: 'x'"]
    with pytest.raises(ValueError, match="unknown kind"):
        generate.field_schema(_Field("x", "list"))


def test_from_result_and_detector_specs():
    result = SimpleNamespace(type="text", fields={"metric": _Field("Name", "string"), "value": _Field("N", "number")})
    out = generate.from_result_spec(result, source="analysis.text_eval")
    assert out["required"] == ["metric", "value"] and out["description"] == "`detail` of a `text` criterion"
    detector = SimpleNamespace(technique="Laplacian", measurements={
        "variance": SimpleNamespace(unit="variance", description="Laplacian variance")})
    out = generate.from_detector_spec(detector, source="cv.quality.blur")
    assert out["properties"]["variance"] == {"type": "number", "description": "Laplacian variance (unit: variance)"}
    assert "required" not in out and out["additionalProperties"] is False


def test_by_discriminator_checks_each_case():
    defs = {"a": {"type": "object", "additionalProperties": False, "required": ["x"],
                  "properties": {"x": {"type": "integer"}}},
            "b": {"type": "object", "additionalProperties": False, "properties": {}}}
    dispatch = generate.by_discriminator("type", "detail", {"a": "#/$defs/a", "b": "#/$defs/b"}, source="pkg.registry")
    vote = {"type": "object", "additionalProperties": False, "required": ["type", "detail"],
            "properties": {"type": {"type": "string"}, "detail": {"type": "object", "additionalProperties": True}},
            "allOf": [{"$ref": "#/$defs/dispatch"}]}
    schema = schemas.standalone(vote, {**defs, "dispatch": dispatch})
    assert schemas.check_schema(schema) == []
    assert schemas.validate(schema, {"type": "a", "detail": {"x": 1}}) == []
    assert schemas.validate(schema, {"type": "b", "detail": {}}) == []
    assert schemas.validate(schema, {"type": "b", "detail": {"x": 1}}) == ["$.detail: has undeclared properties: 'x'"]
    assert schemas.validate(schema, {"type": "a", "detail": {}}) == ["$.detail: missing required 'x'"]
    assert schemas.validate(schema, {"type": "c", "detail": {}}) == ['$.type: is not one of ["a", "b"]']


def test_from_mcp_tools():
    tools = [SimpleNamespace(name="run", inputSchema={"type": "object", "properties": {"code": {"type": "string"}},
                                                      "required": ["code"]})]
    out = generate.from_mcp_tools(tools, source="runner.sandbox_mcp.mcp")
    assert out["run"]["$comment"] == "generated from runner.sandbox_mcp.mcp:run — do not edit"
    assert out["run"]["additionalProperties"] is True and tools[0].inputSchema.get("additionalProperties") is None


def test_apply_generated_writes_reports_and_prunes():
    document: Dict[str, Any] = {"$defs": {"old": {"$comment": "generated from gone — do not edit"},
                                          "hand": {"type": "string"}},
                                "endpoints": {"POST /x": {"request": {"content_type": "application/json"},
                                                          "responses": {"200": {}}}}}
    request = {"$comment": "generated from pkg.Req — do not edit", "type": "object"}
    generated = {("endpoints", "POST /x", "request", "schema"): request,
                 ("$defs", "new"): {"$comment": "generated from pkg.New — do not edit"}}
    changed = generate.apply_generated(document, generated)
    assert changed == ["endpoints / POST /x / request / schema", "$defs / new", "$defs / old (removed)"]
    assert document["endpoints"]["POST /x"]["request"] == {"content_type": "application/json", "schema": request}
    assert set(document["$defs"]) == {"hand", "new"}
    assert generate.apply_generated(document, generated) == []
    assert generate.generated_paths(document) == [("$defs", "new"), ("endpoints", "POST /x", "request", "schema")]
