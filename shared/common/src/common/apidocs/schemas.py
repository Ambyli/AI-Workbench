"""Endpoint JSON Schemas: the ``*.schemas.json`` file beside a collection.

**The file** (``ai/<service>/<collection-stem>.schemas.json``)::

    {
      "$comment": "what this is and how to re-render it",
      "service": "<collection-stem>",
      "doc": "ROUTE_POLICY.md",            # the endpoint doc that gets the appendix (optional)
      "$defs": {...},                      # shared definitions, "$ref": "#/$defs/<name>"
      "endpoints": {
        "POST /v1/x/run": {
          "description": "...",                                     # optional
          "request": {"content_type": "application/json", "schema": {...}},
          "responses": {
            "200": {"content_type": "application/json", "schema": {...}},
            "204": {"description": "no body"},                      # no content_type: no body
            "200": {"content_type": "text/plain"}                   # no schema: not JSON
          },
          "invalid_examples": ["<item name or label>", ...]
        },
        "MCP <tool>": {"arguments": {...}, "result": {...}},       # tools/call
        "MCP initialize": {"params": {...}, "result": {...}},      # another JSON-RPC method
        "GET /v1/y/health": {"same_as": "<service>: <key>"}        # another file's (or this file's) entry
      }
    }

``request`` may also be a LIST of such objects, one per content type (an
endpoint that takes JSON or multipart). Keys are
:func:`common.apidocs.postman.endpoint_key`'s.

**Schemas** are JSON Schema draft 2020-12. A ``$ref`` is ``#/$defs/<name>``;
:func:`standalone` turns any schema of the file into a self-contained one
(the definitions it needs, transitively, flattened into its own ``$defs``),
which is what :func:`check_schema`, :func:`validate` and the renderer take.
A generated schema (``$comment: "generated from ..."``) may carry its own
``$defs``; they are flattened the same way.

:func:`validate` reports where and why an instance fails -- a JSON path and
the rule -- and never quotes the instance: a route-policy body holds
conversation text.

Stdlib only, except :func:`check_schema` and :func:`validate`, which import
``jsonschema`` (the ``apidocs`` extra) when called.
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from . import postman

# Keywords whose value is a schema, a map of schemas, or a list of schemas
# (draft 2020-12, plus the draft-7 spellings a generator may still emit).
# Everything else -- enum, const, default, examples, required -- is data.
SCHEMA_KEYWORDS = frozenset({
    "items", "additionalItems", "additionalProperties", "not", "if", "then", "else", "contains",
    "propertyNames", "unevaluatedItems", "unevaluatedProperties", "contentSchema",
})
SCHEMA_MAP_KEYWORDS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})

FILE_KEYS = frozenset({"$comment", "service", "doc", "$defs", "endpoints"})
HTTP_KEYS = frozenset({"$comment", "description", "request", "responses", "invalid_examples"})
MCP_KEYS = frozenset({"$comment", "description", "arguments", "params", "result", "invalid_examples"})
SAME_AS_KEYS = frozenset({"$comment", "same_as", "invalid_examples"})
BODY_KEYS = frozenset({"content_type", "schema", "description"})

SUFFIX = ".schemas.json"
COLLECTION_SUFFIX = ".postman_collection.json"
_DEF_REF = re.compile(r"^#/\$defs/([^/]+)$")
_STATUS = re.compile(r"^[1-5][0-9][0-9]$")


class SchemaFileError(ValueError):
    """A schemas file that cannot be used: bad structure, an unknown
    ``same_as`` target, an unresolvable ``$ref``."""


# -- walking schemas ------------------------------------------------------------------------


def walk(schema: Any, path: Tuple[Any, ...] = ()) -> Iterator[Tuple[Tuple[Any, ...], Any]]:
    """Every schema position under (and including) ``schema``, as
    ``(path, subschema)`` -- descending only through schema keywords, so a
    property NAMED ``title`` or ``type`` is never mistaken for a keyword."""
    yield path, schema
    if not isinstance(schema, dict):
        return
    for key, value in schema.items():
        if key in SCHEMA_KEYWORDS:
            if isinstance(value, list):                 # draft-7 tuple `items`
                for i, sub in enumerate(value):
                    yield from walk(sub, (*path, key, i))
            else:
                yield from walk(value, (*path, key))
        elif key in SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            for name, sub in value.items():
                yield from walk(sub, (*path, key, name))
        elif key in SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            for i, sub in enumerate(value):
                yield from walk(sub, (*path, key, i))


def transform(schema: Any, fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Any:
    """A copy of ``schema`` with ``fn`` applied to every schema object,
    children first (``fn`` gets a fresh dict it may change and return)."""
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in SCHEMA_KEYWORDS:
            out[key] = [transform(v, fn) for v in value] if isinstance(value, list) else transform(value, fn)
        elif key in SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            out[key] = {name: transform(sub, fn) for name, sub in value.items()}
        elif key in SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            out[key] = [transform(sub, fn) for sub in value]
        else:
            out[key] = copy.deepcopy(value)
    return fn(out)


def is_object_schema(schema: Any) -> bool:
    """A schema that says its instance is an object (``type`` is or
    includes ``object``)."""
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    return kind == "object" or (isinstance(kind, list) and "object" in kind)


def open_objects(schema: Any) -> List[str]:
    """Where an object schema leaves ``additionalProperties`` (or
    ``unevaluatedProperties``) unstated, as ``/``-joined paths."""
    return ["/" + "/".join(str(p) for p in path)
            for path, sub in walk(schema)
            if is_object_schema(sub) and "additionalProperties" not in sub and "unevaluatedProperties" not in sub]


def refs(schema: Any) -> List[str]:
    """Every ``$ref`` string under ``schema``, in walk order."""
    return [sub["$ref"] for _, sub in walk(schema) if isinstance(sub, dict) and isinstance(sub.get("$ref"), str)]


def def_name(ref: str) -> Optional[str]:
    """``<name>`` of a ``#/$defs/<name>`` reference; None for any other."""
    match = _DEF_REF.match(ref)
    return match.group(1).replace("~1", "/").replace("~0", "~") if match else None


def standalone(schema: Any, defs: Optional[Mapping[str, Any]] = None) -> Any:
    """``schema`` as a self-contained document: every ``#/$defs/<name>`` it
    reaches -- in its own ``$defs``, in ``defs`` (the file's), or in the
    ``$defs`` of a definition it pulls in -- flattened into one root
    ``$defs``. Raises SchemaFileError for a name defined twice differently
    or not at all, and for a ``$ref`` of any other form."""
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    root = copy.deepcopy(schema)
    pool: Dict[str, Any] = {}

    def offer(source: Mapping[str, Any]) -> None:
        for name, sub in (source or {}).items():
            if name in pool and pool[name] != sub:
                raise SchemaFileError(f"$defs/{name} is defined twice, differently")
            pool.setdefault(name, sub)

    offer(root.pop("$defs", {}) or {})
    own = dict(pool)
    wanted: Dict[str, Any] = {}
    queue = list(refs(root)) + [r for sub in own.values() for r in refs(sub)]
    file_defs = dict(defs or {})
    while queue:
        ref = queue.pop()
        name = def_name(ref)
        if name is None:
            raise SchemaFileError(f"unsupported $ref {ref!r} (only #/$defs/<name>)")
        if name in wanted:
            continue
        if name not in pool:
            if name not in file_defs:
                raise SchemaFileError(f"$ref {ref!r} names no definition")
            pool[name] = file_defs[name]
        sub = copy.deepcopy(pool[name])
        if isinstance(sub, dict) and "$defs" in sub:
            nested = sub.pop("$defs") or {}
            offer(nested)
            queue.extend(f"#/$defs/{n}" for n in nested)
        wanted[name] = sub
        queue.extend(refs(sub))
    for name, sub in own.items():
        wanted.setdefault(name, copy.deepcopy(sub))
    if wanted:
        root["$defs"] = dict(sorted(wanted.items()))
    return root


def inline_refs(schema: Any, lookup: Callable[[str], Any], *, defs_key: str = "$defs") -> Any:
    """``schema`` with every ``$ref`` replaced by what ``lookup(ref)``
    returns (itself inlined), for display. A ``$ref`` with sibling keywords
    is merged into the target (the siblings win on annotations) or, where
    they clash, kept as ``allOf``. A RECURSIVE definition stays a reference:
    it is kept under ``$defs`` (``defs_key``) as ``#/$defs/<name>``, its own
    body inlined once."""
    kept: Dict[str, Any] = {}
    names: Dict[str, str] = {}

    def name_of(ref: str) -> str:
        if ref not in names:
            base = ref.rsplit("/", 1)[-1] or "ref"
            name, n = base, 2
            while name in names.values():
                name, n = f"{base}_{n}", n + 1
            names[ref] = name
        return names[ref]

    def visit(node: Any, stack: Tuple[str, ...]) -> Any:
        def merge_ref(obj: Dict[str, Any]) -> Dict[str, Any]:
            ref = obj.get("$ref")
            if not isinstance(ref, str):
                return obj
            siblings = {k: v for k, v in obj.items() if k != "$ref"}
            if ref in stack:
                name = name_of(ref)
                kept.setdefault(name, ref)
                return {"$ref": f"#/{defs_key}/{name}", **siblings}
            target = visit(lookup(ref), (*stack, ref))
            if not isinstance(target, dict):
                return {"allOf": [target], **siblings} if siblings else target
            annotations = {"description", "title", "default", "$comment", "examples", "deprecated",
                           "readOnly", "writeOnly"}
            clash = [k for k in siblings if k in target and k not in annotations and siblings[k] != target[k]]
            if clash:
                return {"allOf": [target], **siblings}
            return {**target, **siblings}

        if not isinstance(node, dict):
            return copy.deepcopy(node)
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if key in SCHEMA_KEYWORDS:
                out[key] = [visit(v, stack) for v in value] if isinstance(value, list) else visit(value, stack)
            elif key in SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
                out[key] = {n: visit(s, stack) for n, s in value.items()}
            elif key in SCHEMA_LIST_KEYWORDS and isinstance(value, list):
                out[key] = [visit(s, stack) for s in value]
            else:
                out[key] = copy.deepcopy(value)
        return merge_ref(out)

    result = visit(schema, ())
    done: Dict[str, Any] = {}
    while set(kept) - set(done):
        for name in sorted(set(kept) - set(done)):
            ref = kept[name]
            done[name] = visit(lookup(ref), (ref,))
    if isinstance(result, dict) and done:
        result[defs_key] = dict(sorted(done.items()))
    return result


# -- the file -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Body:
    """One request body or response of an entry."""

    content_type: Optional[str]           # None: no body
    schema: Any = None                     # None: not described by a schema (non-JSON)
    description: Optional[str] = None


@dataclass(frozen=True)
class Endpoint:
    """One entry, with ``same_as`` followed: ``entry`` and ``defs`` belong
    to the file it is written in (``source``)."""

    key: str
    entry: Mapping[str, Any]
    defs: Mapping[str, Any]
    source: str                             # "<service>: <key>" where the entry is written
    same_as: Optional[str] = None           # the reference this entry borrowed through
    invalid_examples: Tuple[str, ...] = ()

    @property
    def is_mcp(self) -> bool:
        return self.key.startswith("MCP ")

    @property
    def description(self) -> Optional[str]:
        return self.entry.get("description")

    def requests(self) -> List[Body]:
        """The request bodies (HTTP), one per content type."""
        spec = self.entry.get("request")
        if spec is None:
            return []
        return [_body(s) for s in (spec if isinstance(spec, list) else [spec])]

    def request_for(self, content_type: str) -> Optional[Body]:
        for b in self.requests():
            if b.content_type == content_type:
                return b
        return None

    def responses(self) -> Dict[str, Body]:
        return {status: _body(spec) for status, spec in (self.entry.get("responses") or {}).items()}

    def standalone(self, schema: Any) -> Any:
        return standalone(schema, self.defs)

    def schemas(self) -> Iterator[Tuple[str, Any]]:
        """``(label, standalone schema)`` for every schema of the entry."""
        if self.is_mcp:
            for part in ("arguments", "params", "result"):
                if part in self.entry:
                    yield part, self.standalone(self.entry[part])
            return
        for b in self.requests():
            if b.schema is not None:
                yield f"request {b.content_type}", self.standalone(b.schema)
        for status, b in self.responses().items():
            if b.schema is not None:
                yield f"response {status}", self.standalone(b.schema)


def _body(spec: Mapping[str, Any]) -> Body:
    return Body(spec.get("content_type"), spec.get("schema"), spec.get("description"))


@dataclass(frozen=True)
class SchemaFile:
    path: pathlib.Path
    data: Mapping[str, Any]
    endpoints: Dict[str, Endpoint] = field(default_factory=dict)

    @property
    def service(self) -> str:
        return str(self.data.get("service", ""))

    @property
    def defs(self) -> Mapping[str, Any]:
        return self.data.get("$defs") or {}

    @property
    def collection_path(self) -> pathlib.Path:
        return self.path.with_name(self.path.name[: -len(SUFFIX)] + COLLECTION_SUFFIX)

    @property
    def doc_path(self) -> Optional[pathlib.Path]:
        doc = self.data.get("doc")
        return self.path.parent / doc if doc else None

    def schemas(self) -> Iterator[Tuple[str, Any]]:
        """``(label, standalone schema)`` for every schema the file holds:
        each ``$defs`` entry, then each entry's (borrowed ones excluded --
        their own file checks them)."""
        for name, sub in self.defs.items():
            yield f"$defs/{name}", standalone({"$ref": f"#/$defs/{name}"}, self.defs)
        for key, endpoint in self.endpoints.items():
            if endpoint.same_as is None:
                for label, schema in endpoint.schemas():
                    yield f"{key} {label}", schema


def schemas_path_for(collection_path: "postman.PathLike") -> pathlib.Path:
    """``x.schemas.json`` beside ``x.postman_collection.json``."""
    path = pathlib.Path(collection_path)
    if not path.name.endswith(COLLECTION_SUFFIX):
        raise ValueError(f"{path.name} is not a *{COLLECTION_SUFFIX}")
    return path.with_name(path.name[: -len(COLLECTION_SUFFIX)] + SUFFIX)


def _check_structure(path: pathlib.Path, data: Any) -> None:
    where = path.name
    if not isinstance(data, dict):
        raise SchemaFileError(f"{where}: not a JSON object")
    extra = set(data) - FILE_KEYS
    if extra:
        raise SchemaFileError(f"{where}: unknown top-level keys {sorted(extra)}")
    if data.get("service") != path.name[: -len(SUFFIX)]:
        raise SchemaFileError(f"{where}: `service` must be {path.name[: -len(SUFFIX)]!r} (the file's stem)")
    if not isinstance(data.get("endpoints"), dict):
        raise SchemaFileError(f"{where}: `endpoints` must be an object")
    if not isinstance(data.get("$defs", {}), dict):
        raise SchemaFileError(f"{where}: `$defs` must be an object")
    for key, entry in data["endpoints"].items():
        at = f"{where}: {key!r}"
        if not isinstance(entry, dict):
            raise SchemaFileError(f"{at}: an entry is an object")
        if "same_as" in entry:
            allowed = SAME_AS_KEYS
        elif key.startswith("MCP "):
            allowed = MCP_KEYS
            if "result" not in entry:
                raise SchemaFileError(f"{at}: an MCP entry needs `result`")
            if "arguments" in entry and "params" in entry:
                raise SchemaFileError(f"{at}: `arguments` (tools/call) or `params` (other methods), not both")
        else:
            allowed = HTTP_KEYS
            if not re.match(r"^[A-Z]+ /", key):
                raise SchemaFileError(f"{at}: a key is '<METHOD> /<path>' or 'MCP <name>'")
            responses = entry.get("responses")
            if not isinstance(responses, dict) or not responses:
                raise SchemaFileError(f"{at}: `responses` must be a non-empty object")
            bodies = list(responses.items())
            request = entry.get("request")
            if request is not None:
                bodies += [("request", r) for r in (request if isinstance(request, list) else [request])]
            for status, spec in bodies:
                if status != "request" and not _STATUS.match(status):
                    raise SchemaFileError(f"{at}: response status {status!r} is not a 3-digit code")
                if not isinstance(spec, dict) or set(spec) - BODY_KEYS:
                    raise SchemaFileError(f"{at} {status}: a body is {{content_type, schema, description}}")
                ctype = spec.get("content_type")
                if status == "request" and not ctype:
                    raise SchemaFileError(f"{at}: a request body needs `content_type`")
                if ctype and _is_json(ctype) and "schema" not in spec:
                    raise SchemaFileError(f"{at} {status}: a JSON body needs a `schema`")
                if not ctype and "schema" in spec:
                    raise SchemaFileError(f"{at} {status}: a `schema` needs a `content_type`")
        unknown = set(entry) - allowed
        if unknown:
            raise SchemaFileError(f"{at}: unknown keys {sorted(unknown)}")
        examples = entry.get("invalid_examples", [])
        if not isinstance(examples, list) or not all(isinstance(e, str) for e in examples):
            raise SchemaFileError(f"{at}: `invalid_examples` is a list of item names")


def _is_json(content_type: str) -> bool:
    ctype = content_type.split(";", 1)[0].strip().lower()
    return ctype == "application/json" or ctype.endswith("+json")


def _split_same_as(ref: str) -> Tuple[Optional[str], str]:
    if ": " in ref:
        service, key = ref.split(": ", 1)
        return service.strip(), key.strip()
    return None, ref.strip()


def load_schemas(path: "postman.PathLike", *, _loading: Optional[Dict[pathlib.Path, Any]] = None) -> SchemaFile:
    """Read and check a schemas file, following each ``same_as`` -- into
    this file, or into ``<service>.schemas.json`` in a sibling directory of
    this file's (``ai/*/``). Raises SchemaFileError."""
    path = pathlib.Path(path).resolve()
    loading = {} if _loading is None else _loading
    if path in loading:
        return loading[path]
    try:
        data = json.loads(postman.read_text(path))
    except FileNotFoundError:
        raise SchemaFileError(f"{path.name}: no such file") from None
    except ValueError as exc:
        raise SchemaFileError(f"{path.name}: not JSON ({exc})") from None
    _check_structure(path, data)
    sf = SchemaFile(path, data)
    loading[path] = sf
    service = sf.service
    for key, entry in data["endpoints"].items():
        examples = tuple(entry.get("invalid_examples", []))
        if "same_as" in entry:
            target = _follow(sf, key, entry["same_as"], loading, seen=(f"{service}: {key}",))
            sf.endpoints[key] = Endpoint(key, target.entry, target.defs, target.source, entry["same_as"], examples)
        else:
            sf.endpoints[key] = Endpoint(key, entry, sf.defs, f"{service}: {key}", None, examples)
    return sf


def _follow(sf: SchemaFile, key: str, ref: str, loading: Dict[pathlib.Path, Any],
            seen: Tuple[str, ...]) -> Endpoint:
    service, target_key = _split_same_as(ref)
    service = service or sf.service
    label = f"{service}: {target_key}"
    if label in seen:
        raise SchemaFileError(f"{sf.path.name}: same_as cycle {' -> '.join((*seen, label))}")
    if service == sf.service:
        target_file = sf
    else:
        candidates = sorted(sf.path.parent.parent.glob(f"*/{service}{SUFFIX}"))
        if len(candidates) != 1:
            raise SchemaFileError(f"{sf.path.name}: {key!r} same_as {ref!r}: "
                                  f"{len(candidates)} files named {service}{SUFFIX}")
        target_file = load_schemas(candidates[0], _loading=loading)
    entry = target_file.data["endpoints"].get(target_key)
    if entry is None:
        raise SchemaFileError(f"{sf.path.name}: {key!r} same_as {ref!r}: no such entry")
    if "same_as" in entry:
        return _follow(target_file, target_key, entry["same_as"], loading, (*seen, label))
    return Endpoint(target_key, entry, target_file.defs, label)


def dump_schemas(data: Mapping[str, Any], path: "postman.PathLike") -> bool:
    """Write a schemas file canonically (the collections' form). True when
    the bytes changed."""
    return postman.dump(data, path)


# -- validation (jsonschema, lazily) ---------------------------------------------------------


def _jsonschema():
    try:
        import jsonschema  # noqa: F401 -- the `apidocs` extra
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover -- an environment without the extra
        raise ImportError("common.apidocs needs jsonschema (the `apidocs` extra) to check or "
                          "validate schemas") from exc
    return Draft202012Validator


def json_path(parts: Any) -> str:
    """A JSONPath for an error location: ``$.votes[0].detail['a-b']``."""
    out = "$"
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(part)):
            out += f".{part}"
        else:
            out += "[" + json.dumps(str(part), ensure_ascii=False) + "]"
    return out


def check_schema(schema: Any) -> List[str]:
    """Problems with ``schema`` itself, against the draft 2020-12
    meta-schema; [] when it is valid. ``schema`` should be standalone."""
    validator_cls = _jsonschema()
    meta = validator_cls(validator_cls.META_SCHEMA)
    problems = [f"{json_path(e.absolute_path)}: {e.message}" for e in meta.iter_errors(schema)]
    if not problems:
        for ref in refs(schema):
            name = def_name(ref)
            if name is None or name not in (schema.get("$defs") or {}):
                problems.append(f"$ref {ref!r} does not resolve inside the schema")
    return sorted(set(problems))


_TYPE_NAMES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"),
               (list, "array"), (dict, "object"), (type(None), "null"))


def _json_type(value: Any) -> str:
    for cls, name in _TYPE_NAMES:
        if isinstance(value, cls):
            return name
    return type(value).__name__


def _short(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(error: Any) -> str:
    """One error as a rule, built from the SCHEMA side (the keyword and its
    value) and the instance's type -- never from the instance's value."""
    kind, rule, instance = error.validator, error.validator_value, error.instance
    if kind == "type":
        return f"is {_json_type(instance)}, expected {_short(rule)}"
    if kind == "required":
        missing = [name for name in rule if isinstance(instance, dict) and name not in instance]
        return "missing required " + ", ".join(repr(n) for n in missing)
    if kind in ("additionalProperties", "unevaluatedProperties") and isinstance(instance, dict):
        if kind == "additionalProperties":
            declared = set((error.schema.get("properties") or {}))
            patterns = list((error.schema.get("patternProperties") or {}))
            extra = [k for k in instance if k not in declared and not any(re.search(p, k) for p in patterns)]
        else:
            # What an allOf / $ref evaluated is the validator's to know; its
            # message names the keys (keys only, never values).
            found = re.search(r"\((.*) (?:was|were) unexpected\)", error.message)
            extra = [k for k in instance if found and repr(k) in found.group(1)] if found else []
        if extra:
            names = ", ".join(repr(k if len(k) <= 60 else k[:59] + "…") for k in extra[:10])
            return f"has undeclared properties: {names}" + (" …" if len(extra) > 10 else "")
        return f"fails {kind}"
    if kind == "enum":
        return f"is not one of {_short(rule)}"
    if kind == "const":
        return f"is not {_short(rule)}"
    if kind == "pattern":
        return f"does not match {rule!r}"
    if kind == "format":
        return f"is not a valid {rule}"
    if kind in ("anyOf", "oneOf"):
        if kind == "oneOf" and not error.context:
            return "matches more than one oneOf branch"
        return f"matches no {kind} branch"
    if kind is None:                      # a `false` schema
        return "is not allowed here"
    return f"fails {kind} {_short(rule)}"


def validate(schema: Any, instance: Any) -> List[str]:
    """Every way ``instance`` fails ``schema`` (a standalone schema), as
    ``"<JSONPath>: <rule>"`` lines, sorted; [] when it is valid. Format
    keywords are checked where jsonschema can (``uuid``, ``email``, ...).
    The messages name keys and rules, never values."""
    validator_cls = _jsonschema()
    from jsonschema.exceptions import best_match

    validator = validator_cls(schema, format_checker=validator_cls.FORMAT_CHECKER)
    out = []
    for error in validator.iter_errors(instance):
        line = f"{json_path(error.absolute_path)}: {_describe(error)}"
        if error.validator in ("anyOf", "oneOf") and error.context:
            best = best_match(error.context)
            line += f" (closest: {json_path(best.absolute_path)}: {_describe(best)})"
        out.append(line)
    return sorted(set(out))
