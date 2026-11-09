"""Schemas built from the code: Pydantic models, declared result shapes,
FastMCP tools.

Every generated schema starts with ``"$comment": "generated from <dotted
path> — do not edit"``. A service's ``test_api_schemas.py`` builds its
generated schemas with these functions, compares them with its
``*.schemas.json`` and, with ``API_SCHEMAS_UPDATE=1``, writes them back
(:func:`apply_generated`). Nothing here imports a service: the caller hands
over the model, the spec or the tools.

* :func:`from_pydantic` -- ``model_json_schema(mode=...)`` (``validation``
  for a request, ``serialization`` for a response), with
  ``additionalProperties`` stated on every object: an object Pydantic left
  open-ended (a model with ``extra="ignore"``, the default) accepts unknown
  keys on input (``true``) and never emits one on output (``false``); what
  Pydantic stated itself (``extra="forbid"`` / ``"allow"``, dicts) is kept.
* :func:`from_output_spec` -- route-policy's ``OutputSpec``: one evaluator
  type's vote ``detail``. :func:`from_result_spec` -- the classifier's
  ``ResultSpec`` (a criterion type's ``detail``). Both read ``fields``:
  ``kind`` -> JSON type, ``nullable`` -> ``"null"`` allowed, ``when`` None ->
  required, ``description`` kept. Closed objects.
* :func:`from_detector_spec` -- the classifier's ``DetectorSpec``: a
  detector's ``measurements`` object.
* :func:`by_discriminator` -- one schema per value of a field (each vote's
  ``detail`` by its ``type``), as ``if`` / ``then`` pairs a validator
  enforces.
* :func:`from_mcp_tools` / :func:`from_mcp` -- each FastMCP tool's
  ``inputSchema``.

Stdlib only.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .schemas import is_object_schema, transform

GENERATED_PREFIX = "generated from "

# A FieldSpec's kind -> its JSON Schema type.
KIND_TYPES = {
    "string": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


def generated_comment(source: str) -> str:
    return f"{GENERATED_PREFIX}{source} — do not edit"


def is_generated(schema: Any) -> bool:
    return isinstance(schema, dict) and str(schema.get("$comment", "")).startswith(GENERATED_PREFIX)


def dotted_name(obj: Any) -> str:
    """``module.QualName`` of a class or function."""
    return f"{obj.__module__}.{obj.__qualname__}"


def state_additional_properties(schema: Any, default: bool) -> Any:
    """A copy of ``schema`` in which every object schema without
    ``additionalProperties`` (or ``unevaluatedProperties``) gets
    ``default``."""

    def fill(node: Dict[str, Any]) -> Dict[str, Any]:
        if is_object_schema(node) and "additionalProperties" not in node and "unevaluatedProperties" not in node:
            node["additionalProperties"] = default
        return node

    return transform(schema, fill)


def from_pydantic(model: Any, *, mode: str = "validation", source: Optional[str] = None) -> Dict[str, Any]:
    """A Pydantic v2 model's JSON Schema (module docstring)."""
    if mode not in ("validation", "serialization"):
        raise ValueError("mode is 'validation' (a request) or 'serialization' (a response)")
    schema = state_additional_properties(model.model_json_schema(mode=mode), mode == "validation")
    return {"$comment": generated_comment(source or dotted_name(model)), **schema}


def field_schema(spec: Any) -> Dict[str, Any]:
    """One declared field (``kind``, ``description``, optional ``nullable``
    and ``when``) as a schema."""
    kind = getattr(spec, "kind", None)
    if kind not in KIND_TYPES:
        raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(KIND_TYPES)}")
    json_type: Any = KIND_TYPES[kind]
    if getattr(spec, "nullable", False):
        json_type = [json_type, "null"]
    out: Dict[str, Any] = {"type": json_type}
    description = str(getattr(spec, "description", "") or "")
    when = getattr(spec, "when", None)
    if when:
        description = f"{description} (present only when {when})" if description else f"present only when {when}"
    if description:
        out["description"] = description
    if kind == "object":
        out["additionalProperties"] = True       # its keys are not declared
    return out


def fields_object(fields: Mapping[str, Any], *, description: Optional[str] = None) -> Dict[str, Any]:
    """A closed object of declared fields: every key typed, the ones with no
    ``when`` required."""
    out: Dict[str, Any] = {"type": "object"}
    if description:
        out["description"] = description
    out["additionalProperties"] = False
    out["required"] = [key for key, spec in fields.items() if getattr(spec, "when", None) is None]
    out["properties"] = {key: field_schema(spec) for key, spec in fields.items()}
    return out


def from_output_spec(spec: Any, *, source: str) -> Dict[str, Any]:
    """route-policy's ``OutputSpec``: the ``detail`` of one evaluator type's
    vote. ``source`` names where the declaration lives (the evaluator
    class's ``output``)."""
    return {"$comment": generated_comment(source), **fields_object(spec.fields, description=spec.summary)}


def from_result_spec(spec: Any, *, source: str) -> Dict[str, Any]:
    """The classifier's ``ResultSpec``: a criterion type's ``detail``."""
    return {"$comment": generated_comment(source),
            **fields_object(spec.fields, description=f"`detail` of a `{spec.type}` criterion")}


def from_detector_spec(spec: Any, *, source: str) -> Dict[str, Any]:
    """The classifier's ``DetectorSpec``: the detector's ``measurements``
    object -- each declared key a number, described with its unit. Which
    keys are always present is not declared, so none is required."""
    properties = {key: {"type": "number", "description": f"{m.description} (unit: {m.unit})"}
                  for key, m in spec.measurements.items()}
    return {"$comment": generated_comment(source), "type": "object",
            "description": f"`measurements` of the detector ({spec.technique})",
            "additionalProperties": False, "properties": properties}


def by_discriminator(discriminator: str, target: str, cases: Mapping[str, str], *, source: str,
                     description: Optional[str] = None) -> Dict[str, Any]:
    """``discriminator`` must be one of ``cases``' keys, and ``target`` must
    match the schema ``cases[value]`` references (a ``$ref`` string) -- as
    ``if`` / ``then`` pairs, so a validator checks each instance against its
    own case. Applies beside the object schema that declares both
    properties (put it in that schema's ``allOf``)."""
    out: Dict[str, Any] = {"$comment": generated_comment(source)}
    if description:
        out["description"] = description
    out["properties"] = {discriminator: {"enum": list(cases)}}
    out["allOf"] = [
        {"if": {"properties": {discriminator: {"const": value}}, "required": [discriminator]},
         "then": {"properties": {target: {"$ref": ref}}}}
        for value, ref in cases.items()
    ]
    return out


def from_mcp_tools(tools: Iterable[Any], *, source: str) -> Dict[str, Dict[str, Any]]:
    """Tool name -> its ``inputSchema`` (objects as listed by
    ``mcp.list_tools()``: ``.name``, ``.inputSchema``), with
    ``additionalProperties`` stated (``true`` where the tool left it open)."""
    out: Dict[str, Dict[str, Any]] = {}
    for tool in tools:
        schema = state_additional_properties(copy.deepcopy(tool.inputSchema), True)
        out[tool.name] = {"$comment": generated_comment(f"{source}:{tool.name}"), **schema}
    return out


async def from_mcp(mcp: Any, *, source: str) -> Dict[str, Dict[str, Any]]:
    """:func:`from_mcp_tools` over ``await mcp.list_tools()`` (FastMCP)."""
    return from_mcp_tools(await mcp.list_tools(), source=source)


# -- writing them back --------------------------------------------------------------------


Path = Tuple[str, ...]


def _get(document: Any, path: Path) -> Any:
    node = document
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def generated_paths(document: Mapping[str, Any]) -> List[Path]:
    """Where a schemas file holds a generated schema: a ``$defs`` entry, a
    request / response schema, an MCP entry's ``arguments`` / ``params`` /
    ``result``."""
    found: List[Path] = []
    for name, sub in (document.get("$defs") or {}).items():
        if is_generated(sub):
            found.append(("$defs", name))
    for key, entry in (document.get("endpoints") or {}).items():
        if not isinstance(entry, dict):
            continue
        request = entry.get("request")
        if isinstance(request, dict) and is_generated(request.get("schema")):
            found.append(("endpoints", key, "request", "schema"))
        for status, spec in (entry.get("responses") or {}).items():
            if isinstance(spec, dict) and is_generated(spec.get("schema")):
                found.append(("endpoints", key, "responses", status, "schema"))
        for part in ("arguments", "params", "result"):
            if is_generated(entry.get(part)):
                found.append(("endpoints", key, part))
    return found


def apply_generated(document: Dict[str, Any], generated: Mapping[Path, Any], *, prune: bool = True) -> List[str]:
    """Write each generated schema at its path in ``document`` (in place;
    the path's parent objects are created when missing). With ``prune``, a
    generated schema at a path ``generated`` no longer lists is removed --
    a model or a type that went away. Returns the paths that changed
    (``" / "``-joined), empty when the file is current."""
    changed: List[str] = []
    for path, schema in generated.items():
        if not path:
            raise ValueError("an empty path")
        if _get(document, path) == schema:
            continue
        node = document
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = copy.deepcopy(schema)
        changed.append(" / ".join(path))
    if prune:
        for path in generated_paths(document):
            if path not in generated:
                parent = _get(document, path[:-1])
                if isinstance(parent, dict):
                    parent.pop(path[-1], None)
                    changed.append(" / ".join(path) + " (removed)")
    return changed
