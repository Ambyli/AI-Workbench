"""Postman v2.1 collections: read, write canonically, walk, key, resolve.

**Canonical form** (:func:`dumps` / :func:`dump`): ``json.dumps`` with a
2-space indent, ``ensure_ascii=False`` and a trailing newline, in the file's
own newline style (CRLF stays CRLF, LF stays LF; a new file is LF). Every
collection under ``ai/`` is kept in it, so a program can write descriptions
back without reformatting the whole file (``scripts/api_schemas.py
normalise`` / ``render``).

**Walking** (:func:`requests`): every request item in collection order,
folders flattened, each with its folder trail.

**Endpoint keys** (:func:`endpoint_key`): the key an endpoint's entry has in
its ``*.schemas.json`` -- ``"<METHOD> <path>"`` with the collection's
``:name`` path parameters and no query string (``"GET
/v1/route-policy/chats/:chatId/decisions"``); a JSON-RPC body (an MCP
request) is keyed by what it calls: ``"MCP <tool>"`` for ``tools/call``,
``"MCP <method>"`` for anything else (``"MCP initialize"``, ``"MCP
tools/list"``). Several items share one key (four ``POST /evaluate``
examples, eleven chat completions); the host is not part of it.

**Bodies** (:func:`body`): what an item sends, with ``{{variables}}``
resolved (:func:`resolve`) from the collection's ``variable`` defaults
(:func:`variables`) -- a JSON body parsed, a form body as a dict of its
enabled fields (a file part is its ``src`` path; a repeated key a list).

Stdlib only; no network, nothing Postman-specific beyond the v2.1 JSON.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

PathLike = Union[str, "os.PathLike[str]"]

# A {{name}} reference (Postman never nests them), and what a variable's
# name can look like (the collections use spaces: `virtual master key`).
VARIABLE_RE = re.compile(r"\{\{([^{}]+)\}\}")
VARIABLE_NAME_RE = re.compile(r"^\$?[A-Za-z_][A-Za-z0-9_ .-]*$")

# Postman's dynamic variables a body may use, pinned to fixed values so a
# validation run is deterministic. Any other {{$...}} is unknown.
DYNAMIC_VARIABLES: Dict[str, str] = {
    "$guid": "00000000-0000-4000-8000-000000000000",
    "$randomUUID": "00000000-0000-4000-8000-000000000000",
    "$timestamp": "1767225600",
    "$isoTimestamp": "2026-01-01T00:00:00.000Z",
    "$randomInt": "0",
}

JSON = "application/json"
MULTIPART = "multipart/form-data"
URLENCODED = "application/x-www-form-urlencoded"

# options.raw.language -> the media type a raw body is sent as.
_RAW_LANGUAGES = {
    "json": JSON,
    "text": "text/plain",
    "xml": "application/xml",
    "html": "text/html",
    "javascript": "application/javascript",
}


# -- files ------------------------------------------------------------------------


def read_text(path: PathLike) -> str:
    """A file's text exactly as stored (UTF-8, no newline translation)."""
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def write_text(path: PathLike, text: str) -> bool:
    """Write ``text`` as UTF-8 bytes, untranslated, unless the file already
    holds exactly that. True when it wrote."""
    data = text.encode("utf-8")
    try:
        with open(path, "rb") as handle:
            if handle.read() == data:
                return False
    except FileNotFoundError:
        pass
    with open(path, "wb") as handle:
        handle.write(data)
    return True


def newline_of(text: str) -> str:
    """``"\\r\\n"`` when ``text`` uses CRLF line ends, else ``"\\n"``. A JSON
    document has no raw newline inside a string, so every one is layout."""
    return "\r\n" if "\r\n" in text else "\n"


def dumps(document: Any, newline: str = "\n") -> str:
    """``document`` in the canonical form, with ``newline`` line ends."""
    if newline not in ("\n", "\r\n"):
        raise ValueError(f"newline must be LF or CRLF, not {newline!r}")
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    return text if newline == "\n" else text.replace("\n", "\r\n")


def load(path: PathLike) -> Any:
    """A collection (or any JSON file) as Python objects."""
    return json.loads(read_text(path))


def dump(document: Any, path: PathLike, *, newline: Optional[str] = None) -> bool:
    """Write ``document`` canonically. ``newline`` None keeps the existing
    file's style (LF for a new file). True when the bytes changed."""
    if newline is None:
        try:
            newline = newline_of(read_text(path))
        except FileNotFoundError:
            newline = "\n"
    return write_text(path, dumps(document, newline))


# -- walking ------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One request item of a collection, with the folders it sits in."""

    item: Dict[str, Any]
    folders: Tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return str(self.item.get("name", ""))

    @property
    def label(self) -> str:
        """``Folder / Sub-folder / Name`` -- unique where names repeat."""
        return " / ".join((*self.folders, self.name))

    @property
    def request(self) -> Dict[str, Any]:
        req = self.item.get("request")
        if isinstance(req, str):            # v2.1 allows a bare URL string
            req = {"method": "GET", "url": req}
            self.item["request"] = req
        return req if isinstance(req, dict) else {}

    @property
    def method(self) -> str:
        return str(self.request.get("method") or "GET").upper()

    @property
    def path(self) -> str:
        """``/a/:b/c`` -- the URL's path segments, no host, no query."""
        url = self.request.get("url")
        if isinstance(url, dict):
            segments = url.get("path")
            if isinstance(segments, list):
                return "/" + "/".join(str(s.get("value", "")) if isinstance(s, dict) else str(s)
                                      for s in segments)
            if isinstance(segments, str):
                return "/" + segments.lstrip("/")
            url = url.get("raw", "")
        return _path_of_raw(str(url or ""))


def _path_of_raw(raw: str) -> str:
    """The path of a raw URL string: scheme and host (or a leading
    ``{{host}}``) dropped, query and fragment cut."""
    raw = re.split(r"[?#]", raw, maxsplit=1)[0]
    raw = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", raw)
    if raw.startswith("{{"):
        end = raw.find("}}")
        raw = raw[end + 2:] if end >= 0 else raw
    elif "/" in raw:
        raw = raw[raw.index("/"):]
    else:
        raw = ""
    return "/" + raw.lstrip("/")


def requests(collection: Mapping[str, Any]) -> Iterator[Request]:
    """Every request item, depth first in collection order."""

    def walk(items: List[Any], trail: Tuple[str, ...]) -> Iterator[Request]:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("item"), list):
                yield from walk(item["item"], (*trail, str(item.get("name", ""))))
            elif "request" in item:
                yield Request(item, trail)

    yield from walk(list(collection.get("item") or []), ())


def variables(collection: Mapping[str, Any]) -> Dict[str, str]:
    """The collection's ``variable`` defaults (enabled ones), as strings."""
    out: Dict[str, str] = {}
    for var in collection.get("variable") or []:
        if not isinstance(var, dict) or var.get("disabled") or "key" not in var:
            continue
        value = var.get("value", "")
        out[str(var["key"])] = value if isinstance(value, str) else json.dumps(value)
    return out


def resolve(text: str, values: Mapping[str, str], *, strict: bool = True) -> str:
    """``text`` with every ``{{name}}`` replaced from ``values`` (then
    :data:`DYNAMIC_VARIABLES`). An unknown NAME raises ``KeyError(name)``
    when ``strict``; braces around anything that cannot be a variable name
    (JSX's ``style={{padding: '2rem'}}`` in a body's source code) are left
    as they are, as Postman sends them."""

    def sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in values:
            return str(values[name])
        if name in DYNAMIC_VARIABLES:
            return DYNAMIC_VARIABLES[name]
        if strict and VARIABLE_NAME_RE.match(name):
            raise KeyError(name)
        return match.group(0)

    return VARIABLE_RE.sub(sub, text)


# -- descriptions ---------------------------------------------------------------------


def get_description(obj: Mapping[str, Any]) -> str:
    """``obj["description"]`` as text (v2.1 allows ``{"content": ...}``)."""
    desc = obj.get("description")
    if isinstance(desc, dict):
        return str(desc.get("content") or "")
    return desc if isinstance(desc, str) else ""


def set_description(obj: Dict[str, Any], text: str) -> None:
    """Set ``obj["description"]``, keeping the ``{"content": ...}`` form if
    that is what it uses."""
    if isinstance(obj.get("description"), dict):
        obj["description"]["content"] = text
    else:
        obj["description"] = text


# -- bodies -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Body:
    """What one request item sends: its media type (no parameters,
    lower-case) and its value -- parsed JSON, a dict of form fields, text,
    or None for a file body."""

    content_type: str
    value: Any


def _header(req: Mapping[str, Any], name: str) -> Optional[str]:
    for h in req.get("header") or []:
        if isinstance(h, dict) and not h.get("disabled") and str(h.get("key", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return None


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def body(req: Union[Request, Mapping[str, Any]], values: Optional[Mapping[str, str]] = None) -> Optional[Body]:
    """The body ``req`` sends, variables resolved; None when it sends none.
    Raises ``KeyError`` for an unknown variable and ``ValueError`` for a
    JSON body that does not parse."""
    request = _request_of(req)
    values = values or {}
    spec = request.get("body")
    if not isinstance(spec, dict) or spec.get("disabled"):
        return None
    mode = spec.get("mode")
    header = _header(request, "Content-Type")
    if mode == "raw":
        raw = spec.get("raw")
        if not isinstance(raw, str) or not raw.strip():
            return None
        language = (((spec.get("options") or {}).get("raw") or {}).get("language") or "").lower()
        content_type = _media_type(resolve(header, values)) if header else _RAW_LANGUAGES.get(language, "text/plain")
        text = resolve(raw, values)
        if content_type == JSON or content_type.endswith("+json") or (not header and language == "json"):
            return Body(content_type, json.loads(text))
        return Body(content_type, text)
    if mode in ("formdata", "urlencoded"):
        fields: Dict[str, Any] = {}
        for part in spec.get(mode) or []:
            if not isinstance(part, dict) or part.get("disabled") or "key" not in part:
                continue
            if part.get("type") == "file":
                src = part.get("src")
                value: Any = " ".join(src) if isinstance(src, list) else str(src or "")
            else:
                value = resolve(str(part.get("value", "")), values)
            key = resolve(str(part["key"]), values)
            if key in fields:
                previous = fields[key]
                fields[key] = (previous if isinstance(previous, list) else [previous]) + [value]
            else:
                fields[key] = value
        return Body(MULTIPART if mode == "formdata" else URLENCODED, fields)
    if mode == "file":
        return Body(_media_type(header) if header else "application/octet-stream", None)
    return None


def _bare_variables_as_zero(raw: str) -> str:
    """``raw`` with each ``{{variable}}`` OUTSIDE a JSON string replaced by
    ``0`` (one inside a string is left alone)."""
    out, i, in_string = [], 0, False
    while i < len(raw):
        ch = raw[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < len(raw):
                out.append(raw[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        else:
            match = VARIABLE_RE.match(raw, i)
            if match:
                out.append("0")
                i = match.end()
                continue
            out.append(ch)
        i += 1
    return "".join(out)


def _loads_loose(raw: str) -> Any:
    """Parse a JSON body that may hold unresolved ``{{variables}}`` (a bare
    one outside a string counts as 0). None when it is not JSON."""
    for text in (raw, _bare_variables_as_zero(raw)):
        try:
            return json.loads(text)
        except ValueError:
            continue
    return None


def _request_of(req: Union[Request, Mapping[str, Any]]) -> Mapping[str, Any]:
    """The request object of a :class:`Request`, an item, or a request."""
    if isinstance(req, Request):
        return req.request
    if "request" in req:
        return Request(dict(req)).request
    return req


def json_rpc(req: Union[Request, Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The JSON-RPC message an item sends (an MCP request: an object with
    ``jsonrpc`` and a string ``method``), unresolved; None otherwise."""
    request = _request_of(req)
    spec = request.get("body")
    if not isinstance(spec, dict) or spec.get("mode") != "raw" or not isinstance(spec.get("raw"), str):
        return None
    message = _loads_loose(spec["raw"])
    if isinstance(message, dict) and "jsonrpc" in message and isinstance(message.get("method"), str):
        return message
    return None


def endpoint_key(req: Union[Request, Mapping[str, Any]]) -> str:
    """The key of the endpoint ``req`` calls (module docstring)."""
    if not isinstance(req, Request):
        req = Request(dict(req))
    message = json_rpc(req)
    if message is not None:
        if message["method"] == "tools/call":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            return f"MCP {params.get('name')}"
        return f"MCP {message['method']}"
    return f"{req.method} {req.path}"


def mcp_instance(req: Union[Request, Mapping[str, Any]], values: Optional[Mapping[str, str]] = None) -> Any:
    """What a JSON-RPC item sends for its endpoint's schema: a
    ``tools/call``'s ``params.arguments`` (``{}`` when absent), another
    method's ``params`` (None when absent). Variables resolved."""
    parsed = body(req, values)
    message = parsed.value if parsed is not None else None
    if not isinstance(message, dict):
        raise ValueError("not a JSON-RPC body")
    params = message.get("params")
    if message.get("method") == "tools/call":
        return (params or {}).get("arguments", {}) if isinstance(params, dict) else {}
    return params
