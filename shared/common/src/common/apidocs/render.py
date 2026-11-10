"""Copy a schemas file into the places people read: the Postman item
descriptions and a generated Markdown file beside the endpoint doc.

**Postman.** The FIRST item of each endpoint (collection order) ends its
``request.description`` with a ``#### Schemas`` section: the request body and
each response, as fenced ``json`` blocks. Every later item of the same
endpoint gets a one-line ``#### Schemas`` section naming that first item.
The section is always the description's last; rendering replaces it (or
appends it) and touches nothing before it, so a second run changes nothing.
An item whose endpoint has no entry loses any section it had.

**Markdown.** The schemas go to ``<doc stem>_SCHEMAS.md`` beside the doc
the file's ``doc`` names (``ROUTE_POLICY.md`` -> ``ROUTE_POLICY_SCHEMAS.md``,
:attr:`SchemaFile.schemas_doc_path`), generated whole: a title, a header
saying where it comes from, one ``##`` section per endpoint in the file's
order (:func:`schemas_markdown`). The doc itself keeps one short generated
block between ``<!-- api-schemas:begin -->`` and ``<!-- api-schemas:end -->``
-- a ``## Request and response schemas`` heading and a line linking to that
file (:func:`link_block`). Without the markers the block is appended at the
end of the doc; with them, only what is between them changes. The sibling
takes the doc's newline style.

**Display form** (:func:`display_schema`, :func:`format_json`): every
``$ref`` inlined (a recursive definition stays under ``$defs``), Pydantic's
``title`` keyword dropped wherever it is a keyword -- a property that is
NAMED ``title`` stays -- and 2-space JSON in which a value holding only
scalars (``{"type": "string"}``, ``["id", "version"]``) stays on one line
when it fits in 100 columns.

Every output keeps its file's newline style. :func:`render_service` does
one service -- the collection, the doc's block, the sibling file -- and
reports what changed (a missing sibling counts); ``check=True`` writes
nothing (what ``scripts/api_schemas.py render --check`` and the tests run).
Stdlib only.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Mapping, Optional

from . import postman
from .schemas import Body, Endpoint, SchemaFile, def_name, inline_refs, load_schemas, transform

SECTION_HEADING = "#### Schemas"
BEGIN = "<!-- api-schemas:begin -->"
END = "<!-- api-schemas:end -->"
LINK_HEADING = "## Request and response schemas"


# -- display ----------------------------------------------------------------------------


def display_schema(schema: Any) -> Any:
    """A standalone schema as it is shown: refs inlined, ``title`` keywords
    dropped."""
    if not isinstance(schema, dict):
        return schema
    defs = dict(schema.get("$defs") or {})
    body = {k: v for k, v in schema.items() if k != "$defs"}

    def lookup(ref: str) -> Any:
        name = def_name(ref)
        if name is None or name not in defs:
            raise KeyError(ref)
        return defs[name]

    inlined = inline_refs(body, lookup)

    def drop_title(node: Dict[str, Any]) -> Dict[str, Any]:
        node.pop("title", None)
        return node

    return transform(inlined, drop_title)


LINE_WIDTH = 100


def _flat(value: Any) -> bool:
    """Scalars, lists of scalars, and objects of those: what may sit on one line."""
    def scalar_list(v: Any) -> bool:
        return isinstance(v, list) and not any(isinstance(x, (dict, list)) for x in v)

    if isinstance(value, dict):
        return all(not isinstance(v, (dict, list)) or scalar_list(v) for v in value.values())
    return not isinstance(value, (dict, list)) or scalar_list(value)


def format_json(value: Any, *, indent: int = 0, prefix: int = 0) -> str:
    """``value`` as 2-space-indented JSON, with every flat value (``_flat``)
    that fits in :data:`LINE_WIDTH` on one line -- ``{"type": "string"}``,
    ``"required": ["id", "version"]`` -- so a schema reads as a shape, not a
    column of brackets. ``prefix`` is what precedes it on its first line."""
    one_line = json.dumps(value, ensure_ascii=False)
    if not isinstance(value, (dict, list)) or not value or \
            (_flat(value) and indent + prefix + len(one_line) <= LINE_WIDTH):
        return one_line
    pad = " " * (indent + 2)
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            head = json.dumps(key, ensure_ascii=False) + ": "
            lines.append(pad + head + format_json(item, indent=indent + 2, prefix=len(head)))
        return "{\n" + ",\n".join(lines) + "\n" + " " * indent + "}"
    return "[\n" + ",\n".join(pad + format_json(item, indent=indent + 2) for item in value) + "\n" + " " * indent + "]"


def schema_block(schema: Any) -> str:
    return "```json\n" + format_json(display_schema(schema)) + "\n```"


def _line(label: str, body: Body) -> str:
    head = f"{label} (`{body.content_type}`)" if body.content_type else f"{label}: no body"
    if body.description:
        head += f" — {body.description}"
    return head + (":" if body.schema is not None else ".")


def endpoint_markdown(endpoint: Endpoint) -> str:
    """One endpoint's schemas as Markdown (no heading)."""
    parts: List[str] = []
    if endpoint.same_as:
        parts.append(f"Same as `{endpoint.source}`.")
    if endpoint.description:
        parts.append(str(endpoint.description))
    if endpoint.is_mcp:
        entry = endpoint.entry
        if "arguments" in entry:
            parts += ["Arguments (`params.arguments` of `tools/call`):",
                      schema_block(endpoint.standalone(entry["arguments"]))]
        if "params" in entry:
            parts += ["Params (`params`):", schema_block(endpoint.standalone(entry["params"]))]
        label = "Result (`result.structuredContent`):" if "arguments" in entry else "Result (`result`):"
        parts += [label, schema_block(endpoint.standalone(entry["result"]))]
    else:
        for body in endpoint.requests():
            parts.append(_line("Request body", body))
            if body.schema is not None:
                parts.append(schema_block(endpoint.standalone(body.schema)))
        for status, body in endpoint.responses().items():
            parts.append(_line(f"Response `{status}`", body))
            if body.schema is not None:
                parts.append(schema_block(endpoint.standalone(body.schema)))
    return "\n\n".join(parts)


# -- Postman ----------------------------------------------------------------------------------


def postman_section(endpoint: Endpoint) -> str:
    """The ``#### Schemas`` section of an endpoint's first item."""
    return f"{SECTION_HEADING}\n\n{endpoint_markdown(endpoint)}"


def sibling_section(first: postman.Request) -> str:
    """The one-line section of every later item of the endpoint."""
    return f"{SECTION_HEADING}\n\nSee *{first.label}*, the first request of this endpoint."


def strip_section(description: str) -> str:
    """``description`` without its final ``#### Schemas`` section."""
    if description.startswith(SECTION_HEADING + "\n"):
        return ""
    at = description.rfind("\n\n" + SECTION_HEADING + "\n")
    return description if at < 0 else description[:at]


def _with_section(description: str, section: str) -> str:
    base = strip_section(description)
    if not section:
        return base
    return f"{base}\n\n{section}" if base else section


def apply_to_collection(collection: Dict[str, Any], schema_file: SchemaFile) -> int:
    """Write every item's section into ``collection`` (in place). Returns
    how many descriptions changed."""
    first: Dict[str, postman.Request] = {}
    changed = 0
    for req in postman.requests(collection):
        key = postman.endpoint_key(req)
        endpoint = schema_file.endpoints.get(key)
        if endpoint is None:
            section = ""
        elif key not in first:
            first[key] = req
            section = postman_section(endpoint)
        else:
            section = sibling_section(first[key])
        old = postman.get_description(req.request)
        new = _with_section(old, section)
        if new != old:
            postman.set_description(req.request, new)
            changed += 1
    return changed


# -- Markdown ---------------------------------------------------------------------------------


def schemas_markdown(schema_file: SchemaFile) -> str:
    """The whole ``<doc stem>_SCHEMAS.md`` (LF line ends, trailing newline):
    a title, where it comes from, one ``##`` section per endpoint."""
    name = schema_file.path.name
    header = (f"Generated from [`{name}`]({name}) by `uv run --no-sync python scripts/api_schemas.py render` "
              "(root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose "
              "`$comment` says `generated from …` comes from the code and is rewritten by the service's "
              "`test_api_schemas.py`. An error status not listed under an endpoint answers the generic error "
              "body the Postman collection's description names.")
    doc = schema_file.doc_path
    if doc is not None:
        header += f" Each endpoint's prose: [`{doc.name}`]({doc.name})."
    parts = [f"# {schema_file.service} request and response schemas", header]
    for key, endpoint in schema_file.endpoints.items():
        parts.append(f"## `{key}`")
        parts.append(endpoint_markdown(endpoint))
    return "\n\n".join(parts) + "\n"


def link_block(schema_file: SchemaFile) -> str:
    """What the endpoint doc holds between the markers (markers not
    included): a heading and one line pointing to the generated file."""
    target, source = schema_file.schemas_doc_path, schema_file.path.name
    if target is None:
        raise ValueError(f"{source} names no `doc`")
    return (f"{LINK_HEADING}\n\n"
            f"Every endpoint's request and response JSON Schemas: [`{target.name}`]({target.name}), generated "
            f"from [`{source}`]({source}) by `scripts/api_schemas.py render`.")


def apply_to_markdown(text: str, schema_file: SchemaFile) -> str:
    """The endpoint doc ``text`` with its generated link block replaced, or
    appended when it has no markers. Raises ValueError on a broken pair of
    markers."""
    newline = postman.newline_of(text)
    block = f"{BEGIN}\n{link_block(schema_file)}\n{END}"
    if newline != "\n":
        block = block.replace("\n", newline)
    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        return text.rstrip("\r\n") + newline * 2 + block + newline
    start, stop = text.find(BEGIN), text.find(END)
    if begins != 1 or ends != 1 or stop < start:
        raise ValueError(f"expected one {BEGIN} followed by one {END}")
    return text[:start] + block + text[stop + len(END):]


# -- files ------------------------------------------------------------------------------------


def render_service(schemas_path: "postman.PathLike", *, check: bool = False) -> List[pathlib.Path]:
    """Render one schemas file into its collection, its doc's link block and
    the doc's ``*_SCHEMAS.md``. Returns the files whose content differs from
    what the render produces -- a missing ``*_SCHEMAS.md`` included --
    written unless ``check``."""
    schema_file = load_schemas(schemas_path)
    changed: List[pathlib.Path] = []

    def update(path: pathlib.Path, old: Optional[str], new: str) -> None:
        if new != old:
            changed.append(path)
            if not check:
                postman.write_text(path, new)

    collection_path = schema_file.collection_path
    old = postman.read_text(collection_path)
    collection = json.loads(old)
    apply_to_collection(collection, schema_file)
    update(collection_path, old, postman.dumps(collection, postman.newline_of(old)))
    doc, target = schema_file.doc_path, schema_file.schemas_doc_path
    if doc is not None and target is not None:
        old = postman.read_text(doc)
        update(doc, old, apply_to_markdown(old, schema_file))
        newline = postman.newline_of(old)
        text = schemas_markdown(schema_file)
        try:
            current: Optional[str] = postman.read_text(target)
        except FileNotFoundError:
            current = None
        update(target, current, text if newline == "\n" else text.replace("\n", newline))
    return changed


def sizes(schema_file: SchemaFile) -> Mapping[str, int]:
    """Characters of each endpoint's rendered Markdown, largest first (a
    size check for reviews)."""
    out = {key: len(endpoint_markdown(e)) for key, e in schema_file.endpoints.items()}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
