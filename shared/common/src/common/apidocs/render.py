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

**Too large to inline** (:func:`shown`): a block whose fully inlined display
exceeds :data:`INLINE_LIMIT` bytes keeps its ``$ref`` pointers instead (still
without ``title`` keywords, and without ``$defs``), followed by a ``Uses:``
line naming every definition it reaches, transitively. Each such definition
is shown ONCE per service, with its own ``$ref`` pointers kept
(:func:`definitions`): in a final ``## Definitions`` section of the
``*_SCHEMAS.md`` (one ``### `<name>``` subsection each, anchored
``def-<name>``, which the ``Uses:`` lines link to) and in a final
``#### Schema definitions`` section of the collection's ``info.description``
(after the error-bodies note, replaced on every render). Only definitions an
over-limit block reaches are listed; a service with none has neither section.

Every output keeps its file's newline style. :func:`render_service` does
one service -- the collection, the doc's block, the sibling file -- and
reports what changed (a missing sibling counts); ``check=True`` writes
nothing (what ``scripts/api_schemas.py render --check`` and the tests run).
Stdlib only.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import postman
from .schemas import Body, Endpoint, SchemaFile, def_name, inline_refs, load_schemas, refs, transform

SECTION_HEADING = "#### Schemas"
BEGIN = "<!-- api-schemas:begin -->"
END = "<!-- api-schemas:end -->"
LINK_HEADING = "## Request and response schemas"
DEFINITIONS_HEADING = "## Definitions"                  # the end of *_SCHEMAS.md
POSTMAN_DEFINITIONS_HEADING = "#### Schema definitions"  # the end of the collection's info.description

# A schema block whose fully inlined display (format_json(display_schema(...)),
# UTF-8) is larger than this keeps its $refs, and the definitions it reaches
# are shown once per service instead. 64 KiB: every block today is under
# 36 KB except the classifier's two job responses (~400 KB each, being 50
# definitions inlined many times over), so the limit only catches blocks
# that inlining blows up, with room for the others to grow.
INLINE_LIMIT = 64 * 1024


# -- display ----------------------------------------------------------------------------


def _drop_title(node: Dict[str, Any]) -> Dict[str, Any]:
    node.pop("title", None)
    return node


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

    return transform(inline_refs(body, lookup), _drop_title)


def display_definition(schema: Any) -> Any:
    """A definition (or an over-limit block) as it is shown: its ``$ref``s
    kept, ``title`` keywords dropped, no ``$defs``."""
    if not isinstance(schema, dict):
        return schema
    return transform({k: v for k, v in schema.items() if k != "$defs"}, _drop_title)


def reached(schema: Any, defs: Mapping[str, Any]) -> List[str]:
    """The names of the ``defs`` that ``schema`` reaches through ``$ref``,
    transitively, breadth first (what it names itself comes first), each
    once. ``schema``'s own ``$defs`` are not walked into."""
    body = {k: v for k, v in schema.items() if k != "$defs"} if isinstance(schema, dict) else schema
    order: List[str] = []
    queue = [def_name(r) for r in refs(body)]
    while queue:
        name = queue.pop(0)
        if name is None or name in order or name not in defs:
            continue
        order.append(name)
        queue.extend(def_name(r) for r in refs(defs[name]))
    return order


@dataclass(frozen=True)
class Shown:
    """How one schema block is displayed: ``text`` is its JSON; ``uses``
    is empty when it is inlined, else the definitions it reaches (in
    :func:`reached` order), which ``defs`` holds."""

    text: str
    uses: Tuple[str, ...] = ()
    defs: Mapping[str, Any] = field(default_factory=dict)


def shown(schema: Any) -> Shown:
    """``schema`` (standalone) fully inlined, or -- when that is over
    :data:`INLINE_LIMIT` bytes -- with its ``$ref``s kept."""
    inlined = format_json(display_schema(schema))
    if not isinstance(schema, dict) or len(inlined.encode("utf-8")) <= INLINE_LIMIT:
        return Shown(inlined)
    defs = schema.get("$defs") or {}
    uses = tuple(reached(schema, defs))
    return Shown(format_json(display_definition(schema)), uses, {name: defs[name] for name in uses})


def definitions(schema_file: SchemaFile) -> Dict[str, Any]:
    """Every definition an over-limit block of ``schema_file`` reaches, by
    name, sorted ignoring case -- what the Definitions sections show.
    Raises ValueError when two blocks reach different definitions under one
    name (only a generated schema's own ``$defs`` or a ``same_as`` borrowing
    from another file can do that: rename one)."""
    found: Dict[str, Any] = {}
    first: Dict[str, str] = {}
    for key, endpoint in schema_file.endpoints.items():
        for label, schema in endpoint.schemas():
            for name, sub in shown(schema).defs.items():
                if name in found and found[name] != sub:
                    raise ValueError(f"{schema_file.path.name}: definition {name!r} differs between "
                                     f"{first[name]} and {key} {label}; rename one")
                if name not in found:
                    found[name], first[name] = sub, f"{key} {label}"
    return dict(sorted(found.items(), key=lambda kv: (kv[0].lower(), kv[0])))


def anchor(name: str) -> str:
    """The HTML id of a definition's subsection in ``*_SCHEMAS.md``."""
    return "def-" + re.sub(r"[^A-Za-z0-9_-]", "-", name)


def _limit_text() -> str:
    return f"{INLINE_LIMIT // 1024} KiB" if INLINE_LIMIT % 1024 == 0 else f"{INLINE_LIMIT} bytes"


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


def _fenced(text: str) -> str:
    return "```json\n" + text + "\n```"


def schema_block(schema: Any, *, postman_copy: bool = False) -> str:
    """A standalone schema as a fenced ``json`` block -- plus, when it is
    over :data:`INLINE_LIMIT` (:func:`shown`), the ``Uses:`` line naming the
    definitions it reaches: linked to their ``### `<name>``` anchors in the
    ``*_SCHEMAS.md`` copy, pointing to the collection's description in the
    Postman one (``postman_copy``)."""
    view = shown(schema)
    block = _fenced(view.text)
    if not view.uses:
        return block
    why = f"too large to show inlined (over {_limit_text()}), so its `$ref`s are kept"
    if postman_copy:
        names = ", ".join(f"`{name}`" for name in view.uses)
        return (f"{block}\n\nUses: {names} — {why}; each definition is shown once under "
                f"*Schema definitions* at the end of the collection's description.")
    names = ", ".join(f"[`{name}`](#{anchor(name)})" for name in view.uses)
    return f"{block}\n\nUses: {names} — {why}; each definition is shown once under [Definitions](#definitions)."


_NO_BODY = re.compile(r"^no body\s*(?:[.:;,—–-]+\s*|$)", re.I)


def _line(label: str, body: Body) -> str:
    """``Response `200` (`application/json`) — <description>:`` -- a body
    with no content type reads ``…: no body``, and a description of its own
    that starts with "no body" is not made to say it twice."""
    description = body.description or ""
    if body.content_type:
        head = f"{label} (`{body.content_type}`)"
    else:
        head = f"{label}: no body"
        description = _NO_BODY.sub("", description, count=1).strip()
    if description:
        head += f" — {description}"
    return head + (":" if body.schema is not None else ".")


def endpoint_markdown(endpoint: Endpoint, *, postman_copy: bool = False) -> str:
    """One endpoint's schemas as Markdown (no heading); ``postman_copy``
    for the collection's copy (it only changes an over-limit block's
    ``Uses:`` line)."""
    parts: List[str] = []

    def block(schema: Any) -> str:
        return schema_block(endpoint.standalone(schema), postman_copy=postman_copy)

    if endpoint.same_as:
        parts.append(f"Same as `{endpoint.source}`.")
    if endpoint.description:
        parts.append(str(endpoint.description))
    if endpoint.is_mcp:
        entry = endpoint.entry
        if "arguments" in entry:
            parts += ["Arguments (`params.arguments` of `tools/call`):", block(entry["arguments"])]
        if "params" in entry:
            parts += ["Params (`params`):", block(entry["params"])]
        label = "Result (`result.structuredContent`):" if "arguments" in entry else "Result (`result`):"
        parts += [label, block(entry["result"])]
    else:
        for body in endpoint.requests():
            parts.append(_line("Request body", body))
            if body.schema is not None:
                parts.append(block(body.schema))
        for status, body in endpoint.all_responses():
            parts.append(_line(f"Response `{status}`", body))
            if body.schema is not None:
                parts.append(block(body.schema))
    return "\n\n".join(parts)


def _definition_blocks(defs: Mapping[str, Any], heading: str) -> List[str]:
    parts: List[str] = []
    for name, sub in defs.items():
        parts.append(heading.format(name=name, anchor=anchor(name)))
        parts.append(_fenced(format_json(display_definition(sub))))
    return parts


def definitions_markdown(schema_file: SchemaFile) -> str:
    """The ``## Definitions`` section that ends ``*_SCHEMAS.md``; "" when
    no block is over the limit."""
    defs = definitions(schema_file)
    if not defs:
        return ""
    intro = (f"The definitions (`#/$defs/<name>`) that the schemas above too large to show inlined (over "
             f"{_limit_text()}) point to, each shown once, with its own `$ref`s kept. Each such schema's "
             "`Uses:` line lists the ones it reaches.")
    return "\n\n".join([DEFINITIONS_HEADING, intro,
                        *_definition_blocks(defs, '### <a id="{anchor}"></a>`{name}`')])


# -- Postman ----------------------------------------------------------------------------------


def postman_section(endpoint: Endpoint) -> str:
    """The ``#### Schemas`` section of an endpoint's first item."""
    return f"{SECTION_HEADING}\n\n{endpoint_markdown(endpoint, postman_copy=True)}"


def sibling_section(first: postman.Request) -> str:
    """The one-line section of every later item of the endpoint."""
    return f"{SECTION_HEADING}\n\nSee *{first.label}*, the first request of this endpoint."


def postman_definitions_section(schema_file: SchemaFile) -> str:
    """The ``#### Schema definitions`` section that ends the collection's
    ``info.description``; "" when no block is over the limit."""
    defs = definitions(schema_file)
    if not defs:
        return ""
    intro = (f"Generated from `{schema_file.path.name}` by `scripts/api_schemas.py render` — never edit it. The "
             f"definitions (`#/$defs/<name>`) that the schemas too large to show inlined (over {_limit_text()}) "
             "point to, each shown once, with its own `$ref`s kept. Each such schema's `Uses:` line lists the "
             "ones it reaches.")
    return "\n\n".join([POSTMAN_DEFINITIONS_HEADING, intro, *_definition_blocks(defs, "##### `{name}`")])


def strip_section(description: str, heading: str = SECTION_HEADING) -> str:
    """``description`` without its final ``heading`` section (by default
    ``#### Schemas``)."""
    if description.startswith(heading + "\n"):
        return ""
    at = description.rfind("\n\n" + heading + "\n")
    return description if at < 0 else description[:at]


def _with_section(description: str, section: str, heading: str = SECTION_HEADING) -> str:
    base = strip_section(description, heading)
    if not section:
        return base
    return f"{base}\n\n{section}" if base else section


def apply_to_collection(collection: Dict[str, Any], schema_file: SchemaFile) -> int:
    """Write every item's section, and the ``info.description``'s
    definitions section, into ``collection`` (in place). Returns how many
    descriptions changed."""
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
    info = collection.setdefault("info", {})
    old = postman.get_description(info)
    new = _with_section(old, postman_definitions_section(schema_file), POSTMAN_DEFINITIONS_HEADING)
    if new != old:
        postman.set_description(info, new)
        changed += 1
    return changed


# -- Markdown ---------------------------------------------------------------------------------


def schemas_markdown(schema_file: SchemaFile) -> str:
    """The whole ``<doc stem>_SCHEMAS.md`` (LF line ends, trailing newline):
    a title, where it comes from, one ``##`` section per endpoint, and the
    ``## Definitions`` section when a block is over the limit."""
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
    defs = definitions_markdown(schema_file)
    if defs:
        parts.append(defs)
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
