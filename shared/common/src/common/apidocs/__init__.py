"""common.apidocs — Postman collections and the JSON Schemas of their
endpoints: walk, render, generate, validate.

Every ``ai/`` service with an HTTP API ships a Postman v2.1 collection
(``<stem>.postman_collection.json``) and, beside it, ``<stem>.schemas.json``:
a JSON Schema (draft 2020-12) for each endpoint's request body and response
bodies (root ``CLAUDE.md`` § Postman collections). The schemas file is the
one source; ``scripts/api_schemas.py render`` copies each entry into its
endpoint's first Postman item and into ``<doc stem>_SCHEMAS.md``, a
generated file beside the endpoint doc (which links to it from a short
generated block), and the tests check every copy, every collection body against
its request schema (``unit-tests/apidocs``) and each service's real
responses against its response schemas (``unit-tests/<service>/
test_api_schemas.py``).

Modules:
    postman    load / dump (canonical: 2-space indent, ensure_ascii=False,
               trailing newline, the file's newline style), requests() (walks
               folders), endpoint_key(), body() / mcp_instance(), variables(),
               resolve(), get_description() / set_description()
    schemas    load_schemas() (follows same_as across files) -> SchemaFile of
               Endpoint, standalone(), walk() / transform() / open_objects(),
               inline_refs(), check_schema() and validate() (jsonschema),
               dump_schemas()
    render     display_schema(), shown() (a block over INLINE_LIMIT keeps its
               $refs), definitions(), endpoint_markdown(), postman_section(),
               apply_to_collection(), schemas_markdown(), link_block(),
               apply_to_markdown(), render_service()
    generate   from_pydantic(), from_output_spec(), from_result_spec(),
               from_detector_spec(), by_discriminator(), from_mcp_tools() /
               from_mcp(), apply_generated()

Stdlib only, except ``schemas.check_schema`` and ``schemas.validate``,
which import ``jsonschema`` (the ``apidocs`` extra) when called. Nothing
here imports a service: callers hand over models, specs and tools.
"""

from . import generate, postman, render, schemas
from .postman import Request, body, endpoint_key, mcp_instance, requests, resolve, variables
from .render import apply_to_collection, apply_to_markdown, render_service
from .schemas import Endpoint, SchemaFile, SchemaFileError, check_schema, load_schemas, standalone, validate

__all__ = [
    "Endpoint",
    "Request",
    "SchemaFile",
    "SchemaFileError",
    "apply_to_collection",
    "apply_to_markdown",
    "body",
    "check_schema",
    "endpoint_key",
    "generate",
    "load_schemas",
    "mcp_instance",
    "postman",
    "render",
    "render_service",
    "requests",
    "resolve",
    "schemas",
    "standalone",
    "validate",
    "variables",
]
