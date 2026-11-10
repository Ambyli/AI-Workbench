#!/usr/bin/env python3
"""Endpoint JSON Schemas: render them into Postman and the endpoint docs.

Every ``ai/<stack>/<stem>.postman_collection.json`` has (or will have) a
``<stem>.schemas.json`` beside it: a JSON Schema for each endpoint's request
and response bodies (root ``CLAUDE.md`` § Postman collections). This script
copies those schemas to where people read them and keeps the collections in
one canonical JSON form. The logic is ``common.apidocs``.

    render [--check] [<service>...]
        Write each entry into its endpoint's first Postman item (a final
        ``#### Schemas`` section; later items of the endpoint point to it)
        and into ``<doc stem>_SCHEMAS.md`` beside the endpoint doc, which
        links to it from a generated block. ``<service>`` is a
        schemas file's stem (``route-policy``); none = every one. With
        ``--check`` nothing is written and the exit status is 1 when a copy
        is stale (what ``unit-tests/apidocs`` runs).

    normalise <collection>...
        Rewrite collections in the canonical form (2-space indent,
        ensure_ascii=False, trailing newline, the file's newline style),
        asserting that ``json.loads`` of the file is unchanged.

    import-openapi <openapi.json> --endpoint "<METHOD> <path>" [...]
        Print schemas-file entries for those operations of an upstream
        OpenAPI document (refs inlined, OpenAPI 3.0 ``nullable`` turned into
        a ``null`` type, FastAPI's 422 left out) -- a starting point for an
        upstream server's subset, to be trimmed by hand. Paths as the OpenAPI
        document writes them (``/files/serve/{path}``); keys are printed in
        the collection's ``:name`` form.

Run from the repo root: ``uv run --no-sync python scripts/api_schemas.py render``.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]

from common.apidocs import postman, render, schemas  # noqa: E402 -- the workspace package


def schema_files(services: List[str]) -> List[pathlib.Path]:
    found = sorted(ROOT.glob(f"ai/*/*{schemas.SUFFIX}"))
    if not services:
        return found
    by_name = {p.name[: -len(schemas.SUFFIX)]: p for p in found}
    unknown = [s for s in services if s not in by_name]
    if unknown:
        raise SystemExit(f"no schemas file for {', '.join(unknown)} (have: {', '.join(sorted(by_name))})")
    return [by_name[s] for s in services]


def cmd_render(args: argparse.Namespace) -> int:
    stale = 0
    for path in schema_files(args.services):
        for changed in render.render_service(path, check=args.check):
            stale += 1
            rel = changed.relative_to(ROOT) if changed.is_relative_to(ROOT) else changed
            print(f"{'stale' if args.check else 'rendered'}: {rel.as_posix()}")
    if args.check and stale:
        print("run: uv run --no-sync python scripts/api_schemas.py render", file=sys.stderr)
        return 1
    return 0


def cmd_normalise(args: argparse.Namespace) -> int:
    for name in args.collections:
        path = pathlib.Path(name)
        old = postman.read_text(path)
        document = json.loads(old)
        new = postman.dumps(document, postman.newline_of(old))
        if json.loads(new) != document:          # cannot happen; the promise of this command
            raise SystemExit(f"{path}: normalising would change the JSON")
        if postman.write_text(path, new):
            print(f"normalised: {path.as_posix()}")
    return 0


# -- import-openapi --------------------------------------------------------------------------


def _openapi_lookup(document: Dict[str, Any]):
    def lookup(ref: str) -> Any:
        if not ref.startswith("#/"):
            raise KeyError(f"external $ref {ref!r}")
        node: Any = document
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node
    return lookup


def _nullable(schema: Any) -> Any:
    """OpenAPI 3.0's ``nullable: true`` as a JSON Schema ``null`` type."""

    def fix(node: Dict[str, Any]) -> Dict[str, Any]:
        if node.pop("nullable", False):
            kind = node.get("type")
            if isinstance(kind, str):
                node["type"] = [kind, "null"]
            elif isinstance(kind, list) and "null" not in kind:
                node["type"] = [*kind, "null"]
            elif "type" not in node:
                node = {"anyOf": [node, {"type": "null"}]}
        return node

    return schemas.transform(schema, fix)


def _body(media: Dict[str, Any], lookup, content_type: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"content_type": content_type}
    if "schema" in media:
        out["schema"] = _nullable(schemas.inline_refs(media["schema"], lookup))
    return out


def openapi_entry(document: Dict[str, Any], method: str, path: str, source: str) -> Dict[str, Any]:
    operation = (document.get("paths", {}).get(path) or {}).get(method.lower())
    if operation is None:
        raise SystemExit(f"{method} {path}: no such operation")
    lookup = _openapi_lookup(document)
    entry: Dict[str, Any] = {"$comment": f"from {source}; trim to the fields this stack uses"}
    content = ((operation.get("requestBody") or {}).get("content")) or {}
    if content:
        bodies = [_body(media, lookup, ctype) for ctype, media in content.items()]
        entry["request"] = bodies[0] if len(bodies) == 1 else bodies
    responses: Dict[str, Any] = {}
    for status, response in (operation.get("responses") or {}).items():
        if not re.match(r"^[1-5][0-9][0-9]$", str(status)) or str(status) == "422":
            continue
        if "$ref" in response:
            response = lookup(response["$ref"])
        media_types = response.get("content") or {}
        if not media_types:
            responses[str(status)] = {}
            continue
        ctype = "application/json" if "application/json" in media_types else next(iter(media_types))
        responses[str(status)] = _body(media_types[ctype], lookup, ctype)
    entry["responses"] = responses
    return entry


def cmd_import_openapi(args: argparse.Namespace) -> int:
    document = json.loads(postman.read_text(args.openapi))
    info = document.get("info") or {}
    source = f"{pathlib.Path(args.openapi).name} ({info.get('title', '?')} {info.get('version', '?')})"
    out: Dict[str, Any] = {}
    for spec in args.endpoint:
        method, _, path = spec.strip().partition(" ")
        path = path.strip()
        collection_path = re.sub(r"\{([^}]+)\}", r":\1", path)
        out[f"{method.upper()} {collection_path}"] = openapi_entry(document, method, path, source)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("render", help="write the schemas into the collections and docs")
    p.add_argument("--check", action="store_true", help="write nothing; exit 1 when a copy is stale")
    p.add_argument("services", nargs="*", help="schemas file stems (default: all)")
    p.set_defaults(func=cmd_render)
    p = sub.add_parser("normalise", help="rewrite collections in the canonical JSON form")
    p.add_argument("collections", nargs="+")
    p.set_defaults(func=cmd_normalise)
    p = sub.add_parser("import-openapi", help="print entries for operations of an OpenAPI document")
    p.add_argument("openapi")
    p.add_argument("--endpoint", action="append", required=True, help='"<METHOD> <path>", repeatable')
    p.set_defaults(func=cmd_import_openapi)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
