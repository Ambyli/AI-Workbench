# kokoro request and response schemas

Generated from [`kokoro.schemas.json`](kokoro.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`KOKORO.md`](KOKORO.md).

## `GET /health`

Response `200` (`application/json`) — kokoro-api is up; kokoro-app and the model are not checked:

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

## `GET /voices`

kokoro-app's body, passed through. A 4xx of kokoro-app's keeps its status and `detail`; kokoro-app unreachable or a 5xx of its own is a 502 `{"detail": "Cannot reach kokoro-app: …"}`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["voices"],
  "properties": {
    "voices": {
      "type": "array",
      "items": {
        "type": "string",
        "description": "A Kokoro voice; its first character is its language's code"
      },
      "description": "Every `voices/*.pt` file of the hexgrad/Kokoro-82M repo, without the folder and extension"
    }
  }
}
```

## `GET /languages`

kokoro-app's body, passed through. A 4xx of kokoro-app's keeps its status and `detail`; kokoro-app unreachable or a 5xx of its own is a 502 `{"detail": "Cannot reach kokoro-app: …"}`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["languages"],
  "properties": {
    "languages": {
      "type": "array",
      "description": "All nine languages Kokoro supports, in a fixed order, each with its voices",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["code", "name", "voices"],
        "properties": {
          "code": {
            "enum": ["a", "b", "e", "f", "h", "i", "j", "p", "z"],
            "description": "The first character of each of its voice names"
          },
          "name": {"type": "string"},
          "voices": {
            "type": "array",
            "items": {
              "type": "string",
              "description": "A Kokoro voice; its first character is its language's code"
            },
            "description": "Sorted"
          }
        }
      }
    }
  }
}
```

## `POST /generate`

`text` (required) and `voice` (default `af_heart`) are query parameters; there is no body. An unknown voice is kokoro-app's 400 `{"detail": "Unknown voice '<voice>'. Available: [...]"}`, passed through like any kokoro-app 4xx (its status and `detail` kept); kokoro-app unreachable or a 5xx of its own is a 502 `{"detail": "Cannot reach kokoro-app: …"}`. The internal kokoro-app item of the collection calls kokoro-app's own /generate, which answers 200 `application/json` `{"audio": "<the WAV bytes as hex>"}` instead (kokoro-api decodes it), and its 400s directly.

Response `200` (`audio/wav`) — kokoro-api: the WAV bytes (24 kHz) of the first chunk Kokoro's pipeline yields.

## `POST /v1/audio/speech`

OpenAI-compatible: `voice` maps an OpenAI alias (alloy, echo, fable, onyx, nova, shimmer) to an English Kokoro voice and passes any other name through. An unknown voice is kokoro-app's 400 `{"detail": "Unknown voice '<voice>'. Available: [...]"}`, passed through like any kokoro-app 4xx (its status and `detail` kept); kokoro-app unreachable or a 5xx of its own is a 502 `{"detail": "Cannot reach kokoro-app: …"}`.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["model", "input"],
  "properties": {
    "model": {
      "type": "string",
      "description": "Not read by kokoro-api (LiteLLM routes `kokoro` here)"
    },
    "input": {"type": "string", "description": "The text to speak"},
    "voice": {
      "type": "string",
      "default": "alloy",
      "description": "An OpenAI alias or a Kokoro voice name"
    },
    "response_format": {
      "type": ["string", "null"],
      "default": "wav",
      "description": "Accepted but IGNORED: Kokoro only produces WAV"
    },
    "speed": {"type": ["number", "null"], "default": 1.0, "description": "Accepted but not applied"}
  }
}
```

Response `200` (`audio/wav`) — the WAV bytes. Through LiteLLM (`{{litellm}}`) the same WAV bytes arrive labelled `audio/mpeg`: LiteLLM's proxy (its `/v1/audio/speech` handler in `proxy_server.py`) sets that type for every TTS model whose name is not a Gemini TTS one, whatever the upstream sent -- go by the bytes (`RIFF…WAVE`), not the header.

## `GET /audio/:audioFilename`

A WAV the `text_to_speech` tool wrote. 400 for a name holding `..`, `/` or `\`; 404 for a file that is not there: kokoro-api clears the directory at startup, and a `text_to_speech` save deletes WAVs older than `AUDIO_MAX_AGE_SECONDS` (a day; a WAV the tool hands out again gets a fresh age).

Response `200` (`audio/wav`) — the file.

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
        "name": {"const": "Kokoro TTS"},
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
          "name": {"const": "text_to_speech"},
          "description": {
            "type": "string",
            "description": "The tool's docstring summary: what the model reads"
          },
          "inputSchema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema: the arguments of `MCP text_to_speech`"
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

## `MCP text_to_speech`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` carries the same string. A kokoro-app failure is not a tool error: it comes back as the string.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "type": "object",
  "$comment": "text_to_speech(text: str, voice: str = \"alloy\") as FastMCP 3.4.5 lists it (it closes a tool's arguments)",
  "additionalProperties": false,
  "required": ["text"],
  "properties": {
    "text": {"type": "string", "description": "The text to convert to speech."},
    "voice": {
      "default": "alloy",
      "type": "string",
      "description": "Voice name. OpenAI aliases (alloy, echo, fable, onyx, nova, shimmer)\nall map to English voices. To speak another language, pass a Kokoro\nvoice name whose prefix identifies the language, e.g.\njf_alpha (Japanese), zf_xiaobei (Mandarin), ff_siwis (French).\nCall GET /languages to see voices grouped by language, or /voices for\nthe flat list."
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
      "description": "`${AUDIO_BASE_URL}/audio/<12 hex>.wav`, a URL GET /audio/:audioFilename serves for at least `AUDIO_MAX_AGE_SECONDS` (a day) unless kokoro-api restarts (the same text and voice again return the same URL); `Error generating audio: <kokoro-app's detail>` for a 4xx of kokoro-app's (an unknown voice); `Error generating audio: <httpx error>` when kokoro-app cannot be reached or answers a 5xx. FastMCP wraps a `str` return as `{\"result\": …}`"
    }
  }
}
```
