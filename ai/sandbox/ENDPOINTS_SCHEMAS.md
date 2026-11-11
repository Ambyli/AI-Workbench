# sandbox-runner request and response schemas

Generated from [`sandbox-runner.schemas.json`](sandbox-runner.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`ENDPOINTS.md`](ENDPOINTS.md).

## `GET /v1/sandbox/health`

Always 200: a failed `sandbox-db` probe is `degraded` / `down`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "db"],
  "properties": {
    "status": {"enum": ["ok", "degraded"]},
    "db": {"enum": ["up", "down"]}
  }
}
```

## `POST /v1/sandbox/run`

A field validator's refusal (a file over SANDBOX_MAX_FILE_BYTES, a payload over SANDBOX_MAX_PAYLOAD_BYTES, a reserved `env` key, a malformed `session_id` / `chat_id`, a negative `ttl_seconds`) is FastAPI's 422; 429 the pool is full; 500 a Docker spawn error.

Request body (`application/json`):

```json
{
  "$comment": "generated from models.RunRequest — do not edit",
  "properties": {
    "runtime": {"description": "One of the keys in runtimes.RUNTIMES", "type": "string"},
    "files": {
      "additionalProperties": {
        "anyOf": [
          {"type": "string"},
          {"additionalProperties": true, "type": "object"}
        ]
      },
      "description": "Path → content map. On the first call for a session this is the initial file set. On a follow-up call (same session_id) it is an overlay — paths given here overwrite files in the running container, unlisted files are left alone. Values are either raw UTF-8 strings, or {\"encoding\": \"base64\", \"content\": \"...\"} for binary payloads (images, PDFs, wheels). Per-file cap: SANDBOX_MAX_FILE_BYTES; total cap: SANDBOX_MAX_PAYLOAD_BYTES.",
      "type": "object"
    },
    "entrypoint": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Shell command inside the sandbox; must bind to port 80"
    },
    "ttl_seconds": {
      "anyOf": [
        {"minimum": 0, "type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Idle TTL in seconds. Omit, null or 0 for SANDBOX_DEFAULT_TTL_SECONDS; a negative value is refused. Server clamps to SANDBOX_HARD_TTL_SECONDS."
    },
    "session_id": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Persistent identifier for a preview across turns. Omit on the first call; the server generates one and returns it. Pass the same value back on follow-up calls to update files in place — no respawn, same URL, dev server hot-reloads."
    },
    "deletes": {
      "description": "Relative paths (under /app) to delete on a follow-up call. Ignored on the first call. Absolute paths and '..' are rejected.",
      "items": {"type": "string"},
      "type": "array"
    },
    "env": {
      "anyOf": [
        {
          "additionalProperties": {"type": "string"},
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Process env vars set inside the container. Immutable after spawn (respawned self-heal replays the same env). Reserved keys HTTP_PROXY/HTTPS_PROXY/PYTHONUNBUFFERED/TERM/etc. are rejected — those are runner-controlled invariants."
    },
    "recreate_if_gone": {
      "default": true,
      "description": "If the session's container is gone, respawn a fresh one under the same session_id (env preserved; in-container files and exec-installed packages LOST). Default true — run is the one-shot path. Set false to get a 409 instead.",
      "type": "boolean"
    },
    "overwrite": {
      "default": false,
      "description": "Follow-up calls only: allow replacing an existing text file of 30+ lines with content that keeps most of its lines. Without it such a write is refused with 409 rewrite_of_existing_file — use edit_file for changes to part of a file. New files, small files, binary files and genuine rewrites are never refused.",
      "type": "boolean"
    },
    "chat_id": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Id of the chat/conversation this call belongs to. When set and no session_id is given, the runner reuses the chat's existing live sandbox instead of spawning a second one — so one conversation maps to one container. Pass it on every call for a chat and you never have to thread session_id yourself."
    }
  },
  "required": ["runtime"],
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "description": "`RunResponse` as the runner fills it",
  "allOf": [
    {
      "$comment": "generated from models.RunResponse — do not edit",
      "properties": {
        "sandbox_id": {"type": "string"},
        "session_id": {"type": "string"},
        "url": {"type": "string"},
        "expires_at": {"type": "string"},
        "reused": {"default": false, "type": "boolean"},
        "runtime": {
          "anyOf": [
            {"type": "string"},
            {"type": "null"}
          ],
          "default": null
        },
        "startup_output": {"default": "", "type": "string"},
        "app_status": {
          "anyOf": [
            {"additionalProperties": true, "type": "object"},
            {"type": "null"}
          ],
          "default": null
        },
        "recreated": {"default": false, "type": "boolean"}
      },
      "required": ["sandbox_id", "session_id", "url", "expires_at"],
      "type": "object",
      "additionalProperties": false
    }
  ],
  "properties": {
    "sandbox_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A sandbox's internal id: its job row's id, 12 hex characters"
    },
    "app_status": {
      "description": "On a fresh spawn (or a warming / existing sandbox) a placeholder `{code: 200, latency_ms: 0, note}` -- the readiness gate already passed; after a write, one live HTTP probe of the runtime's readiness path (3 s deadline): `{code, latency_ms}` for a real HTTP reply, `{error}` for a network failure",
      "oneOf": [
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["code", "latency_ms"],
          "properties": {
            "code": {"type": "integer", "description": "HTTP status the app answered"},
            "latency_ms": {"type": "integer", "minimum": 0},
            "note": {
              "type": "string",
              "description": "`readiness ok`, `warming`, or `existing sandbox for chat (phase=<phase>)`"
            }
          }
        },
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["error"],
          "properties": {
            "error": {
              "type": "string",
              "description": "`timeout after <s>s`, `connection refused (<reason>)`, or `<ExceptionType>: <message>`"
            }
          }
        }
      ]
    }
  }
}
```

Response `400` (`application/json`) — an unknown `runtime`, an `entrypoint` on a runtime that takes none, an unsafe path (a plain string), or static lint:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "object",
          "description": "Every `.py` file in the call is compiled before Docker is touched; nothing was written or spawned",
          "additionalProperties": false,
          "required": ["error", "session_id", "errors", "hint"],
          "properties": {
            "error": {"const": "static lint failed"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "errors": {
              "type": "array",
              "minItems": 1,
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["path", "line", "offset", "message", "text"],
                "properties": {
                  "path": {"type": "string"},
                  "line": {"type": ["integer", "null"]},
                  "offset": {"type": ["integer", "null"]},
                  "message": {"type": "string", "description": "`<ErrorType>: <msg>`"},
                  "text": {"type": "string", "description": "The offending source line"}
                }
              }
            },
            "hint": {"type": "string"}
          }
        }
      ]
    }
  }
}
```

Response `409` (`application/json`) — follow-up calls: the rewrite guard, or the container is gone and `recreate_if_gone` is false; or the one-sandbox-per-chat reservation failed:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {
          "type": "object",
          "description": "The rewrite guard: a listed path already exists as a text file of 30+ lines (WRITE_GUARD_MIN_LINES) and keeps >= 50 % of its lines (WRITE_GUARD_SIMILARITY); no file of the call was written",
          "additionalProperties": false,
          "required": ["error", "kind", "tool", "session_id", "files", "message", "hint"],
          "properties": {
            "error": {"const": "rewrite of existing file"},
            "kind": {"const": "rewrite_of_existing_file"},
            "tool": {"enum": ["write_files", "run"]},
            "session_id": {"type": "string"},
            "files": {
              "type": "array",
              "minItems": 1,
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": [
                  "path",
                  "similarity",
                  "changed_lines",
                  "old_lines",
                  "new_lines",
                  "identical"
                ],
                "properties": {
                  "path": {"type": "string"},
                  "similarity": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Line similarity of the new content to the existing file"
                  },
                  "changed_lines": {"type": "integer", "minimum": 0},
                  "old_lines": {"type": "integer", "minimum": 0},
                  "new_lines": {"type": "integer", "minimum": 0},
                  "identical": {"type": "boolean"}
                }
              }
            },
            "message": {"type": "string"},
            "hint": {"type": "string"}
          }
        },
        {
          "type": "object",
          "description": "The registry says running but the container is gone, and the call does not respawn",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "sandbox container is gone"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        },
        {
          "type": "object",
          "description": "The one-sandbox-per-chat index refused the spawn twice, and the winner could not be found",
          "additionalProperties": false,
          "required": ["error", "chat_id"],
          "properties": {
            "error": {"const": "could not reserve a sandbox for this chat — retry"},
            "chat_id": {"type": ["string", "null"]}
          }
        }
      ]
    }
  }
}
```

Response `504` (`application/json`) — the container did not answer its readiness probe within 30 s:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "The container never answered its readiness probe; it was torn down",
      "additionalProperties": false,
      "required": ["error", "session_id", "logs", "hint"],
      "properties": {
        "error": {"const": "sandbox did not become ready within 30s"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "logs": {
          "type": "string",
          "description": "The container's /tmp/sandbox.log tail, read before teardown"
        },
        "hint": {"type": "string"}
      }
    }
  }
}
```

## `POST /v1/sandbox/create`

The body is `RunResponse`, so the `warming` / `duplicate_create_prevented` / `chat_id` keys `create` computes are on the MCP tool's payload only. 400 an unknown `runtime` or an `entrypoint` on a runtime that takes none; 422 a malformed `chat_id`, a reserved `env` key or a negative `ttl_seconds`; 429 the pool is full; 500 a spawn error.

Request body (`application/json`):

```json
{
  "$comment": "generated from models.CreateRequest — do not edit",
  "properties": {
    "runtime": {"type": "string"},
    "entrypoint": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "ttl_seconds": {
      "anyOf": [
        {"minimum": 0, "type": "integer"},
        {"type": "null"}
      ],
      "default": null
    },
    "env": {
      "anyOf": [
        {
          "additionalProperties": {"type": "string"},
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null
    },
    "chat_id": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Id of the chat this create belongs to. When a live sandbox already exists for this chat_id, the runner returns THAT session instead of spawning a second container (duplicate_create_prevented=true in the response). One chat, one container."
    }
  },
  "required": ["runtime"],
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "description": "`RunResponse` as the runner fills it",
  "allOf": [
    {
      "$comment": "generated from models.RunResponse — do not edit",
      "properties": {
        "sandbox_id": {"type": "string"},
        "session_id": {"type": "string"},
        "url": {"type": "string"},
        "expires_at": {"type": "string"},
        "reused": {"default": false, "type": "boolean"},
        "runtime": {
          "anyOf": [
            {"type": "string"},
            {"type": "null"}
          ],
          "default": null
        },
        "startup_output": {"default": "", "type": "string"},
        "app_status": {
          "anyOf": [
            {"additionalProperties": true, "type": "object"},
            {"type": "null"}
          ],
          "default": null
        },
        "recreated": {"default": false, "type": "boolean"}
      },
      "required": ["sandbox_id", "session_id", "url", "expires_at"],
      "type": "object",
      "additionalProperties": false
    }
  ],
  "properties": {
    "sandbox_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A sandbox's internal id: its job row's id, 12 hex characters"
    },
    "app_status": {
      "description": "On a fresh spawn (or a warming / existing sandbox) a placeholder `{code: 200, latency_ms: 0, note}` -- the readiness gate already passed; after a write, one live HTTP probe of the runtime's readiness path (3 s deadline): `{code, latency_ms}` for a real HTTP reply, `{error}` for a network failure",
      "oneOf": [
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["code", "latency_ms"],
          "properties": {
            "code": {"type": "integer", "description": "HTTP status the app answered"},
            "latency_ms": {"type": "integer", "minimum": 0},
            "note": {
              "type": "string",
              "description": "`readiness ok`, `warming`, or `existing sandbox for chat (phase=<phase>)`"
            }
          }
        },
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["error"],
          "properties": {
            "error": {
              "type": "string",
              "description": "`timeout after <s>s`, `connection refused (<reason>)`, or `<ExceptionType>: <message>`"
            }
          }
        }
      ]
    }
  }
}
```

Response `409` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "The one-sandbox-per-chat index refused the spawn twice, and the winner could not be found",
      "additionalProperties": false,
      "required": ["error", "chat_id"],
      "properties": {
        "error": {"const": "could not reserve a sandbox for this chat — retry"},
        "chat_id": {"type": ["string", "null"]}
      }
    }
  }
}
```

Response `504` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "The container never answered its readiness probe; it was torn down",
      "additionalProperties": false,
      "required": ["error", "session_id", "logs", "hint"],
      "properties": {
        "error": {"const": "sandbox did not become ready within 30s"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "logs": {
          "type": "string",
          "description": "The container's /tmp/sandbox.log tail, read before teardown"
        },
        "hint": {"type": "string"}
      }
    }
  }
}
```

## `GET /v1/sandbox/sessions`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["count", "sessions"],
  "properties": {
    "count": {"type": "integer", "minimum": 0},
    "sessions": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One running sandbox, read back from its job row",
        "additionalProperties": false,
        "required": [
          "session_id",
          "sandbox_id",
          "runtime",
          "url",
          "created_at",
          "updated_at",
          "expires_at",
          "last_used_at",
          "phase"
        ],
        "properties": {
          "session_id": {
            "type": "string",
            "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
          },
          "sandbox_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{12}$",
            "description": "A sandbox's internal id: its job row's id, 12 hex characters"
          },
          "runtime": {"type": "string"},
          "url": {"type": "string"},
          "created_at": {"type": "string", "format": "date-time"},
          "updated_at": {"type": "string", "format": "date-time"},
          "expires_at": {"type": "string", "format": "date-time"},
          "last_used_at": {
            "type": "string",
            "format": "date-time",
            "description": "Bumped by every write / read_file / edit_file / exec"
          },
          "phase": {"const": "running"}
        }
      },
      "description": "Every running sandbox on the deployment, newest first -- no per-user filtering"
    }
  }
}
```

## `POST /v1/sandbox/session/:sandboxSessionId/files`

A file over SANDBOX_MAX_FILE_BYTES or a payload over SANDBOX_MAX_PAYLOAD_BYTES is FastAPI's 422.

Request body (`application/json`):

```json
{
  "$comment": "generated from models.WriteFilesRequest — do not edit",
  "properties": {
    "files": {
      "additionalProperties": {
        "anyOf": [
          {"type": "string"},
          {"additionalProperties": true, "type": "object"}
        ]
      },
      "description": "Path → content overlay. Absent paths are preserved. Values are str (UTF-8) or {encoding: 'base64', content: '...'}.",
      "type": "object"
    },
    "deletes": {
      "description": "Relative paths under /app to delete.",
      "items": {"type": "string"},
      "type": "array"
    },
    "recreate_if_gone": {
      "default": false,
      "description": "If true and the session's container is gone, respawn a fresh container under the same session_id (files and packages installed via exec are LOST; env is preserved). Default false — caller must opt-in explicitly.",
      "type": "boolean"
    },
    "overwrite": {
      "default": false,
      "description": "Allow replacing an existing text file of 30+ lines with content that keeps most of its lines. Without it such a write is refused with 409 rewrite_of_existing_file — use edit_file for changes to part of a file. New files, small files, binary files and genuine rewrites are never refused.",
      "type": "boolean"
    }
  },
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "description": "`RunResponse` as the runner fills it",
  "allOf": [
    {
      "$comment": "generated from models.RunResponse — do not edit",
      "properties": {
        "sandbox_id": {"type": "string"},
        "session_id": {"type": "string"},
        "url": {"type": "string"},
        "expires_at": {"type": "string"},
        "reused": {"default": false, "type": "boolean"},
        "runtime": {
          "anyOf": [
            {"type": "string"},
            {"type": "null"}
          ],
          "default": null
        },
        "startup_output": {"default": "", "type": "string"},
        "app_status": {
          "anyOf": [
            {"additionalProperties": true, "type": "object"},
            {"type": "null"}
          ],
          "default": null
        },
        "recreated": {"default": false, "type": "boolean"}
      },
      "required": ["sandbox_id", "session_id", "url", "expires_at"],
      "type": "object",
      "additionalProperties": false
    }
  ],
  "properties": {
    "sandbox_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A sandbox's internal id: its job row's id, 12 hex characters"
    },
    "app_status": {
      "description": "On a fresh spawn (or a warming / existing sandbox) a placeholder `{code: 200, latency_ms: 0, note}` -- the readiness gate already passed; after a write, one live HTTP probe of the runtime's readiness path (3 s deadline): `{code, latency_ms}` for a real HTTP reply, `{error}` for a network failure",
      "oneOf": [
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["code", "latency_ms"],
          "properties": {
            "code": {"type": "integer", "description": "HTTP status the app answered"},
            "latency_ms": {"type": "integer", "minimum": 0},
            "note": {
              "type": "string",
              "description": "`readiness ok`, `warming`, or `existing sandbox for chat (phase=<phase>)`"
            }
          }
        },
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["error"],
          "properties": {
            "error": {
              "type": "string",
              "description": "`timeout after <s>s`, `connection refused (<reason>)`, or `<ExceptionType>: <message>`"
            }
          }
        }
      ]
    }
  }
}
```

Response `400` (`application/json`) — an invalid `session_id` or an unsafe path (a plain string), or static lint:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "object",
          "description": "Every `.py` file in the call is compiled before Docker is touched; nothing was written or spawned",
          "additionalProperties": false,
          "required": ["error", "session_id", "errors", "hint"],
          "properties": {
            "error": {"const": "static lint failed"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "errors": {
              "type": "array",
              "minItems": 1,
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["path", "line", "offset", "message", "text"],
                "properties": {
                  "path": {"type": "string"},
                  "line": {"type": ["integer", "null"]},
                  "offset": {"type": ["integer", "null"]},
                  "message": {"type": "string", "description": "`<ErrorType>: <msg>`"},
                  "text": {"type": "string", "description": "The offending source line"}
                }
              }
            },
            "hint": {"type": "string"}
          }
        }
      ]
    }
  }
}
```

Response `404` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "No running sandbox for this `session_id`",
      "additionalProperties": false,
      "required": ["error", "session_id", "hint"],
      "properties": {
        "error": {"const": "session not found"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "hint": {"type": "string"}
      }
    }
  }
}
```

Response `409` (`application/json`) — the rewrite guard, or the container is gone and `recreate_if_gone` is false:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {
          "type": "object",
          "description": "The rewrite guard: a listed path already exists as a text file of 30+ lines (WRITE_GUARD_MIN_LINES) and keeps >= 50 % of its lines (WRITE_GUARD_SIMILARITY); no file of the call was written",
          "additionalProperties": false,
          "required": ["error", "kind", "tool", "session_id", "files", "message", "hint"],
          "properties": {
            "error": {"const": "rewrite of existing file"},
            "kind": {"const": "rewrite_of_existing_file"},
            "tool": {"enum": ["write_files", "run"]},
            "session_id": {"type": "string"},
            "files": {
              "type": "array",
              "minItems": 1,
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": [
                  "path",
                  "similarity",
                  "changed_lines",
                  "old_lines",
                  "new_lines",
                  "identical"
                ],
                "properties": {
                  "path": {"type": "string"},
                  "similarity": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Line similarity of the new content to the existing file"
                  },
                  "changed_lines": {"type": "integer", "minimum": 0},
                  "old_lines": {"type": "integer", "minimum": 0},
                  "new_lines": {"type": "integer", "minimum": 0},
                  "identical": {"type": "boolean"}
                }
              }
            },
            "message": {"type": "string"},
            "hint": {"type": "string"}
          }
        },
        {
          "type": "object",
          "description": "The registry says running but the container is gone, and the call does not respawn",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "sandbox container is gone"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        },
        {
          "type": "object",
          "description": "The one-sandbox-per-chat index refused the spawn twice, and the winner could not be found",
          "additionalProperties": false,
          "required": ["error", "chat_id"],
          "properties": {
            "error": {"const": "could not reserve a sandbox for this chat — retry"},
            "chat_id": {"type": ["string", "null"]}
          }
        }
      ]
    }
  }
}
```

Response `500` (`application/json`) — a spawn error (a plain string), or a self-heal with no runtime recorded:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "object",
          "description": "`recreate_if_gone` on a session whose job row records no known runtime",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "cannot self-heal: runtime metadata missing"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        }
      ]
    }
  }
}
```

Response `504` (`application/json`) — a self-heal respawn did not become ready:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "The container never answered its readiness probe; it was torn down",
      "additionalProperties": false,
      "required": ["error", "session_id", "logs", "hint"],
      "properties": {
        "error": {"const": "sandbox did not become ready within 30s"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "logs": {
          "type": "string",
          "description": "The container's /tmp/sandbox.log tail, read before teardown"
        },
        "hint": {"type": "string"}
      }
    }
  }
}
```

## `GET /v1/sandbox/session/:sandboxSessionId/files`

400 an invalid `session_id`, 404 no running sandbox for it.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["session_id", "sandbox_id", "files"],
  "properties": {
    "session_id": {
      "type": "string",
      "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
    },
    "sandbox_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A sandbox's internal id: its job row's id, 12 hex characters"
    },
    "files": {
      "type": "array",
      "items": {
        "anyOf": [
          {
            "type": "object",
            "description": "A regular file under /app (no `paths`: a listing, no contents)",
            "additionalProperties": false,
            "required": ["path", "size"],
            "properties": {
              "path": {"type": "string", "description": "Relative to /app"},
              "size": {"type": "integer", "minimum": 0}
            }
          },
          {
            "type": "object",
            "additionalProperties": false,
            "required": ["path", "size", "encoding", "content", "truncated"],
            "properties": {
              "path": {"type": "string", "description": "Relative to /app"},
              "size": {
                "type": "integer",
                "minimum": 0,
                "description": "The file's real size in bytes"
              },
              "encoding": {
                "enum": ["utf-8", "base64"],
                "description": "base64 for a file with a NUL byte or invalid UTF-8"
              },
              "content": {
                "type": "string",
                "description": "The first `max_bytes_per_file` bytes; a truncated text file is cut on a character boundary"
              },
              "truncated": {"type": "boolean"},
              "error": {
                "type": "string",
                "description": "`not found`, or why the path could not be read (`size` 0, empty `content`)"
              }
            }
          }
        ]
      },
      "description": "Without `paths`: every regular file under /app (path + size); with `paths`: each one's content, binary as base64, a missing one with `error`"
    }
  }
}
```

Response `409` (`application/json`) — the session's container is gone (get_files never respawns):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "The registry says running but the container is gone, and the call does not respawn",
      "additionalProperties": false,
      "required": ["error", "session_id", "hint"],
      "properties": {
        "error": {"const": "sandbox container is gone"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "hint": {"type": "string"}
      }
    }
  }
}
```

## `GET /v1/sandbox/session/:sandboxSessionId/file`

Counts as activity (bumps `last_used_at`). A missing `path` query is FastAPI's 422.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "session_id",
    "sandbox_id",
    "path",
    "start_line",
    "end_line",
    "total_lines",
    "more_lines",
    "truncated",
    "lines_cut",
    "size",
    "file_truncated",
    "content"
  ],
  "properties": {
    "session_id": {
      "type": "string",
      "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
    },
    "sandbox_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A sandbox's internal id: its job row's id, 12 hex characters"
    },
    "path": {"type": "string", "description": "The normalised relative path"},
    "start_line": {"type": "integer", "minimum": 1},
    "end_line": {
      "type": "integer",
      "minimum": 0,
      "description": "The last line shown (start_line - 1 for an empty window)"
    },
    "total_lines": {
      "type": "integer",
      "minimum": 0,
      "description": "Lines in the file (in its first READ_FILE_MAX_BYTES when `file_truncated`)"
    },
    "more_lines": {"type": "integer", "minimum": 0, "description": "Lines after `end_line`"},
    "truncated": {
      "type": "boolean",
      "description": "More lines follow `end_line` (ask again with offset = end_line + 1)"
    },
    "lines_cut": {
      "type": "integer",
      "minimum": 0,
      "description": "Lines cut at READ_FILE_MAX_LINE_CHARS"
    },
    "size": {"type": "integer", "minimum": 0, "description": "The file's size in bytes"},
    "file_truncated": {
      "type": "boolean",
      "description": "Only the first READ_FILE_MAX_BYTES were read"
    },
    "content": {"type": "string", "description": "`cat -n` style: the line number, a TAB, the line"}
  }
}
```

Response `400` (`application/json`) — an invalid `session_id` (a plain string), or `unsafe_path`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "object",
          "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
          "additionalProperties": false,
          "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
          "properties": {
            "error": {
              "enum": [
                "unsafe path",
                "file does not exist",
                "path is a directory",
                "not a regular file",
                "not a UTF-8 text file",
                "file too large",
                "old_string is empty",
                "old_string equals new_string",
                "old_string not found",
                "old_string contains read_file line numbers",
                "old_string matches more than once"
              ]
            },
            "kind": {
              "enum": [
                "unsafe_path",
                "missing_file",
                "is_directory",
                "not_a_file",
                "binary_file",
                "too_large",
                "empty_old_string",
                "no_change",
                "not_found",
                "line_number_prefix",
                "multiple_matches"
              ]
            },
            "tool": {"enum": ["read_file", "edit_file"]},
            "session_id": {"type": "string"},
            "path": {"type": "string"},
            "message": {"type": "string"},
            "hint": {"type": "string", "description": "The exact next action"},
            "closest_start_line": {
              "type": "integer",
              "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
            },
            "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
            "similarity": {
              "type": "number",
              "description": "not_found: how close it is (2 decimals)"
            },
            "closest_text": {
              "type": "string",
              "description": "not_found: the closest region, numbered like read_file"
            },
            "whitespace_only": {
              "type": "boolean",
              "description": "not_found: it differs only in whitespace / indentation"
            },
            "matches_without_prefixes": {
              "type": "integer",
              "description": "line_number_prefix: how often old_string matches with the prefixes removed"
            },
            "count": {
              "type": "integer",
              "description": "multiple_matches: how often old_string occurs"
            },
            "match_lines": {
              "type": "array",
              "items": {"type": "integer"},
              "description": "multiple_matches: the first line of each match (up to 20)"
            }
          },
          "allOf": [
            {
              "if": {
                "properties": {
                  "kind": {"const": "line_number_prefix"}
                }
              },
              "then": {"required": ["matches_without_prefixes"]}
            },
            {
              "if": {
                "properties": {
                  "kind": {"const": "multiple_matches"}
                }
              },
              "then": {"required": ["count", "match_lines"]}
            }
          ]
        }
      ]
    }
  }
}
```

Response `404` (`application/json`) — no running session, or `missing_file`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {
          "type": "object",
          "description": "No running sandbox for this `session_id`",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "session not found"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        },
        {
          "type": "object",
          "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
          "additionalProperties": false,
          "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
          "properties": {
            "error": {
              "enum": [
                "unsafe path",
                "file does not exist",
                "path is a directory",
                "not a regular file",
                "not a UTF-8 text file",
                "file too large",
                "old_string is empty",
                "old_string equals new_string",
                "old_string not found",
                "old_string contains read_file line numbers",
                "old_string matches more than once"
              ]
            },
            "kind": {
              "enum": [
                "unsafe_path",
                "missing_file",
                "is_directory",
                "not_a_file",
                "binary_file",
                "too_large",
                "empty_old_string",
                "no_change",
                "not_found",
                "line_number_prefix",
                "multiple_matches"
              ]
            },
            "tool": {"enum": ["read_file", "edit_file"]},
            "session_id": {"type": "string"},
            "path": {"type": "string"},
            "message": {"type": "string"},
            "hint": {"type": "string", "description": "The exact next action"},
            "closest_start_line": {
              "type": "integer",
              "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
            },
            "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
            "similarity": {
              "type": "number",
              "description": "not_found: how close it is (2 decimals)"
            },
            "closest_text": {
              "type": "string",
              "description": "not_found: the closest region, numbered like read_file"
            },
            "whitespace_only": {
              "type": "boolean",
              "description": "not_found: it differs only in whitespace / indentation"
            },
            "matches_without_prefixes": {
              "type": "integer",
              "description": "line_number_prefix: how often old_string matches with the prefixes removed"
            },
            "count": {
              "type": "integer",
              "description": "multiple_matches: how often old_string occurs"
            },
            "match_lines": {
              "type": "array",
              "items": {"type": "integer"},
              "description": "multiple_matches: the first line of each match (up to 20)"
            }
          },
          "allOf": [
            {
              "if": {
                "properties": {
                  "kind": {"const": "line_number_prefix"}
                }
              },
              "then": {"required": ["matches_without_prefixes"]}
            },
            {
              "if": {
                "properties": {
                  "kind": {"const": "multiple_matches"}
                }
              },
              "then": {"required": ["count", "match_lines"]}
            }
          ]
        }
      ]
    }
  }
}
```

Response `409` (`application/json`) — the container is gone (read_file never respawns), or `binary_file` / `is_directory` / `not_a_file`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {
          "type": "object",
          "description": "The registry says running but the container is gone, and the call does not respawn",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "sandbox container is gone"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        },
        {
          "type": "object",
          "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
          "additionalProperties": false,
          "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
          "properties": {
            "error": {
              "enum": [
                "unsafe path",
                "file does not exist",
                "path is a directory",
                "not a regular file",
                "not a UTF-8 text file",
                "file too large",
                "old_string is empty",
                "old_string equals new_string",
                "old_string not found",
                "old_string contains read_file line numbers",
                "old_string matches more than once"
              ]
            },
            "kind": {
              "enum": [
                "unsafe_path",
                "missing_file",
                "is_directory",
                "not_a_file",
                "binary_file",
                "too_large",
                "empty_old_string",
                "no_change",
                "not_found",
                "line_number_prefix",
                "multiple_matches"
              ]
            },
            "tool": {"enum": ["read_file", "edit_file"]},
            "session_id": {"type": "string"},
            "path": {"type": "string"},
            "message": {"type": "string"},
            "hint": {"type": "string", "description": "The exact next action"},
            "closest_start_line": {
              "type": "integer",
              "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
            },
            "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
            "similarity": {
              "type": "number",
              "description": "not_found: how close it is (2 decimals)"
            },
            "closest_text": {
              "type": "string",
              "description": "not_found: the closest region, numbered like read_file"
            },
            "whitespace_only": {
              "type": "boolean",
              "description": "not_found: it differs only in whitespace / indentation"
            },
            "matches_without_prefixes": {
              "type": "integer",
              "description": "line_number_prefix: how often old_string matches with the prefixes removed"
            },
            "count": {
              "type": "integer",
              "description": "multiple_matches: how often old_string occurs"
            },
            "match_lines": {
              "type": "array",
              "items": {"type": "integer"},
              "description": "multiple_matches: the first line of each match (up to 20)"
            }
          },
          "allOf": [
            {
              "if": {
                "properties": {
                  "kind": {"const": "line_number_prefix"}
                }
              },
              "then": {"required": ["matches_without_prefixes"]}
            },
            {
              "if": {
                "properties": {
                  "kind": {"const": "multiple_matches"}
                }
              },
              "then": {"required": ["count", "match_lines"]}
            }
          ]
        }
      ]
    }
  }
}
```

## `POST /v1/sandbox/session/:sandboxSessionId/edit`

All-or-nothing: no file is modified on any error. `old_string` / `new_string` over SANDBOX_MAX_FILE_BYTES, or an empty `path`, is FastAPI's 422.

Request body (`application/json`):

```json
{
  "$comment": "generated from models.EditFileRequest — do not edit",
  "description": "One exact-string replacement in one file — ``edit_file``.",
  "properties": {
    "path": {
      "description": "Relative path under /app of an EXISTING UTF-8 file. Absolute paths and '..' are rejected.",
      "type": "string"
    },
    "old_string": {
      "description": "The exact text to replace, copied from read_file output WITHOUT the line-number prefix. Must occur exactly once in the file unless replace_all is true.",
      "type": "string"
    },
    "new_string": {
      "description": "The replacement text. Must differ from old_string.",
      "type": "string"
    },
    "replace_all": {
      "default": false,
      "description": "Replace every occurrence of old_string instead of exactly one.",
      "type": "boolean"
    }
  },
  "required": ["path", "old_string", "new_string"],
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "allOf": [
    {
      "description": "What `edit_file` returns (`operations._do_edit_file`)",
      "properties": {
        "sandbox_id": {
          "type": "string",
          "pattern": "^[0-9a-f]{12}$",
          "description": "A sandbox's internal id: its job row's id, 12 hex characters"
        },
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "url": {"type": "string"},
        "expires_at": {"type": "string", "format": "date-time"},
        "runtime": {"type": ["string", "null"]},
        "path": {"type": "string"},
        "replacements": {"type": "integer", "minimum": 1},
        "start_line": {
          "type": "integer",
          "description": "The (first) replaced region in the NEW file"
        },
        "end_line": {"type": "integer"},
        "match_lines": {
          "type": "array",
          "items": {"type": "integer"},
          "description": "The first line of every replacement (up to 20)"
        },
        "total_lines": {"type": "integer", "minimum": 0},
        "startup_output": {
          "type": "string",
          "description": "What the dev server printed after this edit"
        },
        "app_status": {
          "description": "On a fresh spawn (or a warming / existing sandbox) a placeholder `{code: 200, latency_ms: 0, note}` -- the readiness gate already passed; after a write, one live HTTP probe of the runtime's readiness path (3 s deadline): `{code, latency_ms}` for a real HTTP reply, `{error}` for a network failure",
          "oneOf": [
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["code", "latency_ms"],
              "properties": {
                "code": {"type": "integer", "description": "HTTP status the app answered"},
                "latency_ms": {"type": "integer", "minimum": 0},
                "note": {
                  "type": "string",
                  "description": "`readiness ok`, `warming`, or `existing sandbox for chat (phase=<phase>)`"
                }
              }
            },
            {
              "type": "object",
              "additionalProperties": false,
              "required": ["error"],
              "properties": {
                "error": {
                  "type": "string",
                  "description": "`timeout after <s>s`, `connection refused (<reason>)`, or `<ExceptionType>: <message>`"
                }
              }
            }
          ]
        },
        "recreated": {"const": false}
      }
    }
  ],
  "properties": {
    "snippet": {
      "type": "string",
      "description": "The replaced region with 3 lines of context each side, numbered, at most 40 rows"
    }
  },
  "required": [
    "sandbox_id",
    "session_id",
    "url",
    "expires_at",
    "runtime",
    "path",
    "replacements",
    "start_line",
    "end_line",
    "match_lines",
    "total_lines",
    "startup_output",
    "app_status",
    "recreated",
    "snippet"
  ],
  "unevaluatedProperties": false
}
```

Response `400` (`application/json`) — an invalid `session_id` (a plain string), `unsafe_path`, or the edited `.py` file does not compile:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "object",
          "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
          "additionalProperties": false,
          "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
          "properties": {
            "error": {
              "enum": [
                "unsafe path",
                "file does not exist",
                "path is a directory",
                "not a regular file",
                "not a UTF-8 text file",
                "file too large",
                "old_string is empty",
                "old_string equals new_string",
                "old_string not found",
                "old_string contains read_file line numbers",
                "old_string matches more than once"
              ]
            },
            "kind": {
              "enum": [
                "unsafe_path",
                "missing_file",
                "is_directory",
                "not_a_file",
                "binary_file",
                "too_large",
                "empty_old_string",
                "no_change",
                "not_found",
                "line_number_prefix",
                "multiple_matches"
              ]
            },
            "tool": {"enum": ["read_file", "edit_file"]},
            "session_id": {"type": "string"},
            "path": {"type": "string"},
            "message": {"type": "string"},
            "hint": {"type": "string", "description": "The exact next action"},
            "closest_start_line": {
              "type": "integer",
              "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
            },
            "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
            "similarity": {
              "type": "number",
              "description": "not_found: how close it is (2 decimals)"
            },
            "closest_text": {
              "type": "string",
              "description": "not_found: the closest region, numbered like read_file"
            },
            "whitespace_only": {
              "type": "boolean",
              "description": "not_found: it differs only in whitespace / indentation"
            },
            "matches_without_prefixes": {
              "type": "integer",
              "description": "line_number_prefix: how often old_string matches with the prefixes removed"
            },
            "count": {
              "type": "integer",
              "description": "multiple_matches: how often old_string occurs"
            },
            "match_lines": {
              "type": "array",
              "items": {"type": "integer"},
              "description": "multiple_matches: the first line of each match (up to 20)"
            }
          },
          "allOf": [
            {
              "if": {
                "properties": {
                  "kind": {"const": "line_number_prefix"}
                }
              },
              "then": {"required": ["matches_without_prefixes"]}
            },
            {
              "if": {
                "properties": {
                  "kind": {"const": "multiple_matches"}
                }
              },
              "then": {"required": ["count", "match_lines"]}
            }
          ]
        },
        {
          "type": "object",
          "description": "Every `.py` file in the call is compiled before Docker is touched; nothing was written or spawned",
          "additionalProperties": false,
          "required": ["error", "session_id", "errors", "hint"],
          "properties": {
            "error": {"const": "static lint failed"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "errors": {
              "type": "array",
              "minItems": 1,
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["path", "line", "offset", "message", "text"],
                "properties": {
                  "path": {"type": "string"},
                  "line": {"type": ["integer", "null"]},
                  "offset": {"type": ["integer", "null"]},
                  "message": {"type": "string", "description": "`<ErrorType>: <msg>`"},
                  "text": {"type": "string", "description": "The offending source line"}
                }
              }
            },
            "hint": {"type": "string"}
          }
        }
      ]
    }
  }
}
```

Response `404` (`application/json`) — no running session, or `missing_file`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {
          "type": "object",
          "description": "No running sandbox for this `session_id`",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "session not found"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        },
        {
          "type": "object",
          "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
          "additionalProperties": false,
          "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
          "properties": {
            "error": {
              "enum": [
                "unsafe path",
                "file does not exist",
                "path is a directory",
                "not a regular file",
                "not a UTF-8 text file",
                "file too large",
                "old_string is empty",
                "old_string equals new_string",
                "old_string not found",
                "old_string contains read_file line numbers",
                "old_string matches more than once"
              ]
            },
            "kind": {
              "enum": [
                "unsafe_path",
                "missing_file",
                "is_directory",
                "not_a_file",
                "binary_file",
                "too_large",
                "empty_old_string",
                "no_change",
                "not_found",
                "line_number_prefix",
                "multiple_matches"
              ]
            },
            "tool": {"enum": ["read_file", "edit_file"]},
            "session_id": {"type": "string"},
            "path": {"type": "string"},
            "message": {"type": "string"},
            "hint": {"type": "string", "description": "The exact next action"},
            "closest_start_line": {
              "type": "integer",
              "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
            },
            "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
            "similarity": {
              "type": "number",
              "description": "not_found: how close it is (2 decimals)"
            },
            "closest_text": {
              "type": "string",
              "description": "not_found: the closest region, numbered like read_file"
            },
            "whitespace_only": {
              "type": "boolean",
              "description": "not_found: it differs only in whitespace / indentation"
            },
            "matches_without_prefixes": {
              "type": "integer",
              "description": "line_number_prefix: how often old_string matches with the prefixes removed"
            },
            "count": {
              "type": "integer",
              "description": "multiple_matches: how often old_string occurs"
            },
            "match_lines": {
              "type": "array",
              "items": {"type": "integer"},
              "description": "multiple_matches: the first line of each match (up to 20)"
            }
          },
          "allOf": [
            {
              "if": {
                "properties": {
                  "kind": {"const": "line_number_prefix"}
                }
              },
              "then": {"required": ["matches_without_prefixes"]}
            },
            {
              "if": {
                "properties": {
                  "kind": {"const": "multiple_matches"}
                }
              },
              "then": {"required": ["count", "match_lines"]}
            }
          ]
        }
      ]
    }
  }
}
```

Response `409` (`application/json`) — the container is gone (no self-heal), or a refused edit: `not_found`, `multiple_matches`, `line_number_prefix`, `no_change`, `empty_old_string`, `binary_file`, `is_directory`, `not_a_file`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {
          "type": "object",
          "description": "The registry says running but the container is gone, and the call does not respawn",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "sandbox container is gone"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        },
        {
          "type": "object",
          "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
          "additionalProperties": false,
          "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
          "properties": {
            "error": {
              "enum": [
                "unsafe path",
                "file does not exist",
                "path is a directory",
                "not a regular file",
                "not a UTF-8 text file",
                "file too large",
                "old_string is empty",
                "old_string equals new_string",
                "old_string not found",
                "old_string contains read_file line numbers",
                "old_string matches more than once"
              ]
            },
            "kind": {
              "enum": [
                "unsafe_path",
                "missing_file",
                "is_directory",
                "not_a_file",
                "binary_file",
                "too_large",
                "empty_old_string",
                "no_change",
                "not_found",
                "line_number_prefix",
                "multiple_matches"
              ]
            },
            "tool": {"enum": ["read_file", "edit_file"]},
            "session_id": {"type": "string"},
            "path": {"type": "string"},
            "message": {"type": "string"},
            "hint": {"type": "string", "description": "The exact next action"},
            "closest_start_line": {
              "type": "integer",
              "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
            },
            "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
            "similarity": {
              "type": "number",
              "description": "not_found: how close it is (2 decimals)"
            },
            "closest_text": {
              "type": "string",
              "description": "not_found: the closest region, numbered like read_file"
            },
            "whitespace_only": {
              "type": "boolean",
              "description": "not_found: it differs only in whitespace / indentation"
            },
            "matches_without_prefixes": {
              "type": "integer",
              "description": "line_number_prefix: how often old_string matches with the prefixes removed"
            },
            "count": {
              "type": "integer",
              "description": "multiple_matches: how often old_string occurs"
            },
            "match_lines": {
              "type": "array",
              "items": {"type": "integer"},
              "description": "multiple_matches: the first line of each match (up to 20)"
            }
          },
          "allOf": [
            {
              "if": {
                "properties": {
                  "kind": {"const": "line_number_prefix"}
                }
              },
              "then": {"required": ["matches_without_prefixes"]}
            },
            {
              "if": {
                "properties": {
                  "kind": {"const": "multiple_matches"}
                }
              },
              "then": {"required": ["count", "match_lines"]}
            }
          ]
        }
      ]
    }
  }
}
```

Response `413` (`application/json`) — `too_large`: the file, or the edited file, is over SANDBOX_MAX_FILE_BYTES:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
      "additionalProperties": false,
      "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
      "properties": {
        "error": {
          "enum": [
            "unsafe path",
            "file does not exist",
            "path is a directory",
            "not a regular file",
            "not a UTF-8 text file",
            "file too large",
            "old_string is empty",
            "old_string equals new_string",
            "old_string not found",
            "old_string contains read_file line numbers",
            "old_string matches more than once"
          ]
        },
        "kind": {
          "enum": [
            "unsafe_path",
            "missing_file",
            "is_directory",
            "not_a_file",
            "binary_file",
            "too_large",
            "empty_old_string",
            "no_change",
            "not_found",
            "line_number_prefix",
            "multiple_matches"
          ]
        },
        "tool": {"enum": ["read_file", "edit_file"]},
        "session_id": {"type": "string"},
        "path": {"type": "string"},
        "message": {"type": "string"},
        "hint": {"type": "string", "description": "The exact next action"},
        "closest_start_line": {
          "type": "integer",
          "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
        },
        "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
        "similarity": {"type": "number", "description": "not_found: how close it is (2 decimals)"},
        "closest_text": {
          "type": "string",
          "description": "not_found: the closest region, numbered like read_file"
        },
        "whitespace_only": {
          "type": "boolean",
          "description": "not_found: it differs only in whitespace / indentation"
        },
        "matches_without_prefixes": {
          "type": "integer",
          "description": "line_number_prefix: how often old_string matches with the prefixes removed"
        },
        "count": {"type": "integer", "description": "multiple_matches: how often old_string occurs"},
        "match_lines": {
          "type": "array",
          "items": {"type": "integer"},
          "description": "multiple_matches: the first line of each match (up to 20)"
        }
      },
      "allOf": [
        {
          "if": {
            "properties": {
              "kind": {"const": "line_number_prefix"}
            }
          },
          "then": {"required": ["matches_without_prefixes"]}
        },
        {
          "if": {
            "properties": {
              "kind": {"const": "multiple_matches"}
            }
          },
          "then": {"required": ["count", "match_lines"]}
        }
      ]
    }
  }
}
```

## `POST /v1/sandbox/session/:sandboxSessionId/exec`

400 an invalid `session_id`, an empty `command` or an unsafe `working_dir`.

Request body (`application/json`):

```json
{
  "$comment": "generated from models.ExecRequest — do not edit",
  "properties": {
    "command": {"description": "Shell command (sh -c). Non-interactive.", "type": "string"},
    "timeout_seconds": {
      "anyOf": [
        {"type": "integer"},
        {"type": "null"}
      ],
      "default": null
    },
    "working_dir": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null
    }
  },
  "required": ["command"],
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "allOf": [
    {
      "properties": {
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "sandbox_id": {
          "type": "string",
          "pattern": "^[0-9a-f]{12}$",
          "description": "A sandbox's internal id: its job row's id, 12 hex characters"
        },
        "exit_code": {
          "type": "integer",
          "description": "-1 when the command timed out or the exec failed"
        },
        "duration_ms": {"type": "integer", "minimum": 0},
        "truncated": {
          "type": "boolean",
          "description": "Earlier output was dropped (the LAST EXEC_MAX_OUTPUT_BYTES are kept)"
        },
        "timed_out": {"type": "boolean"}
      }
    }
  ],
  "properties": {
    "command": {"type": "string", "description": "Echoed"},
    "output": {
      "type": "string",
      "description": "Combined stdout + stderr: the LAST EXEC_MAX_OUTPUT_BYTES, cut on a character boundary"
    }
  },
  "required": [
    "session_id",
    "sandbox_id",
    "command",
    "exit_code",
    "duration_ms",
    "output",
    "truncated",
    "timed_out"
  ],
  "unevaluatedProperties": false
}
```

Response `404` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "exec / preview: no running sandbox for this `session_id`",
      "additionalProperties": false,
      "required": ["error", "session_id", "hint"],
      "properties": {
        "error": {"const": "no running sandbox for this session"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "hint": {"type": "string"}
      }
    }
  }
}
```

Response `409` (`application/json`) — the session's container is gone (exec never respawns):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "The registry says running but the container is gone, and the call does not respawn",
      "additionalProperties": false,
      "required": ["error", "session_id", "hint"],
      "properties": {
        "error": {"const": "sandbox container is gone"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "hint": {"type": "string"}
      }
    }
  }
}
```

## `DELETE /v1/sandbox/session/:sandboxSessionId`

Idempotent: an unknown or expired session is a 204 too. 400 an invalid `session_id`.

Response `204`: no body.

## `GET /v1/sandbox/session/:sandboxSessionId/download`

Follows self-heal: the session's CURRENT sandbox. 400 an invalid `session_id`, 404 no running sandbox (or its container is gone).

Response `200` (`application/x-tar`) — The container's /app as a plain tar stream (archive root `app/`), `Content-Disposition: attachment; filename="sandbox-<sandbox_id>.tar"`.

## `GET /v1/sandbox/session/:sandboxSessionId/logs`

400 an invalid `session_id`, 404 no running sandbox.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "allOf": [
    {
      "properties": {
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "sandbox_id": {
          "type": "string",
          "pattern": "^[0-9a-f]{12}$",
          "description": "A sandbox's internal id: its job row's id, 12 hex characters"
        },
        "lines_requested": {
          "type": "integer",
          "description": "The `lines` asked for (the tail is clamped to 1..1000)"
        },
        "logs": {
          "type": "string",
          "description": "The tail of /tmp/sandbox.log: the app's stdout + stderr and `[browser]` lines; empty is not an error"
        }
      }
    }
  ],
  "required": ["session_id", "sandbox_id", "lines_requested", "logs"],
  "unevaluatedProperties": false
}
```

## `GET /v1/sandbox/jobs`

Response `200` (`application/json`):

```json
{
  "description": "The newest 20 job rows (PostgresRegistry.list_all), terminal ones included",
  "allOf": [
    {
      "$comment": "generated from common.jobs.model.JobsListResponse — do not edit",
      "description": "Response body for ``GET /jobs``.\n\n``max_concurrent`` and ``available`` are populated by backends that have\na concurrency ceiling (like the in-memory backend fronted by a port pool);\npersistent backends leave them ``None``.",
      "properties": {
        "active_count": {"type": "integer"},
        "jobs": {
          "items": {
            "description": "Snapshot of one job at a point in time.\n\nFields:\n    job_id: 12-char hex identifier assigned at registration.\n    phase: Freeform string; each consumer defines its own convention.\n        Interceptor-api uses ``\"cloning\" | \"capturing\" | \"actions\" |\n        \"cleaning_up\"`` (``\"actions\"`` while a capture's\n        ``page_script`` / ``actions`` run);\n        classifier uses ``\"staging\" | \"pending\" | \"processing\" |\n        \"completed\" | \"failed\" | \"cancelled\"``.\n    created_at: ISO-8601 UTC timestamp when the job was registered.\n    updated_at: ISO-8601 UTC timestamp of the last phase / result / error\n        transition. Equals ``created_at`` for a job that hasn't transitioned.\n    elapsed_seconds: Wall-clock seconds since ``created_at``, computed at\n        snapshot time from a monotonic clock. Present for ephemeral jobs;\n        derived from ``created_at`` for persistent (SQLite-backed) jobs\n        where the monotonic anchor is process-local and doesn't survive\n        restarts.\n    metadata: Consumer-defined dict describing the job. Typically the\n        result of a pydantic ``BaseModel.model_dump()``.\n    result: Populated on successful completion for persistent backends.\n        Ephemeral backends never set this — a completed ephemeral job is\n        already gone from the registry.\n    error: Populated on failure for persistent backends. Same caveat as\n        ``result`` for ephemeral.",
            "properties": {
              "job_id": {"type": "string"},
              "phase": {"type": "string"},
              "created_at": {"type": "string"},
              "updated_at": {"type": "string"},
              "elapsed_seconds": {"type": "number"},
              "metadata": {"additionalProperties": true, "type": "object"},
              "result": {
                "anyOf": [
                  {"additionalProperties": true, "type": "object"},
                  {"type": "null"}
                ],
                "default": null
              },
              "error": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null
              }
            },
            "required": [
              "job_id",
              "phase",
              "created_at",
              "updated_at",
              "elapsed_seconds",
              "metadata"
            ],
            "type": "object",
            "additionalProperties": false
          },
          "type": "array"
        },
        "max_concurrent": {
          "anyOf": [
            {"type": "integer"},
            {"type": "null"}
          ],
          "default": null
        },
        "available": {
          "anyOf": [
            {"type": "integer"},
            {"type": "null"}
          ],
          "default": null
        }
      },
      "required": ["active_count", "jobs"],
      "type": "object",
      "additionalProperties": false
    }
  ],
  "properties": {
    "jobs": {
      "items": {
        "description": "One sandbox's job row: the shared `common.jobs` `JobBase` with the runner's `metadata` / `result`",
        "allOf": [
          {
            "$comment": "generated from common.jobs.model.JobBase — do not edit",
            "description": "Snapshot of one job at a point in time.\n\nFields:\n    job_id: 12-char hex identifier assigned at registration.\n    phase: Freeform string; each consumer defines its own convention.\n        Interceptor-api uses ``\"cloning\" | \"capturing\" | \"actions\" |\n        \"cleaning_up\"`` (``\"actions\"`` while a capture's\n        ``page_script`` / ``actions`` run);\n        classifier uses ``\"staging\" | \"pending\" | \"processing\" |\n        \"completed\" | \"failed\" | \"cancelled\"``.\n    created_at: ISO-8601 UTC timestamp when the job was registered.\n    updated_at: ISO-8601 UTC timestamp of the last phase / result / error\n        transition. Equals ``created_at`` for a job that hasn't transitioned.\n    elapsed_seconds: Wall-clock seconds since ``created_at``, computed at\n        snapshot time from a monotonic clock. Present for ephemeral jobs;\n        derived from ``created_at`` for persistent (SQLite-backed) jobs\n        where the monotonic anchor is process-local and doesn't survive\n        restarts.\n    metadata: Consumer-defined dict describing the job. Typically the\n        result of a pydantic ``BaseModel.model_dump()``.\n    result: Populated on successful completion for persistent backends.\n        Ephemeral backends never set this — a completed ephemeral job is\n        already gone from the registry.\n    error: Populated on failure for persistent backends. Same caveat as\n        ``result`` for ephemeral.",
            "properties": {
              "job_id": {"type": "string"},
              "phase": {"type": "string"},
              "created_at": {"type": "string"},
              "updated_at": {"type": "string"},
              "elapsed_seconds": {"type": "number"},
              "metadata": {"additionalProperties": true, "type": "object"},
              "result": {
                "anyOf": [
                  {"additionalProperties": true, "type": "object"},
                  {"type": "null"}
                ],
                "default": null
              },
              "error": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null
              }
            },
            "required": [
              "job_id",
              "phase",
              "created_at",
              "updated_at",
              "elapsed_seconds",
              "metadata"
            ],
            "type": "object",
            "additionalProperties": false
          }
        ],
        "properties": {
          "job_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{12}$",
            "description": "A sandbox's internal id: its job row's id, 12 hex characters"
          },
          "phase": {
            "enum": ["spawning", "starting", "running", "expired", "closed", "failed"],
            "description": "`spawning` (container being created) → `starting` (readiness probe) → `running`; then `closed` (close / DELETE /session), `expired` (reaped, or replaced by a self-heal respawn) or `failed` (spawn or readiness failed, `error` set)"
          },
          "metadata": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "runtime",
              "entrypoint",
              "ttl_seconds",
              "expires_at",
              "session_id",
              "chat_id",
              "last_used_at",
              "env"
            ],
            "properties": {
              "runtime": {"type": "string"},
              "entrypoint": {
                "type": "string",
                "description": "The caller's entrypoint, else the runtime's default"
              },
              "ttl_seconds": {
                "type": "integer",
                "description": "The caller's `ttl_seconds` (else SANDBOX_DEFAULT_TTL_SECONDS), clamped to SANDBOX_HARD_TTL_SECONDS"
              },
              "expires_at": {"type": "string", "format": "date-time"},
              "session_id": {
                "type": "string",
                "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
              },
              "chat_id": {"type": ["string", "null"]},
              "last_used_at": {"type": "string", "format": "date-time"},
              "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "The caller's env, replayed on a self-heal respawn"
              },
              "warmed": {
                "const": true,
                "description": "Present on a `create` (a warming container)"
              }
            }
          },
          "result": {
            "oneOf": [
              {"type": "null"},
              {
                "type": "object",
                "additionalProperties": false,
                "required": ["url", "container_name"],
                "properties": {
                  "url": {"type": "string"},
                  "container_name": {"type": "string", "description": "`sandbox-<sandbox_id>`"}
                }
              }
            ],
            "description": "Set when the sandbox reached `running`"
          },
          "error": {"type": ["string", "null"], "description": "Why spawn / readiness failed"}
        }
      }
    },
    "max_concurrent": {
      "type": "null",
      "description": "The registry tracks no ceiling (the runner's semaphore does)"
    },
    "available": {"type": "null"}
  },
  "required": ["max_concurrent", "available"]
}
```

## `GET /v1/sandbox/jobs/:sandboxId`

404 no such sandbox.

Response `200` (`application/json`):

```json
{
  "description": "One sandbox's job row: the shared `common.jobs` `JobBase` with the runner's `metadata` / `result`",
  "allOf": [
    {
      "$comment": "generated from common.jobs.model.JobBase — do not edit",
      "description": "Snapshot of one job at a point in time.\n\nFields:\n    job_id: 12-char hex identifier assigned at registration.\n    phase: Freeform string; each consumer defines its own convention.\n        Interceptor-api uses ``\"cloning\" | \"capturing\" | \"actions\" |\n        \"cleaning_up\"`` (``\"actions\"`` while a capture's\n        ``page_script`` / ``actions`` run);\n        classifier uses ``\"staging\" | \"pending\" | \"processing\" |\n        \"completed\" | \"failed\" | \"cancelled\"``.\n    created_at: ISO-8601 UTC timestamp when the job was registered.\n    updated_at: ISO-8601 UTC timestamp of the last phase / result / error\n        transition. Equals ``created_at`` for a job that hasn't transitioned.\n    elapsed_seconds: Wall-clock seconds since ``created_at``, computed at\n        snapshot time from a monotonic clock. Present for ephemeral jobs;\n        derived from ``created_at`` for persistent (SQLite-backed) jobs\n        where the monotonic anchor is process-local and doesn't survive\n        restarts.\n    metadata: Consumer-defined dict describing the job. Typically the\n        result of a pydantic ``BaseModel.model_dump()``.\n    result: Populated on successful completion for persistent backends.\n        Ephemeral backends never set this — a completed ephemeral job is\n        already gone from the registry.\n    error: Populated on failure for persistent backends. Same caveat as\n        ``result`` for ephemeral.",
      "properties": {
        "job_id": {"type": "string"},
        "phase": {"type": "string"},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
        "elapsed_seconds": {"type": "number"},
        "metadata": {"additionalProperties": true, "type": "object"},
        "result": {
          "anyOf": [
            {"additionalProperties": true, "type": "object"},
            {"type": "null"}
          ],
          "default": null
        },
        "error": {
          "anyOf": [
            {"type": "string"},
            {"type": "null"}
          ],
          "default": null
        }
      },
      "required": ["job_id", "phase", "created_at", "updated_at", "elapsed_seconds", "metadata"],
      "type": "object",
      "additionalProperties": false
    }
  ],
  "properties": {
    "job_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A sandbox's internal id: its job row's id, 12 hex characters"
    },
    "phase": {
      "enum": ["spawning", "starting", "running", "expired", "closed", "failed"],
      "description": "`spawning` (container being created) → `starting` (readiness probe) → `running`; then `closed` (close / DELETE /session), `expired` (reaped, or replaced by a self-heal respawn) or `failed` (spawn or readiness failed, `error` set)"
    },
    "metadata": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "runtime",
        "entrypoint",
        "ttl_seconds",
        "expires_at",
        "session_id",
        "chat_id",
        "last_used_at",
        "env"
      ],
      "properties": {
        "runtime": {"type": "string"},
        "entrypoint": {
          "type": "string",
          "description": "The caller's entrypoint, else the runtime's default"
        },
        "ttl_seconds": {
          "type": "integer",
          "description": "The caller's `ttl_seconds` (else SANDBOX_DEFAULT_TTL_SECONDS), clamped to SANDBOX_HARD_TTL_SECONDS"
        },
        "expires_at": {"type": "string", "format": "date-time"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "chat_id": {"type": ["string", "null"]},
        "last_used_at": {"type": "string", "format": "date-time"},
        "env": {
          "type": "object",
          "additionalProperties": {"type": "string"},
          "description": "The caller's env, replayed on a self-heal respawn"
        },
        "warmed": {"const": true, "description": "Present on a `create` (a warming container)"}
      }
    },
    "result": {
      "oneOf": [
        {"type": "null"},
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["url", "container_name"],
          "properties": {
            "url": {"type": "string"},
            "container_name": {"type": "string", "description": "`sandbox-<sandbox_id>`"}
          }
        }
      ],
      "description": "Set when the sandbox reached `running`"
    },
    "error": {"type": ["string", "null"], "description": "Why spawn / readiness failed"}
  }
}
```

## `GET /v1/sandbox/jobs/:sandboxId/download`

404 no such sandbox (or its container is gone), 409 it never reached `running`.

Response `200` (`application/x-tar`) — The container's /app as a plain tar stream (archive root `app/`), `Content-Disposition: attachment; filename="sandbox-<sandbox_id>.tar"`.

## `GET /v1/sandbox/jobs/:sandboxId/logs`

404 no such sandbox, 409 it never reached `running`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["sandbox_id", "lines_requested", "logs"],
  "properties": {
    "sandbox_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A sandbox's internal id: its job row's id, 12 hex characters"
    },
    "lines_requested": {
      "type": "integer",
      "description": "The `lines` asked for (the tail is clamped to 1..1000)"
    },
    "logs": {"type": "string", "description": "The tail of that container's /tmp/sandbox.log"}
  }
}
```

## `DELETE /v1/sandbox/jobs/:sandboxId`

Stops the container, deletes the row, releases the slot if it held one. 404 no such sandbox.

Response `204`: no body.

## `POST /v1/sandbox/internal/browser-log/:sandboxId`

Internal: the browser shim's ingest. Always 204 -- a malformed id, body or entry is dropped silently -- except a body over BROWSER_LOG_MAX_BODY_BYTES, a 413 before parsing. Entries past BROWSER_LOG_RATE_LIMIT_PER_MIN per sandbox per minute collapse into one `[browser rate-limited: N events dropped …]` line.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["entries"],
  "description": "Parsed by hand: anything else is dropped, never refused",
  "properties": {
    "entries": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["level", "message"],
        "properties": {
          "level": {
            "enum": ["error", "warn", "log", "info", "debug"],
            "description": "Anything else drops the entry"
          },
          "ts": {
            "type": "number",
            "description": "Epoch milliseconds; missing or non-numeric is the runner's clock"
          },
          "message": {"type": "string", "description": "Folded onto one line"},
          "source": {"type": "string", "description": "Script URL or filename"},
          "line": {"type": "integer"},
          "col": {"type": "integer"},
          "stack": {"type": "string", "description": "Rendered indented under the line"}
        }
      }
    }
  }
}
```

Response `204`: no body.

## `GET /v1/sandbox/tool/openapi.json`

OpenWebUI fetches it to register the Tool Server.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "FastAPI's OpenAPI document for the /tool sub-app; its structure is the OpenAPI specification's, so it stays open",
  "required": ["openapi", "info", "paths"],
  "properties": {
    "openapi": {"type": "string", "description": "The OpenAPI version FastAPI emits"},
    "info": {
      "type": "object",
      "additionalProperties": true,
      "required": ["title", "version"],
      "properties": {
        "title": {"const": "sandbox-runner tools"},
        "version": {"type": "string"}
      }
    },
    "paths": {
      "type": "object",
      "additionalProperties": true,
      "required": ["/run", "/get_runtime_types"]
    }
  }
}
```

## `POST /v1/sandbox/tool/run`

`recreate_if_gone` is always true and the rewrite guard is off (`overwrite=true`): the Tool Server's only tool is `run`. Errors are JSON, not HTML; 422 a field validator's refusal (size caps, reserved `env` key, malformed `chat_id`, a negative `ttl_seconds`), 429 the pool is full, 500 a spawn error.

Request body (`application/json`):

```json
{
  "$comment": "generated from models.ToolRunRequest — do not edit",
  "description": "Request shape for the OpenWebUI Tool Server ``/tool/run`` route.\n\nKept separate from ``RunRequest`` because the Tool Server surface has\nslightly different defaults (no ``recreate_if_gone`` — the OpenWebUI\npath always self-heals) and doesn't need the same field descriptions.",
  "properties": {
    "runtime": {"description": "One of 'static', 'python', 'node'.", "type": "string"},
    "files": {
      "additionalProperties": {
        "anyOf": [
          {"type": "string"},
          {"additionalProperties": true, "type": "object"}
        ]
      },
      "description": "Map of relative path → content. Values are UTF-8 str or {encoding:'base64', content:'...'} for binary. On follow-up with the same session_id, only send changed files.",
      "type": "object"
    },
    "entrypoint": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "ttl_seconds": {
      "anyOf": [
        {"minimum": 0, "type": "integer"},
        {"type": "null"}
      ],
      "default": null
    },
    "session_id": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "deletes": {
      "items": {"type": "string"},
      "type": "array"
    },
    "env": {
      "anyOf": [
        {
          "additionalProperties": {"type": "string"},
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null
    },
    "chat_id": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Chat id. With no session_id, reuse the chat's existing sandbox instead of spawning a second one."
    }
  },
  "required": ["runtime"],
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`text/html`) — The preview page (`sandbox_mcp.render_preview_html`), with `Content-Disposition: inline`.

Response `400` (`application/json`) — an invalid `session_id` or unknown `runtime` (a plain string), or static lint:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "object",
          "description": "Every `.py` file in the call is compiled before Docker is touched; nothing was written or spawned",
          "additionalProperties": false,
          "required": ["error", "session_id", "errors", "hint"],
          "properties": {
            "error": {"const": "static lint failed"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "errors": {
              "type": "array",
              "minItems": 1,
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["path", "line", "offset", "message", "text"],
                "properties": {
                  "path": {"type": "string"},
                  "line": {"type": ["integer", "null"]},
                  "offset": {"type": ["integer", "null"]},
                  "message": {"type": "string", "description": "`<ErrorType>: <msg>`"},
                  "text": {"type": "string", "description": "The offending source line"}
                }
              }
            },
            "hint": {"type": "string"}
          }
        }
      ]
    }
  }
}
```

Response `409` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "The one-sandbox-per-chat index refused the spawn twice, and the winner could not be found",
      "additionalProperties": false,
      "required": ["error", "chat_id"],
      "properties": {
        "error": {"const": "could not reserve a sandbox for this chat — retry"},
        "chat_id": {"type": ["string", "null"]}
      }
    }
  }
}
```

Response `504` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detail"],
  "properties": {
    "detail": {
      "type": "object",
      "description": "The container never answered its readiness probe; it was torn down",
      "additionalProperties": false,
      "required": ["error", "session_id", "logs", "hint"],
      "properties": {
        "error": {"const": "sandbox did not become ready within 30s"},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "logs": {
          "type": "string",
          "description": "The container's /tmp/sandbox.log tail, read before teardown"
        },
        "hint": {"type": "string"}
      }
    }
  }
}
```

## `GET /v1/sandbox/tool/get_runtime_types`

Response `200` (`application/json`):

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "required": [
      "name",
      "summary",
      "default_entrypoint",
      "allows_custom_entrypoint",
      "prebaked_packages",
      "example_files"
    ],
    "properties": {
      "name": {
        "type": "string",
        "description": "A key of `runtimes.RUNTIMES` (`static`, `python`, `node`)"
      },
      "summary": {"type": "string"},
      "default_entrypoint": {
        "type": "string",
        "description": "Command run when the caller gives none; binds port 80"
      },
      "allows_custom_entrypoint": {
        "type": "boolean",
        "description": "False: an `entrypoint` is a 400 (`static`)"
      },
      "prebaked_packages": {
        "type": "array",
        "items": {"type": "string"}
      },
      "example_files": {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "A minimal `files` map for the runtime"
      }
    }
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
        "name": {"const": "sandbox"},
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
      "minItems": 12,
      "maxItems": 12,
      "items": {
        "type": "object",
        "additionalProperties": true,
        "$comment": "mcp.types.Tool: the SDK allows extra keys",
        "required": ["name", "inputSchema"],
        "properties": {
          "name": {
            "enum": [
              "get_runtime_types",
              "create",
              "write_files",
              "read_file",
              "edit_file",
              "get_files",
              "get_logs",
              "exec",
              "preview",
              "close",
              "list_sessions",
              "run"
            ]
          },
          "description": {
            "type": "string",
            "description": "The tool's docstring, or the description built from constants.py: what the model reads"
          },
          "inputSchema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema: the arguments of `MCP <name>`"
          },
          "_meta": {"type": "object", "additionalProperties": true, "description": "FastMCP's own"}
        }
      }
    },
    "nextCursor": {"type": "string"}
  }
}
```

## `MCP get_logs`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. Errors as `GET /session/:sandboxSessionId/logs`'s, as data.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:get_logs — do not edit",
  "additionalProperties": false,
  "properties": {
    "session_id": {
      "description": "The session_id from a previous create / run / write_files response. Must match ^[A-Za-z0-9_-]{1,64}$.",
      "type": "string"
    },
    "lines": {
      "default": 100,
      "description": "Trailing log lines to return. Clamped 1..1000. Default 100 is enough for most tracebacks.",
      "type": "integer"
    }
  },
  "required": ["session_id"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The logs, or an error",
  "oneOf": [
    {
      "type": "object",
      "allOf": [
        {
          "properties": {
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "sandbox_id": {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A sandbox's internal id: its job row's id, 12 hex characters"
            },
            "lines_requested": {
              "type": "integer",
              "description": "The `lines` asked for (the tail is clamped to 1..1000)"
            },
            "logs": {
              "type": "string",
              "description": "The tail of /tmp/sandbox.log: the app's stdout + stderr and `[browser]` lines; empty is not an error"
            }
          }
        }
      ],
      "properties": {
        "ok": {"const": true},
        "empty": {"const": true, "description": "Present when `logs` is empty"}
      },
      "required": ["ok", "session_id", "sandbox_id", "lines_requested", "logs"],
      "unevaluatedProperties": false
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    }
  ]
}
```

## `MCP get_runtime_types`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. Never fails.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:get_runtime_types — do not edit",
  "additionalProperties": false,
  "properties": {},
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["runtime_types"],
  "properties": {
    "runtime_types": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "name",
          "summary",
          "default_entrypoint",
          "allows_custom_entrypoint",
          "prebaked_packages",
          "example_files"
        ],
        "properties": {
          "name": {
            "type": "string",
            "description": "A key of `runtimes.RUNTIMES` (`static`, `python`, `node`)"
          },
          "summary": {"type": "string"},
          "default_entrypoint": {
            "type": "string",
            "description": "Command run when the caller gives none; binds port 80"
          },
          "allows_custom_entrypoint": {
            "type": "boolean",
            "description": "False: an `entrypoint` is a 400 (`static`)"
          },
          "prebaked_packages": {
            "type": "array",
            "items": {"type": "string"}
          },
          "example_files": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "A minimal `files` map for the runtime"
          }
        }
      }
    }
  }
}
```

## `MCP create`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. Every key `create` computes, unlike POST /create's `RunResponse`.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:create — do not edit",
  "additionalProperties": false,
  "properties": {
    "runtime": {
      "description": "Runtime for the empty warming container. One of the names in get_runtime_types.",
      "type": "string"
    },
    "ttl_seconds": {
      "anyOf": [
        {"type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Idle TTL in seconds. Omit, null or 0 for the default (SANDBOX_DEFAULT_TTL_SECONDS); a negative value is refused. Server clamps to SANDBOX_HARD_TTL_SECONDS."
    },
    "entrypoint": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Shell command bound to port 80. Leave unset for the runtime's default. Rejected for the 'static' runtime."
    },
    "env": {
      "anyOf": [
        {
          "additionalProperties": {"type": "string"},
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Process env vars set inside the container. IMMUTABLE after create — self-heal respawn replays the same env. Reserved keys (HTTP_PROXY, PYTHONUNBUFFERED, TERM, etc.) are rejected."
    },
    "chat_id": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Id of the chat you're answering in. Pass it and the runner enforces ONE sandbox per chat: if this chat already created one, create returns THAT session instead of spawning a duplicate (no extra slot used). Use the OpenWebUI chat id, or any stable per-conversation string. Strongly recommended — it stops a chat from leaking containers across turns when the session_id is lost."
    }
  },
  "required": ["runtime"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The session, or an error",
  "oneOf": [
    {
      "type": "object",
      "allOf": [
        {
          "description": "What `create` / `run` / `write_files` return (`operations._reuse_or_spawn`, `_do_create`, `_apply_files_to_running`)",
          "properties": {
            "sandbox_id": {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A sandbox's internal id: its job row's id, 12 hex characters"
            },
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "url": {
              "type": "string",
              "description": "`<SANDBOX_PROXY_URL>/<sandbox_id>/`: the iframe URL"
            },
            "expires_at": {
              "type": "string",
              "format": "date-time",
              "description": "ISO-8601 UTC: spawn time + the TTL (clamped to SANDBOX_HARD_TTL_SECONDS)"
            },
            "reused": {
              "type": "boolean",
              "description": "True: the session's running container was reused (no respawn)"
            },
            "runtime": {
              "type": ["string", "null"],
              "description": "The runtime the session was spawned under"
            },
            "startup_output": {
              "type": "string",
              "description": "A tail of /tmp/sandbox.log -- after a write, only what the dev server printed since this write"
            },
            "app_status": {
              "description": "On a fresh spawn (or a warming / existing sandbox) a placeholder `{code: 200, latency_ms: 0, note}` -- the readiness gate already passed; after a write, one live HTTP probe of the runtime's readiness path (3 s deadline): `{code, latency_ms}` for a real HTTP reply, `{error}` for a network failure",
              "oneOf": [
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["code", "latency_ms"],
                  "properties": {
                    "code": {"type": "integer", "description": "HTTP status the app answered"},
                    "latency_ms": {"type": "integer", "minimum": 0},
                    "note": {
                      "type": "string",
                      "description": "`readiness ok`, `warming`, or `existing sandbox for chat (phase=<phase>)`"
                    }
                  }
                },
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["error"],
                  "properties": {
                    "error": {
                      "type": "string",
                      "description": "`timeout after <s>s`, `connection refused (<reason>)`, or `<ExceptionType>: <message>`"
                    }
                  }
                }
              ]
            },
            "recreated": {
              "type": "boolean",
              "description": "True: the session's container was gone and was respawned (self-heal)"
            },
            "warming": {
              "type": "boolean",
              "description": "The container is still warming (`create`, or an existing sandbox still in `spawning` / `starting`)"
            },
            "duplicate_create_prevented": {
              "type": "boolean",
              "description": "True: `chat_id` matched a live sandbox, so this is that session, not a new spawn"
            },
            "chat_id": {"type": ["string", "null"], "description": "The `chat_id` the call carried"}
          }
        }
      ],
      "properties": {
        "ok": {"const": true}
      },
      "required": [
        "ok",
        "sandbox_id",
        "session_id",
        "url",
        "expires_at",
        "reused",
        "runtime",
        "startup_output",
        "app_status",
        "recreated",
        "warming",
        "duplicate_create_prevented",
        "chat_id"
      ],
      "unevaluatedProperties": false
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error with a structured `detail`, returned as a normal tool result",
      "additionalProperties": false,
      "required": ["ok", "status", "error", "session_id", "detail"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "`detail.error`"},
        "session_id": {"type": ["string", "null"], "description": "`detail.session_id`"},
        "detail": {
          "anyOf": [
            {
              "type": "object",
              "description": "MCP only: the arguments failed the pydantic model the HTTP route validates with (413 a size cap, 422 anything else)",
              "additionalProperties": false,
              "required": ["error", "tool", "message", "hint"],
              "properties": {
                "error": {"enum": ["payload too large", "invalid arguments"]},
                "tool": {"type": "string", "description": "The MCP tool"},
                "message": {
                  "type": "string",
                  "description": "The validator's messages, one per line"
                },
                "hint": {"type": "string"},
                "session_id": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The one-sandbox-per-chat index refused the spawn twice, and the winner could not be found",
              "additionalProperties": false,
              "required": ["error", "chat_id"],
              "properties": {
                "error": {"const": "could not reserve a sandbox for this chat — retry"},
                "chat_id": {"type": ["string", "null"]}
              }
            },
            {
              "type": "object",
              "description": "The container never answered its readiness probe; it was torn down",
              "additionalProperties": false,
              "required": ["error", "session_id", "logs", "hint"],
              "properties": {
                "error": {"const": "sandbox did not become ready within 30s"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "logs": {
                  "type": "string",
                  "description": "The container's /tmp/sandbox.log tail, read before teardown"
                },
                "hint": {"type": "string"}
              }
            }
          ]
        }
      }
    }
  ]
}
```

## `MCP write_files`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:write_files — do not edit",
  "additionalProperties": false,
  "properties": {
    "session_id": {
      "description": "The session_id from a previous create / run response.",
      "type": "string"
    },
    "files": {
      "additionalProperties": true,
      "description": "Path → FULL content of NEW files (or of files you are rewriting from scratch). Every listed file is replaced wholesale; unlisted files are preserved. Values are UTF-8 str, or {\"encoding\":\"base64\",\"content\":\"...\"} for binaries. Per-file cap: SANDBOX_MAX_FILE_BYTES; total cap: SANDBOX_MAX_PAYLOAD_BYTES. To change part of an existing file use edit_file instead.",
      "type": "object"
    },
    "deletes": {
      "description": "Relative paths under /app to remove. Ignored on fresh spawns.",
      "items": {"type": "string"},
      "type": "array"
    },
    "recreate_if_gone": {
      "default": false,
      "description": "OPT-IN self-heal. If false (default) and the container is gone, this returns an error telling you to explicitly opt in. If true, the runner respawns fresh — files installed via exec and in-container state are LOST; env is preserved.",
      "type": "boolean"
    },
    "overwrite": {
      "default": false,
      "description": "Set true ONLY to deliberately replace an existing text file of 30+ lines with content that keeps 50% or more of its lines. Without it that write is refused (rewrite_of_existing_file) — use edit_file for such changes.",
      "type": "boolean"
    }
  },
  "required": ["session_id"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The updated session, or an error",
  "oneOf": [
    {
      "type": "object",
      "allOf": [
        {
          "description": "What `create` / `run` / `write_files` return (`operations._reuse_or_spawn`, `_do_create`, `_apply_files_to_running`)",
          "properties": {
            "sandbox_id": {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A sandbox's internal id: its job row's id, 12 hex characters"
            },
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "url": {
              "type": "string",
              "description": "`<SANDBOX_PROXY_URL>/<sandbox_id>/`: the iframe URL"
            },
            "expires_at": {
              "type": "string",
              "format": "date-time",
              "description": "ISO-8601 UTC: spawn time + the TTL (clamped to SANDBOX_HARD_TTL_SECONDS)"
            },
            "reused": {
              "type": "boolean",
              "description": "True: the session's running container was reused (no respawn)"
            },
            "runtime": {
              "type": ["string", "null"],
              "description": "The runtime the session was spawned under"
            },
            "startup_output": {
              "type": "string",
              "description": "A tail of /tmp/sandbox.log -- after a write, only what the dev server printed since this write"
            },
            "app_status": {
              "description": "On a fresh spawn (or a warming / existing sandbox) a placeholder `{code: 200, latency_ms: 0, note}` -- the readiness gate already passed; after a write, one live HTTP probe of the runtime's readiness path (3 s deadline): `{code, latency_ms}` for a real HTTP reply, `{error}` for a network failure",
              "oneOf": [
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["code", "latency_ms"],
                  "properties": {
                    "code": {"type": "integer", "description": "HTTP status the app answered"},
                    "latency_ms": {"type": "integer", "minimum": 0},
                    "note": {
                      "type": "string",
                      "description": "`readiness ok`, `warming`, or `existing sandbox for chat (phase=<phase>)`"
                    }
                  }
                },
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["error"],
                  "properties": {
                    "error": {
                      "type": "string",
                      "description": "`timeout after <s>s`, `connection refused (<reason>)`, or `<ExceptionType>: <message>`"
                    }
                  }
                }
              ]
            },
            "recreated": {
              "type": "boolean",
              "description": "True: the session's container was gone and was respawned (self-heal)"
            },
            "warming": {
              "type": "boolean",
              "description": "The container is still warming (`create`, or an existing sandbox still in `spawning` / `starting`)"
            },
            "duplicate_create_prevented": {
              "type": "boolean",
              "description": "True: `chat_id` matched a live sandbox, so this is that session, not a new spawn"
            },
            "chat_id": {"type": ["string", "null"], "description": "The `chat_id` the call carried"}
          }
        }
      ],
      "properties": {
        "ok": {"const": true}
      },
      "required": [
        "ok",
        "sandbox_id",
        "session_id",
        "url",
        "expires_at",
        "reused",
        "runtime",
        "startup_output",
        "app_status",
        "recreated"
      ],
      "unevaluatedProperties": false
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error with a structured `detail`, returned as a normal tool result",
      "additionalProperties": false,
      "required": ["ok", "status", "error", "session_id", "detail"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "`detail.error`"},
        "session_id": {"type": ["string", "null"], "description": "`detail.session_id`"},
        "detail": {
          "anyOf": [
            {
              "type": "object",
              "description": "MCP only: the arguments failed the pydantic model the HTTP route validates with (413 a size cap, 422 anything else)",
              "additionalProperties": false,
              "required": ["error", "tool", "message", "hint"],
              "properties": {
                "error": {"enum": ["payload too large", "invalid arguments"]},
                "tool": {"type": "string", "description": "The MCP tool"},
                "message": {
                  "type": "string",
                  "description": "The validator's messages, one per line"
                },
                "hint": {"type": "string"},
                "session_id": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "Every `.py` file in the call is compiled before Docker is touched; nothing was written or spawned",
              "additionalProperties": false,
              "required": ["error", "session_id", "errors", "hint"],
              "properties": {
                "error": {"const": "static lint failed"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "errors": {
                  "type": "array",
                  "minItems": 1,
                  "items": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["path", "line", "offset", "message", "text"],
                    "properties": {
                      "path": {"type": "string"},
                      "line": {"type": ["integer", "null"]},
                      "offset": {"type": ["integer", "null"]},
                      "message": {"type": "string", "description": "`<ErrorType>: <msg>`"},
                      "text": {"type": "string", "description": "The offending source line"}
                    }
                  }
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "No running sandbox for this `session_id`",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "session not found"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The registry says running but the container is gone, and the call does not respawn",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "sandbox container is gone"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The rewrite guard: a listed path already exists as a text file of 30+ lines (WRITE_GUARD_MIN_LINES) and keeps >= 50 % of its lines (WRITE_GUARD_SIMILARITY); no file of the call was written",
              "additionalProperties": false,
              "required": ["error", "kind", "tool", "session_id", "files", "message", "hint"],
              "properties": {
                "error": {"const": "rewrite of existing file"},
                "kind": {"const": "rewrite_of_existing_file"},
                "tool": {"enum": ["write_files", "run"]},
                "session_id": {"type": "string"},
                "files": {
                  "type": "array",
                  "minItems": 1,
                  "items": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": [
                      "path",
                      "similarity",
                      "changed_lines",
                      "old_lines",
                      "new_lines",
                      "identical"
                    ],
                    "properties": {
                      "path": {"type": "string"},
                      "similarity": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "Line similarity of the new content to the existing file"
                      },
                      "changed_lines": {"type": "integer", "minimum": 0},
                      "old_lines": {"type": "integer", "minimum": 0},
                      "new_lines": {"type": "integer", "minimum": 0},
                      "identical": {"type": "boolean"}
                    }
                  }
                },
                "message": {"type": "string"},
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "`recreate_if_gone` on a session whose job row records no known runtime",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "cannot self-heal: runtime metadata missing"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The one-sandbox-per-chat index refused the spawn twice, and the winner could not be found",
              "additionalProperties": false,
              "required": ["error", "chat_id"],
              "properties": {
                "error": {"const": "could not reserve a sandbox for this chat — retry"},
                "chat_id": {"type": ["string", "null"]}
              }
            },
            {
              "type": "object",
              "description": "The container never answered its readiness probe; it was torn down",
              "additionalProperties": false,
              "required": ["error", "session_id", "logs", "hint"],
              "properties": {
                "error": {"const": "sandbox did not become ready within 30s"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "logs": {
                  "type": "string",
                  "description": "The container's /tmp/sandbox.log tail, read before teardown"
                },
                "hint": {"type": "string"}
              }
            }
          ]
        }
      }
    }
  ]
}
```

## `MCP read_file`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. The numbered lines are in the text only.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:read_file — do not edit",
  "additionalProperties": false,
  "properties": {
    "session_id": {"description": "The session_id returned by create / run.", "type": "string"},
    "path": {"description": "Relative path of ONE text file under /app.", "type": "string"},
    "offset": {
      "default": 1,
      "description": "1-based line number to start reading at.",
      "type": "integer"
    },
    "limit": {
      "default": 400,
      "description": "Number of lines to return (max 2000).",
      "type": "integer"
    }
  },
  "required": ["session_id", "path"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The window's bounds, or an error",
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "ok",
        "session_id",
        "sandbox_id",
        "path",
        "start_line",
        "end_line",
        "total_lines",
        "truncated"
      ],
      "properties": {
        "ok": {"const": true},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "sandbox_id": {
          "type": "string",
          "pattern": "^[0-9a-f]{12}$",
          "description": "A sandbox's internal id: its job row's id, 12 hex characters"
        },
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 0},
        "total_lines": {"type": "integer", "minimum": 0},
        "truncated": {"type": "boolean"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error with a structured `detail`, returned as a normal tool result",
      "additionalProperties": false,
      "required": ["ok", "status", "error", "session_id", "detail"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "`detail.error`"},
        "session_id": {"type": ["string", "null"], "description": "`detail.session_id`"},
        "detail": {
          "anyOf": [
            {
              "type": "object",
              "description": "No running sandbox for this `session_id`",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "session not found"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The registry says running but the container is gone, and the call does not respawn",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "sandbox container is gone"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
              "additionalProperties": false,
              "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
              "properties": {
                "error": {
                  "enum": [
                    "unsafe path",
                    "file does not exist",
                    "path is a directory",
                    "not a regular file",
                    "not a UTF-8 text file",
                    "file too large",
                    "old_string is empty",
                    "old_string equals new_string",
                    "old_string not found",
                    "old_string contains read_file line numbers",
                    "old_string matches more than once"
                  ]
                },
                "kind": {
                  "enum": [
                    "unsafe_path",
                    "missing_file",
                    "is_directory",
                    "not_a_file",
                    "binary_file",
                    "too_large",
                    "empty_old_string",
                    "no_change",
                    "not_found",
                    "line_number_prefix",
                    "multiple_matches"
                  ]
                },
                "tool": {"enum": ["read_file", "edit_file"]},
                "session_id": {"type": "string"},
                "path": {"type": "string"},
                "message": {"type": "string"},
                "hint": {"type": "string", "description": "The exact next action"},
                "closest_start_line": {
                  "type": "integer",
                  "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
                },
                "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
                "similarity": {
                  "type": "number",
                  "description": "not_found: how close it is (2 decimals)"
                },
                "closest_text": {
                  "type": "string",
                  "description": "not_found: the closest region, numbered like read_file"
                },
                "whitespace_only": {
                  "type": "boolean",
                  "description": "not_found: it differs only in whitespace / indentation"
                },
                "matches_without_prefixes": {
                  "type": "integer",
                  "description": "line_number_prefix: how often old_string matches with the prefixes removed"
                },
                "count": {
                  "type": "integer",
                  "description": "multiple_matches: how often old_string occurs"
                },
                "match_lines": {
                  "type": "array",
                  "items": {"type": "integer"},
                  "description": "multiple_matches: the first line of each match (up to 20)"
                }
              },
              "allOf": [
                {
                  "if": {
                    "properties": {
                      "kind": {"const": "line_number_prefix"}
                    }
                  },
                  "then": {"required": ["matches_without_prefixes"]}
                },
                {
                  "if": {
                    "properties": {
                      "kind": {"const": "multiple_matches"}
                    }
                  },
                  "then": {"required": ["count", "match_lines"]}
                }
              ]
            }
          ]
        }
      }
    }
  ]
}
```

## `MCP edit_file`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. The snippet is in the text only.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:edit_file — do not edit",
  "additionalProperties": false,
  "properties": {
    "session_id": {"description": "The session_id returned by create / run.", "type": "string"},
    "path": {"description": "Relative path under /app of an EXISTING text file.", "type": "string"},
    "old_string": {
      "description": "The exact text to replace, copied from read_file output WITHOUT the line-number prefix. Include enough lines to make it unique in the file.",
      "type": "string"
    },
    "new_string": {"description": "The text to put in its place (must differ).", "type": "string"},
    "replace_all": {
      "default": false,
      "description": "Replace every occurrence of old_string (e.g. renaming a variable). Default: old_string must occur exactly once.",
      "type": "boolean"
    }
  },
  "required": ["session_id", "path", "old_string", "new_string"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The edit, or an error",
  "oneOf": [
    {
      "type": "object",
      "allOf": [
        {
          "description": "What `edit_file` returns (`operations._do_edit_file`)",
          "properties": {
            "sandbox_id": {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A sandbox's internal id: its job row's id, 12 hex characters"
            },
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "url": {"type": "string"},
            "expires_at": {"type": "string", "format": "date-time"},
            "runtime": {"type": ["string", "null"]},
            "path": {"type": "string"},
            "replacements": {"type": "integer", "minimum": 1},
            "start_line": {
              "type": "integer",
              "description": "The (first) replaced region in the NEW file"
            },
            "end_line": {"type": "integer"},
            "match_lines": {
              "type": "array",
              "items": {"type": "integer"},
              "description": "The first line of every replacement (up to 20)"
            },
            "total_lines": {"type": "integer", "minimum": 0},
            "startup_output": {
              "type": "string",
              "description": "What the dev server printed after this edit"
            },
            "app_status": {
              "description": "On a fresh spawn (or a warming / existing sandbox) a placeholder `{code: 200, latency_ms: 0, note}` -- the readiness gate already passed; after a write, one live HTTP probe of the runtime's readiness path (3 s deadline): `{code, latency_ms}` for a real HTTP reply, `{error}` for a network failure",
              "oneOf": [
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["code", "latency_ms"],
                  "properties": {
                    "code": {"type": "integer", "description": "HTTP status the app answered"},
                    "latency_ms": {"type": "integer", "minimum": 0},
                    "note": {
                      "type": "string",
                      "description": "`readiness ok`, `warming`, or `existing sandbox for chat (phase=<phase>)`"
                    }
                  }
                },
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["error"],
                  "properties": {
                    "error": {
                      "type": "string",
                      "description": "`timeout after <s>s`, `connection refused (<reason>)`, or `<ExceptionType>: <message>`"
                    }
                  }
                }
              ]
            },
            "recreated": {"const": false}
          }
        }
      ],
      "properties": {
        "ok": {"const": true}
      },
      "required": [
        "ok",
        "sandbox_id",
        "session_id",
        "url",
        "expires_at",
        "runtime",
        "path",
        "replacements",
        "start_line",
        "end_line",
        "match_lines",
        "total_lines",
        "startup_output",
        "app_status",
        "recreated"
      ],
      "unevaluatedProperties": false
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error with a structured `detail`, returned as a normal tool result",
      "additionalProperties": false,
      "required": ["ok", "status", "error", "session_id", "detail"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "`detail.error`"},
        "session_id": {"type": ["string", "null"], "description": "`detail.session_id`"},
        "detail": {
          "anyOf": [
            {
              "type": "object",
              "description": "MCP only: the arguments failed the pydantic model the HTTP route validates with (413 a size cap, 422 anything else)",
              "additionalProperties": false,
              "required": ["error", "tool", "message", "hint"],
              "properties": {
                "error": {"enum": ["payload too large", "invalid arguments"]},
                "tool": {"type": "string", "description": "The MCP tool"},
                "message": {
                  "type": "string",
                  "description": "The validator's messages, one per line"
                },
                "hint": {"type": "string"},
                "session_id": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "No running sandbox for this `session_id`",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "session not found"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The registry says running but the container is gone, and the call does not respawn",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "sandbox container is gone"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "A refused read_file / edit_file (`common.textops.FileOpError`); nothing was modified",
              "additionalProperties": false,
              "required": ["error", "kind", "tool", "session_id", "path", "message", "hint"],
              "properties": {
                "error": {
                  "enum": [
                    "unsafe path",
                    "file does not exist",
                    "path is a directory",
                    "not a regular file",
                    "not a UTF-8 text file",
                    "file too large",
                    "old_string is empty",
                    "old_string equals new_string",
                    "old_string not found",
                    "old_string contains read_file line numbers",
                    "old_string matches more than once"
                  ]
                },
                "kind": {
                  "enum": [
                    "unsafe_path",
                    "missing_file",
                    "is_directory",
                    "not_a_file",
                    "binary_file",
                    "too_large",
                    "empty_old_string",
                    "no_change",
                    "not_found",
                    "line_number_prefix",
                    "multiple_matches"
                  ]
                },
                "tool": {"enum": ["read_file", "edit_file"]},
                "session_id": {"type": "string"},
                "path": {"type": "string"},
                "message": {"type": "string"},
                "hint": {"type": "string", "description": "The exact next action"},
                "closest_start_line": {
                  "type": "integer",
                  "description": "not_found: first line of the closest region (only when one scores >= 0.3)"
                },
                "closest_end_line": {"type": "integer", "description": "not_found: its last line"},
                "similarity": {
                  "type": "number",
                  "description": "not_found: how close it is (2 decimals)"
                },
                "closest_text": {
                  "type": "string",
                  "description": "not_found: the closest region, numbered like read_file"
                },
                "whitespace_only": {
                  "type": "boolean",
                  "description": "not_found: it differs only in whitespace / indentation"
                },
                "matches_without_prefixes": {
                  "type": "integer",
                  "description": "line_number_prefix: how often old_string matches with the prefixes removed"
                },
                "count": {
                  "type": "integer",
                  "description": "multiple_matches: how often old_string occurs"
                },
                "match_lines": {
                  "type": "array",
                  "items": {"type": "integer"},
                  "description": "multiple_matches: the first line of each match (up to 20)"
                }
              },
              "allOf": [
                {
                  "if": {
                    "properties": {
                      "kind": {"const": "line_number_prefix"}
                    }
                  },
                  "then": {"required": ["matches_without_prefixes"]}
                },
                {
                  "if": {
                    "properties": {
                      "kind": {"const": "multiple_matches"}
                    }
                  },
                  "then": {"required": ["count", "match_lines"]}
                }
              ]
            },
            {
              "type": "object",
              "description": "Every `.py` file in the call is compiled before Docker is touched; nothing was written or spawned",
              "additionalProperties": false,
              "required": ["error", "session_id", "errors", "hint"],
              "properties": {
                "error": {"const": "static lint failed"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "errors": {
                  "type": "array",
                  "minItems": 1,
                  "items": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["path", "line", "offset", "message", "text"],
                    "properties": {
                      "path": {"type": "string"},
                      "line": {"type": ["integer", "null"]},
                      "offset": {"type": ["integer", "null"]},
                      "message": {"type": "string", "description": "`<ErrorType>: <msg>`"},
                      "text": {"type": "string", "description": "The offending source line"}
                    }
                  }
                },
                "hint": {"type": "string"}
              }
            }
          ]
        }
      }
    }
  ]
}
```

## `MCP get_files`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. With `paths`, the contents are in the text only.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:get_files — do not edit",
  "additionalProperties": false,
  "properties": {
    "session_id": {"description": "The session_id returned by create / run.", "type": "string"},
    "paths": {
      "anyOf": [
        {
          "items": {"type": "string"},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Relative paths under /app. Omit to get a directory listing (paths + sizes only, no contents)."
    },
    "max_bytes_per_file": {
      "default": 8192,
      "description": "Truncate each file to this many bytes. Hard cap 65536.",
      "type": "integer"
    }
  },
  "required": ["session_id"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The listing or the files' metadata, or an error",
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["ok", "session_id", "sandbox_id", "files"],
      "properties": {
        "ok": {"const": true},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "sandbox_id": {
          "type": "string",
          "pattern": "^[0-9a-f]{12}$",
          "description": "A sandbox's internal id: its job row's id, 12 hex characters"
        },
        "files": {
          "type": "array",
          "items": {
            "anyOf": [
              {
                "type": "object",
                "description": "A regular file under /app (no `paths`: a listing, no contents)",
                "additionalProperties": false,
                "required": ["path", "size"],
                "properties": {
                  "path": {"type": "string", "description": "Relative to /app"},
                  "size": {"type": "integer", "minimum": 0}
                }
              },
              {
                "type": "object",
                "description": "A `file_entry` without `content` (the contents are in the tool's text)",
                "additionalProperties": false,
                "required": ["path", "size", "encoding", "truncated"],
                "properties": {
                  "path": {"type": "string"},
                  "size": {"type": "integer", "minimum": 0},
                  "encoding": {"enum": ["utf-8", "base64"]},
                  "truncated": {"type": "boolean"},
                  "error": {"type": "string"}
                }
              }
            ]
          }
        }
      }
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error with a structured `detail`, returned as a normal tool result",
      "additionalProperties": false,
      "required": ["ok", "status", "error", "session_id", "detail"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "`detail.error`"},
        "session_id": {"type": ["string", "null"], "description": "`detail.session_id`"},
        "detail": {
          "type": "object",
          "description": "The registry says running but the container is gone, and the call does not respawn",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "sandbox container is gone"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        }
      }
    }
  ]
}
```

## `MCP exec`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. The command and its output are in the text only.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:exec — do not edit",
  "additionalProperties": false,
  "properties": {
    "session_id": {"description": "The target session.", "type": "string"},
    "command": {
      "description": "Shell command (via `sh -c`). NON-INTERACTIVE — stdin is closed, so anything that reads input hangs until timeout. Use -y / non-interactive flags.",
      "type": "string"
    },
    "timeout_seconds": {
      "default": 30,
      "description": "Command timeout in seconds. Hard cap 120.",
      "type": "integer"
    },
    "working_dir": {
      "default": "/app",
      "description": "Working dir inside the container. Must be under /app; absolute paths outside /app and '..' traversal are rejected.",
      "type": "string"
    }
  },
  "required": ["session_id", "command"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The exit status, or an error",
  "oneOf": [
    {
      "type": "object",
      "allOf": [
        {
          "properties": {
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "sandbox_id": {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A sandbox's internal id: its job row's id, 12 hex characters"
            },
            "exit_code": {
              "type": "integer",
              "description": "-1 when the command timed out or the exec failed"
            },
            "duration_ms": {"type": "integer", "minimum": 0},
            "truncated": {
              "type": "boolean",
              "description": "Earlier output was dropped (the LAST EXEC_MAX_OUTPUT_BYTES are kept)"
            },
            "timed_out": {"type": "boolean"}
          }
        }
      ],
      "properties": {
        "ok": {"const": true}
      },
      "required": [
        "ok",
        "session_id",
        "sandbox_id",
        "exit_code",
        "duration_ms",
        "truncated",
        "timed_out"
      ],
      "unevaluatedProperties": false
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error with a structured `detail`, returned as a normal tool result",
      "additionalProperties": false,
      "required": ["ok", "status", "error", "session_id", "detail"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "`detail.error`"},
        "session_id": {"type": ["string", "null"], "description": "`detail.session_id`"},
        "detail": {
          "anyOf": [
            {
              "type": "object",
              "description": "exec / preview: no running sandbox for this `session_id`",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "no running sandbox for this session"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The registry says running but the container is gone, and the call does not respawn",
              "additionalProperties": false,
              "required": ["error", "session_id", "hint"],
              "properties": {
                "error": {"const": "sandbox container is gone"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "hint": {"type": "string"}
              }
            }
          ]
        }
      }
    }
  ]
}
```

## `MCP preview`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. The text ends with the fenced ```html block the model must relay verbatim.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:preview — do not edit",
  "additionalProperties": false,
  "properties": {
    "session_id": {"description": "The session_id whose preview iframe you want.", "type": "string"}
  },
  "required": ["session_id"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The preview, or an error",
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "ok",
        "session_id",
        "sandbox_id",
        "url",
        "expires_at",
        "runtime",
        "download_url",
        "iframe_html"
      ],
      "properties": {
        "ok": {"const": true},
        "session_id": {
          "type": "string",
          "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
        },
        "sandbox_id": {
          "type": "string",
          "pattern": "^[0-9a-f]{12}$",
          "description": "A sandbox's internal id: its job row's id, 12 hex characters"
        },
        "url": {"type": "string"},
        "expires_at": {"type": "string", "format": "date-time"},
        "runtime": {"type": ["string", "null"]},
        "download_url": {
          "type": "string",
          "description": "`<SANDBOX_PROXY_URL>/download/<session_id>`"
        },
        "iframe_html": {
          "type": "string",
          "description": "The preview page the fenced block carries"
        }
      }
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error with a structured `detail`, returned as a normal tool result",
      "additionalProperties": false,
      "required": ["ok", "status", "error", "session_id", "detail"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "`detail.error`"},
        "session_id": {"type": ["string", "null"], "description": "`detail.session_id`"},
        "detail": {
          "type": "object",
          "description": "exec / preview: no running sandbox for this `session_id`",
          "additionalProperties": false,
          "required": ["error", "session_id", "hint"],
          "properties": {
            "error": {"const": "no running sandbox for this session"},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "hint": {"type": "string"}
          }
        }
      }
    }
  ]
}
```

## `MCP close`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. Idempotent.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:close — do not edit",
  "additionalProperties": false,
  "properties": {
    "session_id": {"description": "The session_id to tear down.", "type": "string"}
  },
  "required": ["session_id"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The teardown, or an error",
  "oneOf": [
    {
      "oneOf": [
        {
          "type": "object",
          "description": "Torn down, slot released",
          "additionalProperties": false,
          "required": ["ok", "session_id", "sandbox_id", "was_running"],
          "properties": {
            "ok": {"const": true},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "sandbox_id": {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A sandbox's internal id: its job row's id, 12 hex characters"
            },
            "was_running": {"const": true}
          }
        },
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["ok", "session_id", "was_running", "note"],
          "properties": {
            "ok": {"const": true},
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "was_running": {"const": false},
            "note": {"const": "already closed or never existed"}
          }
        }
      ]
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    }
  ]
}
```

## `MCP list_sessions`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:list_sessions — do not edit",
  "additionalProperties": false,
  "properties": {},
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["ok", "count", "sessions"],
  "properties": {
    "ok": {"const": true},
    "count": {"type": "integer", "minimum": 0},
    "sessions": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One running sandbox, read back from its job row",
        "additionalProperties": false,
        "required": [
          "session_id",
          "sandbox_id",
          "runtime",
          "url",
          "created_at",
          "updated_at",
          "expires_at",
          "last_used_at",
          "phase"
        ],
        "properties": {
          "session_id": {
            "type": "string",
            "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
          },
          "sandbox_id": {
            "type": "string",
            "pattern": "^[0-9a-f]{12}$",
            "description": "A sandbox's internal id: its job row's id, 12 hex characters"
          },
          "runtime": {"type": "string"},
          "url": {"type": "string"},
          "created_at": {"type": "string", "format": "date-time"},
          "updated_at": {"type": "string", "format": "date-time"},
          "expires_at": {"type": "string", "format": "date-time"},
          "last_used_at": {
            "type": "string",
            "format": "date-time",
            "description": "Bumped by every write / read_file / edit_file / exec"
          },
          "phase": {"const": "running"}
        }
      }
    }
  }
}
```

## `MCP run`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` is the prose the model reads. The text ends with the fenced ```html block the model must relay verbatim. `recreate_if_gone` is always true.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app._mcp:run — do not edit",
  "additionalProperties": false,
  "properties": {
    "runtime": {
      "description": "Runtime for the sandbox. One of the names in get_runtime_types.",
      "type": "string"
    },
    "files": {
      "additionalProperties": true,
      "description": "Path → content map. First call (no session_id): full initial file set. Follow-up (with session_id): overlay — only NEW files; change existing ones with edit_file. Values are UTF-8 str or {encoding:'base64', content:'...'} for binaries.",
      "type": "object"
    },
    "entrypoint": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Shell command inside the sandbox, must bind port 80. Leave unset for the runtime's default. Rejected for runtimes with allows_custom_entrypoint=false. Ignored on follow-up calls."
    },
    "ttl_seconds": {
      "anyOf": [
        {"type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Idle TTL in seconds. Omit, null or 0 for the default (SANDBOX_DEFAULT_TTL_SECONDS); a negative value is refused. Server clamps to SANDBOX_HARD_TTL_SECONDS."
    },
    "session_id": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Handle for a persistent preview. OMIT on the first call, PASS on follow-ups to update in place."
    },
    "deletes": {
      "description": "Relative paths under /app to remove on a follow-up call.",
      "items": {"type": "string"},
      "type": "array"
    },
    "env": {
      "anyOf": [
        {
          "additionalProperties": {"type": "string"},
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Process env vars. Applied only on the first call. Reserved keys (HTTP_PROXY, PYTHONUNBUFFERED, etc.) rejected."
    },
    "chat_id": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Id of the chat you're answering in. With no session_id, the runner reuses this chat's existing container instead of spawning a new one — so repeated run calls in one chat update the same preview even if you lost the session_id. Pass it on every run for the chat."
    },
    "overwrite": {
      "default": false,
      "description": "Follow-up calls only. Set true ONLY to deliberately replace an existing text file of 30+ lines with content that keeps 50% or more of its lines. Without it that write is refused (rewrite_of_existing_file) — use edit_file for such changes.",
      "type": "boolean"
    }
  },
  "required": ["runtime"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The session and its preview, or an error",
  "oneOf": [
    {
      "type": "object",
      "allOf": [
        {
          "description": "What `create` / `run` / `write_files` return (`operations._reuse_or_spawn`, `_do_create`, `_apply_files_to_running`)",
          "properties": {
            "sandbox_id": {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A sandbox's internal id: its job row's id, 12 hex characters"
            },
            "session_id": {
              "type": "string",
              "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
            },
            "url": {
              "type": "string",
              "description": "`<SANDBOX_PROXY_URL>/<sandbox_id>/`: the iframe URL"
            },
            "expires_at": {
              "type": "string",
              "format": "date-time",
              "description": "ISO-8601 UTC: spawn time + the TTL (clamped to SANDBOX_HARD_TTL_SECONDS)"
            },
            "reused": {
              "type": "boolean",
              "description": "True: the session's running container was reused (no respawn)"
            },
            "runtime": {
              "type": ["string", "null"],
              "description": "The runtime the session was spawned under"
            },
            "startup_output": {
              "type": "string",
              "description": "A tail of /tmp/sandbox.log -- after a write, only what the dev server printed since this write"
            },
            "app_status": {
              "description": "On a fresh spawn (or a warming / existing sandbox) a placeholder `{code: 200, latency_ms: 0, note}` -- the readiness gate already passed; after a write, one live HTTP probe of the runtime's readiness path (3 s deadline): `{code, latency_ms}` for a real HTTP reply, `{error}` for a network failure",
              "oneOf": [
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["code", "latency_ms"],
                  "properties": {
                    "code": {"type": "integer", "description": "HTTP status the app answered"},
                    "latency_ms": {"type": "integer", "minimum": 0},
                    "note": {
                      "type": "string",
                      "description": "`readiness ok`, `warming`, or `existing sandbox for chat (phase=<phase>)`"
                    }
                  }
                },
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["error"],
                  "properties": {
                    "error": {
                      "type": "string",
                      "description": "`timeout after <s>s`, `connection refused (<reason>)`, or `<ExceptionType>: <message>`"
                    }
                  }
                }
              ]
            },
            "recreated": {
              "type": "boolean",
              "description": "True: the session's container was gone and was respawned (self-heal)"
            },
            "warming": {
              "type": "boolean",
              "description": "The container is still warming (`create`, or an existing sandbox still in `spawning` / `starting`)"
            },
            "duplicate_create_prevented": {
              "type": "boolean",
              "description": "True: `chat_id` matched a live sandbox, so this is that session, not a new spawn"
            },
            "chat_id": {"type": ["string", "null"], "description": "The `chat_id` the call carried"}
          }
        }
      ],
      "properties": {
        "ok": {"const": true},
        "download_url": {
          "type": "string",
          "description": "`<SANDBOX_PROXY_URL>/download/<session_id>`"
        },
        "iframe_html": {
          "type": "string",
          "description": "The preview page the fenced block carries"
        }
      },
      "required": [
        "ok",
        "sandbox_id",
        "session_id",
        "url",
        "expires_at",
        "reused",
        "runtime",
        "startup_output",
        "app_status",
        "recreated",
        "download_url",
        "iframe_html"
      ],
      "unevaluatedProperties": false
    },
    {
      "type": "object",
      "description": "An HTTP error of the operation whose `detail` was a string, returned as a normal tool result (never a JSON-RPC error)",
      "additionalProperties": false,
      "required": ["ok", "status", "error"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "The plain detail"}
      }
    },
    {
      "type": "object",
      "description": "An HTTP error with a structured `detail`, returned as a normal tool result",
      "additionalProperties": false,
      "required": ["ok", "status", "error", "session_id", "detail"],
      "properties": {
        "ok": {"const": false},
        "status": {"type": "integer", "description": "The HTTP status the operation raised"},
        "error": {"type": "string", "description": "`detail.error`"},
        "session_id": {"type": ["string", "null"], "description": "`detail.session_id`"},
        "detail": {
          "anyOf": [
            {
              "type": "object",
              "description": "MCP only: the arguments failed the pydantic model the HTTP route validates with (413 a size cap, 422 anything else)",
              "additionalProperties": false,
              "required": ["error", "tool", "message", "hint"],
              "properties": {
                "error": {"enum": ["payload too large", "invalid arguments"]},
                "tool": {"type": "string", "description": "The MCP tool"},
                "message": {
                  "type": "string",
                  "description": "The validator's messages, one per line"
                },
                "hint": {"type": "string"},
                "session_id": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "Every `.py` file in the call is compiled before Docker is touched; nothing was written or spawned",
              "additionalProperties": false,
              "required": ["error", "session_id", "errors", "hint"],
              "properties": {
                "error": {"const": "static lint failed"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "errors": {
                  "type": "array",
                  "minItems": 1,
                  "items": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["path", "line", "offset", "message", "text"],
                    "properties": {
                      "path": {"type": "string"},
                      "line": {"type": ["integer", "null"]},
                      "offset": {"type": ["integer", "null"]},
                      "message": {"type": "string", "description": "`<ErrorType>: <msg>`"},
                      "text": {"type": "string", "description": "The offending source line"}
                    }
                  }
                },
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The rewrite guard: a listed path already exists as a text file of 30+ lines (WRITE_GUARD_MIN_LINES) and keeps >= 50 % of its lines (WRITE_GUARD_SIMILARITY); no file of the call was written",
              "additionalProperties": false,
              "required": ["error", "kind", "tool", "session_id", "files", "message", "hint"],
              "properties": {
                "error": {"const": "rewrite of existing file"},
                "kind": {"const": "rewrite_of_existing_file"},
                "tool": {"enum": ["write_files", "run"]},
                "session_id": {"type": "string"},
                "files": {
                  "type": "array",
                  "minItems": 1,
                  "items": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": [
                      "path",
                      "similarity",
                      "changed_lines",
                      "old_lines",
                      "new_lines",
                      "identical"
                    ],
                    "properties": {
                      "path": {"type": "string"},
                      "similarity": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "Line similarity of the new content to the existing file"
                      },
                      "changed_lines": {"type": "integer", "minimum": 0},
                      "old_lines": {"type": "integer", "minimum": 0},
                      "new_lines": {"type": "integer", "minimum": 0},
                      "identical": {"type": "boolean"}
                    }
                  }
                },
                "message": {"type": "string"},
                "hint": {"type": "string"}
              }
            },
            {
              "type": "object",
              "description": "The one-sandbox-per-chat index refused the spawn twice, and the winner could not be found",
              "additionalProperties": false,
              "required": ["error", "chat_id"],
              "properties": {
                "error": {"const": "could not reserve a sandbox for this chat — retry"},
                "chat_id": {"type": ["string", "null"]}
              }
            },
            {
              "type": "object",
              "description": "The container never answered its readiness probe; it was torn down",
              "additionalProperties": false,
              "required": ["error", "session_id", "logs", "hint"],
              "properties": {
                "error": {"const": "sandbox did not become ready within 30s"},
                "session_id": {
                  "type": "string",
                  "description": "Persistent handle across turns (`^[A-Za-z0-9_-]{1,64}$`; a generated one is 12 URL-safe characters)"
                },
                "logs": {
                  "type": "string",
                  "description": "The container's /tmp/sandbox.log tail, read before teardown"
                },
                "hint": {"type": "string"}
              }
            }
          ]
        }
      }
    }
  ]
}
```
