# madlad request and response schemas

Generated from [`madlad.schemas.json`](madlad.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`MADLAD.md`](MADLAD.md).

## `GET /v1/madlad/health`

Response `200` (`application/json`) — madlad-api is up; madlad-app is not checked:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status"],
  "properties": {
    "status": {"const": "ok"}
  }
}
```

## `GET /v1/madlad/languages`

madlad-app's body, passed through. A 4xx of madlad-app's is passed through with its status and `detail`; madlad-app unreachable or a 5xx of its own is a 502 `{"detail": "Cannot reach madlad-app: …"}`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["languages"],
  "properties": {
    "languages": {
      "type": "array",
      "description": "Every target language the checkpoint's tokenizer has a `<2xx>` token for, sorted by code",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["code", "name"],
        "properties": {
          "code": {
            "type": "string",
            "pattern": "^[a-z]{2,3}(?:_[A-Za-z]+)?$",
            "description": "Used as `target_lang` in /translate. ISO 639-1 where available, otherwise ISO 639-3, occasionally with a regional suffix (e.g. `zh_CN`)"
          },
          "name": {
            "type": "string",
            "description": "Resolved via the `langcodes` library; an unknown code falls back to the code itself"
          }
        }
      }
    }
  }
}
```

## `POST /v1/madlad/translate`

madlad-app's body, passed through. An unknown `target_lang` is madlad-app's 400 `{"detail": "Unknown target_lang '<code>'. Use /languages to list supported codes."}`, passed through with that status and detail (any madlad-app 4xx is); madlad-app unreachable or a 5xx -- a model that produced nothing is its 500 -- is a 502 `{"detail": "Cannot reach madlad-app: …"}`. A body that is not JSON is FastAPI's 422 (`json_invalid`), as is a missing field.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["text", "target_lang"],
  "properties": {
    "text": {
      "type": "string",
      "description": "Source language is auto-detected; there is no `src` field"
    },
    "target_lang": {"type": "string", "description": "One of the codes GET /languages returns"}
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["translated"],
  "properties": {
    "translated": {"type": "string"}
  }
}
```

## `MCP initialize`

The JSON-RPC response's `result` (streamable HTTP at `/mcp/`: one SSE `data:` message when the request accepts `text/event-stream`); the `mcp-session-id` response header names the session the later calls send back.

Params (`params`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.InitializeRequestParams: the SDK allows extra keys",
  "required": ["protocolVersion", "capabilities", "clientInfo"],
  "properties": {
    "protocolVersion": {
      "type": "string",
      "description": "The latest version of the Model Context Protocol that the client supports"
    },
    "capabilities": {"type": "object", "additionalProperties": true},
    "clientInfo": {
      "type": "object",
      "additionalProperties": true,
      "required": ["name", "version"],
      "properties": {
        "name": {"type": "string"},
        "version": {"type": "string"}
      }
    }
  }
}
```

Result (`result`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.InitializeResult: the SDK allows extra keys",
  "required": ["protocolVersion", "capabilities", "serverInfo"],
  "properties": {
    "protocolVersion": {
      "type": "string",
      "description": "The version of the Model Context Protocol that the server wants to use; it may not match the version the client requested"
    },
    "capabilities": {
      "type": "object",
      "additionalProperties": true,
      "required": ["tools"],
      "description": "What the server supports (`mcp.types.ServerCapabilities`); FastMCP announces `tools`, `prompts`, `resources` and `logging`"
    },
    "serverInfo": {
      "type": "object",
      "additionalProperties": true,
      "required": ["name", "version"],
      "properties": {
        "name": {"const": "MADLAD Translation"},
        "version": {"type": "string", "description": "The version FastMCP reports"}
      }
    },
    "instructions": {"type": "string"}
  }
}
```

## `MCP tools/list`

The JSON-RPC response's `result` (one SSE `data:` message).

Params (`params`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.PaginatedRequestParams",
  "properties": {
    "cursor": {
      "type": "string",
      "description": "An opaque token representing the current pagination position"
    }
  }
}
```

Result (`result`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.ListToolsResult: the SDK allows extra keys",
  "required": ["tools"],
  "properties": {
    "tools": {
      "type": "array",
      "minItems": 1,
      "maxItems": 1,
      "items": {
        "type": "object",
        "additionalProperties": true,
        "$comment": "mcp.types.Tool: the SDK allows extra keys",
        "required": ["name", "inputSchema"],
        "properties": {
          "name": {"const": "translate"},
          "description": {
            "type": "string",
            "description": "The tool's docstring summary: what the model reads"
          },
          "inputSchema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema: the arguments of `MCP translate`"
          },
          "outputSchema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema of `structuredContent`: `{\"result\": <string>}` (FastMCP wraps a `str` return)"
          },
          "_meta": {"type": "object", "additionalProperties": true, "description": "FastMCP's own"}
        }
      }
    },
    "nextCursor": {"type": "string"}
  }
}
```

## `MCP translate`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` carries the same string. A madlad-app failure is not a tool error: it comes back as the string.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "type": "object",
  "$comment": "translate(text: str, target_lang: str) as FastMCP 3.4.5 lists it (it closes a tool's arguments)",
  "additionalProperties": false,
  "required": ["text", "target_lang"],
  "properties": {
    "text": {"type": "string", "description": "The source text. Source language is auto-detected."},
    "target_lang": {
      "type": "string",
      "description": "ISO 639-1 language code (e.g. \"es\", \"fr\", \"ja\", \"zh\").\nCall the /languages endpoint for the full list of supported codes."
    }
  }
}
```

Result (`result.structuredContent`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["result"],
  "properties": {
    "result": {
      "type": "string",
      "description": "The translated text; `Error translating: <madlad-app's detail>` for a 4xx of madlad-app's (an unknown target_lang); `Error translating: <httpx error>` when madlad-app cannot be reached or answers a 5xx. FastMCP wraps a `str` return as `{\"result\": …}`"
    }
  }
}
```
