# open-terminal request and response schemas

Generated from [`open-terminal.schemas.json`](open-terminal.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`OPEN_TERMINAL.md`](OPEN_TERMINAL.md).

## `GET /health`

No authentication (no API key dependency).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["status"],
  "properties": {
    "status": {"const": "ok"}
  }
}
```

## `GET /openapi.json`

No authentication (FastAPI serves it without the routes' API-key dependency). Lists only the routes without `include_in_schema=False` -- on the box 16 paths: `/info`, the file read / write / search tools, `/execute*` and `/api/terminals/user/*` -- which Open WebUI turns into the model's tools.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "OpenAPI 3.1 document",
  "required": ["openapi", "info", "paths"],
  "properties": {
    "openapi": {"type": "string", "description": "`3.1.0`"},
    "info": {
      "type": "object",
      "additionalProperties": true,
      "required": ["title", "version"],
      "properties": {
        "title": {"const": "Open Terminal"},
        "version": {"type": "string"}
      }
    },
    "paths": {"type": "object", "additionalProperties": true},
    "components": {"type": "object", "additionalProperties": true}
  }
}
```

## `GET /api/config`

No authentication (no API key dependency).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["features"],
  "properties": {
    "features": {
      "type": "object",
      "additionalProperties": true,
      "required": ["terminal", "notebooks", "system"],
      "properties": {
        "terminal": {"type": "boolean", "description": "OPEN_TERMINAL_ENABLE_TERMINAL"},
        "notebooks": {"type": "boolean", "description": "OPEN_TERMINAL_ENABLE_NOTEBOOKS"},
        "system": {"type": "boolean", "description": "OPEN_TERMINAL_ENABLE_SYSTEM_PROMPT"}
      }
    }
  }
}
```

## `GET /system`

Mounted only with OPEN_TERMINAL_ENABLE_SYSTEM_PROMPT (else 404).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["prompt"],
  "properties": {
    "prompt": {
      "type": "string",
      "description": "OS, kernel, architecture, host, user, shell and Python version, then OPEN_TERMINAL_INFO; or OPEN_TERMINAL_SYSTEM_PROMPT with its `{{...}}` variables filled"
    }
  }
}
```

## `GET /info`

Mounted only when OPEN_TERMINAL_INFO is non-empty (else 404). The model's `get_info` tool.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["info"],
  "properties": {
    "info": {"type": "string", "description": "OPEN_TERMINAL_INFO, verbatim"}
  }
}
```

## `GET /ports`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["ports"],
  "properties": {
    "ports": {
      "type": "array",
      "description": "TCP ports listening in the container that the caller owns: X-User-Id's account in multi-user mode, else the server's descendant processes",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["port", "pid", "process"],
        "properties": {
          "port": {"type": "integer"},
          "pid": {"type": ["integer", "null"]},
          "process": {"type": ["string", "null"], "description": "The process name"}
        }
      }
    }
  }
}
```

## `GET /files/cwd`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["cwd", "home"],
  "properties": {
    "cwd": {"type": "string", "description": "X-Session-Id's working directory, else the home"},
    "home": {"type": "string"},
    "root": {
      "type": "object",
      "additionalProperties": true,
      "description": "The file browser's visual root (OPEN_TERMINAL_FILE_BROWSER_ROOT); absent for `filesystem`. A UI hint, not a restriction",
      "required": ["path", "label"],
      "properties": {
        "path": {"type": "string"},
        "label": {"type": "string"}
      }
    }
  }
}
```

## `POST /files/cwd`

404 when the directory does not exist (single-user mode only).

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "MkdirRequest, reused: here the directory to remember",
  "required": ["path"],
  "properties": {
    "path": {
      "type": "string",
      "description": "Directory path to create. Parent directories are created automatically."
    }
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["cwd"],
  "properties": {
    "cwd": {"type": "string", "description": "The resolved path"}
  }
}
```

## `GET /files/list`

The model's `list_files` tool. 404 when the directory does not exist.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["dir", "writable", "entries"],
  "properties": {
    "dir": {"type": "string", "description": "The resolved directory"},
    "writable": {"type": "boolean"},
    "entries": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "$comment": "UserFS.listdir entry (open_terminal/utils/fs.py)",
        "required": ["name", "type", "size", "modified", "writable"],
        "properties": {
          "name": {"type": "string"},
          "type": {"enum": ["file", "directory"]},
          "size": {"type": "integer", "description": "Bytes (st_size)"},
          "modified": {"type": "number", "description": "Unix seconds (st_mtime)"},
          "writable": {"type": "boolean"}
        }
      },
      "description": "Sorted by name"
    }
  }
}
```

## `GET /files/read`

The model's `read_file` tool. A text file, or a document whose text it extracts (PDF, Office, OpenDocument, …), answers JSON; a binary whose type starts with an OPEN_TERMINAL_BINARY_MIME_PREFIXES entry (`image` by default) answers its raw bytes with that type; any other binary is a 415.

Response `200` (`application/json`) — or, for an image, its bytes:

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["path", "total_lines", "content"],
  "properties": {
    "path": {"type": "string"},
    "total_lines": {"type": "integer", "description": "Lines in the whole file"},
    "content": {
      "type": "string",
      "description": "Lines `start_line`..`end_line` (1-indexed, inclusive)"
    }
  }
}
```

## `GET /files/view`

Any file's raw bytes, for the UI (none of `/files/read`'s restrictions). 404 when the file is not there.

Response `200` (`application/octet-stream`) — the bytes, typed by the file's extension (`application/octet-stream` when unknown); with `preview=true` a DOCX / PPTX that LibreOffice renders comes back as `application/pdf`.

## `GET /files/serve/:filePath`

`/files/view` addressed by path (`/files/serve/home/user/x.html` serves `/home/user/x.html`), never a preview.

Response `200` (`application/octet-stream`) — the bytes, typed by the file's extension.

## `GET /files/display`

The model's `display_file` tool: serves nothing; Open WebUI opens the file in its preview pane.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["path", "exists"],
  "properties": {
    "path": {"type": "string"},
    "exists": {"type": "boolean", "description": "Whether it is a file"}
  }
}
```

## `POST /files/write`

The model's `write_file` tool. 400 on an OS error.

Request body (`application/json`):

```json
{
  "properties": {
    "path": {
      "type": "string",
      "description": "Absolute or relative path to write to. Parent directories are created automatically."
    },
    "content": {"type": "string", "description": "Text content to write to the file."}
  },
  "type": "object",
  "required": ["path", "content"],
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["path", "size"],
  "properties": {
    "path": {"type": "string", "description": "The resolved absolute path"},
    "size": {"type": "integer", "description": "Bytes written"}
  }
}
```

## `POST /files/replace`

The model's `replace_file_content` tool. 404 when the file is not there; 400 when a target is not found, or found more than once without `allow_multiple`.

Request body (`application/json`):

```json
{
  "properties": {
    "path": {"type": "string", "description": "Path to the file to modify."},
    "replacements": {
      "items": {
        "properties": {
          "target": {
            "type": "string",
            "description": "Exact string to find. Must match precisely, including whitespace."
          },
          "replacement": {"type": "string", "description": "Content to replace the target with."},
          "start_line": {
            "anyOf": [
              {"type": "integer", "minimum": 1.0},
              {"type": "null"}
            ],
            "description": "Narrow the search to lines at or after this (1-indexed)."
          },
          "end_line": {
            "anyOf": [
              {"type": "integer", "minimum": 1.0},
              {"type": "null"}
            ],
            "description": "Narrow the search to lines at or before this (1-indexed)."
          },
          "allow_multiple": {
            "type": "boolean",
            "description": "If true, replaces all occurrences. If false, errors when multiple matches are found.",
            "default": false
          }
        },
        "type": "object",
        "required": ["target", "replacement"],
        "additionalProperties": true
      },
      "type": "array",
      "description": "List of find-and-replace operations to apply sequentially."
    }
  },
  "type": "object",
  "required": ["path", "replacements"],
  "additionalProperties": true
}
```

Response `200` (`application/json`) — `size`: the new file's bytes:

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["path", "size"],
  "properties": {
    "path": {"type": "string", "description": "The resolved absolute path"},
    "size": {"type": "integer", "description": "Bytes written"}
  }
}
```

## `POST /files/mkdir`

400 on an OS error.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "MkdirRequest",
  "required": ["path"],
  "properties": {
    "path": {
      "type": "string",
      "description": "Directory path to create. Parent directories are created automatically."
    }
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["path"],
  "properties": {
    "path": {"type": "string"}
  }
}
```

## `POST /files/move`

404 when the source is missing, 400 when the destination's parent is, 409 when the destination exists.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "MoveRequest",
  "required": ["source", "destination"],
  "properties": {
    "source": {"type": "string", "description": "Path to the file or directory to move."},
    "destination": {"type": "string", "description": "Destination path (new location)."}
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["source", "destination"],
  "properties": {
    "source": {"type": "string"},
    "destination": {"type": "string"}
  }
}
```

## `DELETE /files/delete`

Recursive, no undo. 404 when the path is not there, 400 on an OS error.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["path", "type"],
  "properties": {
    "path": {"type": "string"},
    "type": {"enum": ["file", "directory"]}
  }
}
```

## `POST /files/upload`

`directory` (a query parameter) is created when missing; the file keeps its own basename. 403 on a permission error, 400 on another OS error.

Request body (`multipart/form-data`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["file"],
  "properties": {
    "file": {
      "type": "string",
      "contentMediaType": "application/octet-stream",
      "description": "The file to upload."
    }
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["path", "size"],
  "properties": {
    "path": {"type": "string", "description": "The resolved absolute path"},
    "size": {"type": "integer", "description": "Bytes written"}
  }
}
```

## `POST /files/archive`

400 for an empty `paths`, 404 when any path is not there.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "ArchiveRequest",
  "required": ["paths"],
  "properties": {
    "paths": {
      "type": "array",
      "items": {"type": "string"},
      "description": "List of file or directory paths to include in the ZIP archive."
    }
  }
}
```

Response `200` (`application/zip`) — `attachment; filename="<name>.zip"`: the basename of a single path, else `download.zip`.

## `POST /files/compare`

A line diff of two files' text (documents through their text extractor), run as the caller's account in a 60 s worker. 404 / 403 for a missing / forbidden file, 422 when the text cannot be extracted, 408 past 60 s.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "CompareRequest (open_terminal/utils/file_compare.py)",
  "required": ["original", "revised"],
  "properties": {
    "original": {"type": "string", "minLength": 1, "description": "The first file's path"},
    "revised": {"type": "string", "minLength": 1, "description": "The second file's path"},
    "ignore_whitespace": {"type": "boolean", "default": false}
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["original", "revised", "additions", "deletions", "hunks"],
  "properties": {
    "original": {
      "type": "object",
      "additionalProperties": true,
      "required": ["path", "name", "notices"],
      "properties": {
        "path": {"type": "string"},
        "name": {"type": "string"},
        "notices": {
          "type": "array",
          "items": {"type": "string"},
          "description": "What the text extraction leaves out"
        }
      }
    },
    "revised": {
      "type": "object",
      "additionalProperties": true,
      "required": ["path", "name", "notices"],
      "properties": {
        "path": {"type": "string"},
        "name": {"type": "string"},
        "notices": {
          "type": "array",
          "items": {"type": "string"},
          "description": "What the text extraction leaves out"
        }
      }
    },
    "additions": {"type": "integer"},
    "deletions": {"type": "integer"},
    "hunks": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["header", "lines"],
        "properties": {
          "header": {"type": "string", "description": "`@@ -a,b +c,d @@`"},
          "lines": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": true,
              "required": ["type", "oldNumber", "newNumber", "content", "segments"],
              "properties": {
                "type": {"enum": ["context", "removed", "added"]},
                "oldNumber": {"type": ["integer", "null"]},
                "newNumber": {"type": ["integer", "null"]},
                "content": {"type": "string"},
                "revisedContent": {"type": "string", "description": "On `context` lines only"},
                "segments": {
                  "type": "array",
                  "items": {
                    "type": "object",
                    "additionalProperties": true,
                    "required": ["text", "changed"],
                    "properties": {
                      "text": {"type": "string"},
                      "changed": {"type": "boolean"}
                    }
                  },
                  "description": "Word-level changes of a removed / added pair"
                }
              }
            }
          }
        }
      }
    }
  }
}
```

## `GET /files/grep`

The model's `grep_search` tool. 404 when the path is not there, 400 for a bad regex.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["query", "path", "matches", "truncated"],
  "properties": {
    "query": {"type": "string"},
    "path": {"type": "string"},
    "matches": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["file"],
        "properties": {
          "file": {"type": "string"},
          "line": {"type": "integer", "description": "With `match_per_line` (the default)"},
          "content": {"type": "string", "description": "The matching line, with `match_per_line`"}
        }
      }
    },
    "truncated": {"type": "boolean", "description": "`max_results` was reached"}
  }
}
```

## `GET /files/glob`

The model's `glob_search` tool. 404 when the directory is not there.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["pattern", "path", "matches", "truncated"],
  "properties": {
    "pattern": {"type": "string"},
    "path": {"type": "string"},
    "matches": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["path", "type", "size", "modified"],
        "properties": {
          "path": {"type": "string", "description": "Relative to `path`"},
          "type": {"enum": ["file", "directory"]},
          "size": {"type": "integer"},
          "modified": {"type": "number"}
        }
      }
    },
    "truncated": {"type": "boolean"}
  }
}
```

## `GET /files/search`

The model's `search_files` tool: ranked filename matches (git-tracked files when the directory is a repo). 404 when the directory is not there.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["results"],
  "properties": {
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["path", "name", "type", "size", "modified"],
        "properties": {
          "path": {"type": "string"},
          "name": {"type": "string"},
          "type": {"enum": ["file", "directory"]},
          "size": {"type": "integer"},
          "modified": {"type": "number"}
        }
      }
    }
  }
}
```

## `GET /files/matches`

The model's `match_files` tool: ranked name, path and content matches, paged by `offset` / `limit` (at most 100). 400 for a blank query, 404 when the directory is not there.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["results", "next_offset"],
  "properties": {
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["path", "relative_path", "name", "type", "name_match", "content_matches"],
        "properties": {
          "path": {"type": "string"},
          "relative_path": {"type": "string"},
          "name": {"type": "string"},
          "type": {"enum": ["file", "directory"]},
          "name_match": {"type": "boolean"},
          "content_matches": {
            "type": "array",
            "description": "At most 3 per file",
            "items": {
              "type": "object",
              "additionalProperties": true,
              "required": ["line", "column", "text"],
              "properties": {
                "line": {"type": "integer"},
                "column": {"type": "integer"},
                "text": {"type": "string"}
              }
            }
          }
        }
      }
    },
    "next_offset": {"type": ["integer", "null"], "description": "null on the last page"}
  }
}
```

## `GET /skills`

Response `200` (`application/json`):

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": true,
    "$comment": "SkillSummary in open_terminal/main.py",
    "required": ["id", "name", "description", "location", "scope", "source"],
    "properties": {
      "id": {"type": "string", "description": "`terminal:<url-quoted name>`"},
      "name": {"type": "string"},
      "description": {
        "type": "string",
        "description": "From the SKILL.md frontmatter, at most 1024 chars"
      },
      "location": {"type": "string", "description": "The SKILL.md path"},
      "scope": {"const": "global"},
      "source": {"const": "terminal"}
    }
  },
  "description": "Skills under ~/.agents/skills, ~/.cptr/skills, ~/.claude/skills and ~/.codex/skills with a described SKILL.md; the first of a name wins"
}
```

## `GET /skills/read`

404 when no skill has that name.

Response `200` (`application/json`):

```json
{
  "allOf": [
    {
      "type": "object",
      "additionalProperties": true,
      "$comment": "SkillSummary in open_terminal/main.py",
      "required": ["id", "name", "description", "location", "scope", "source"],
      "properties": {
        "id": {"type": "string", "description": "`terminal:<url-quoted name>`"},
        "name": {"type": "string"},
        "description": {
          "type": "string",
          "description": "From the SKILL.md frontmatter, at most 1024 chars"
        },
        "location": {"type": "string", "description": "The SKILL.md path"},
        "scope": {"const": "global"},
        "source": {"const": "terminal"}
      }
    }
  ],
  "type": "object",
  "additionalProperties": true,
  "required": ["content", "resources"],
  "properties": {
    "content": {"type": "string", "description": "SKILL.md without its frontmatter"},
    "resources": {
      "type": "array",
      "items": {"type": "string"},
      "description": "The skill folder's other files, relative"
    }
  }
}
```

## `POST /execute`

The model's `run_command` tool. The command always runs in the background; `wait` (seconds, 0-300; OPEN_TERMINAL_EXECUTE_TIMEOUT when absent) only holds the answer.

Request body (`application/json`):

```json
{
  "properties": {
    "command": {
      "type": "string",
      "description": "Shell command to execute. Supports chaining (&&, ||, ;), pipes (|), and redirections.",
      "examples": ["echo hello", "ls -la && whoami"]
    },
    "cwd": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "description": "Working directory for the command. Defaults to the server's current directory if not set."
    },
    "env": {
      "anyOf": [
        {
          "additionalProperties": {"type": "string"},
          "type": "object"
        },
        {"type": "null"}
      ],
      "description": "Extra environment variables merged into the subprocess environment."
    }
  },
  "type": "object",
  "required": ["command"],
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "execute() / get_status() in open_terminal/main.py; output from open_terminal/utils/log.py read_log",
  "required": [
    "id",
    "command",
    "status",
    "exit_code",
    "output",
    "truncated",
    "next_offset",
    "log_path"
  ],
  "properties": {
    "id": {"type": "string", "description": "`YYYYMMDD-HHMMSS-<6 hex>`"},
    "command": {"type": "string"},
    "status": {"enum": ["running", "done", "killed"]},
    "exit_code": {"type": ["integer", "null"], "description": "null while running"},
    "output": {
      "type": "array",
      "description": "Output entries since `offset` (the last `tail` of them when `tail` is set)",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["type", "data"],
        "properties": {
          "type": {
            "enum": ["stdout", "stderr", "output"],
            "description": "`output` when the command runs on a PTY"
          },
          "data": {"type": "string"}
        }
      }
    },
    "truncated": {
      "type": "boolean",
      "description": "Entries were skipped: rotated out of the log, or cut by `tail`"
    },
    "next_offset": {
      "type": "integer",
      "description": "Pass back as `offset` to read only new output"
    },
    "log_path": {"type": ["string", "null"], "description": "The process's JSONL log file"}
  }
}
```

## `GET /execute`

The model's `list_processes` tool: this X-User-Id + X-Session-Id pair's processes; a finished one is dropped 5 minutes after it ends.

Response `200` (`application/json`):

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": true,
    "required": ["id", "command", "status", "exit_code", "log_path"],
    "properties": {
      "id": {"type": "string"},
      "command": {"type": "string"},
      "status": {"enum": ["running", "done", "killed"]},
      "exit_code": {"type": ["integer", "null"]},
      "log_path": {"type": ["string", "null"]}
    }
  }
}
```

## `GET /execute/:processId/status`

The model's `get_process_status` tool. 404 for an unknown process, or one of another user / session.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "execute() / get_status() in open_terminal/main.py; output from open_terminal/utils/log.py read_log",
  "required": [
    "id",
    "command",
    "status",
    "exit_code",
    "output",
    "truncated",
    "next_offset",
    "log_path"
  ],
  "properties": {
    "id": {"type": "string", "description": "`YYYYMMDD-HHMMSS-<6 hex>`"},
    "command": {"type": "string"},
    "status": {"enum": ["running", "done", "killed"]},
    "exit_code": {"type": ["integer", "null"], "description": "null while running"},
    "output": {
      "type": "array",
      "description": "Output entries since `offset` (the last `tail` of them when `tail` is set)",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["type", "data"],
        "properties": {
          "type": {
            "enum": ["stdout", "stderr", "output"],
            "description": "`output` when the command runs on a PTY"
          },
          "data": {"type": "string"}
        }
      }
    },
    "truncated": {
      "type": "boolean",
      "description": "Entries were skipped: rotated out of the log, or cut by `tail`"
    },
    "next_offset": {
      "type": "integer",
      "description": "Pass back as `offset` to read only new output"
    },
    "log_path": {"type": ["string", "null"], "description": "The process's JSONL log file"}
  }
}
```

## `POST /execute/:processId/input`

The model's `send_process_input` tool. Literal escapes (`\n`, `\x03`) are turned into the characters. 404 for an unknown process; 400 when it has exited or its stdin is closed.

Request body (`application/json`):

```json
{
  "properties": {
    "input": {
      "type": "string",
      "description": "Text to send to the process's stdin. Include newline characters as needed."
    }
  },
  "type": "object",
  "required": ["input"],
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["status"],
  "properties": {
    "status": {"const": "ok"}
  }
}
```

## `DELETE /execute/:processId`

The model's `kill_process` tool: SIGTERM, or SIGKILL with `force=true`. 404 for an unknown process.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "description": "also for a process that had already finished",
  "required": ["status"],
  "properties": {
    "status": {"const": "killed"}
  }
}
```

## `POST /api/terminals`

Attach over the WebSocket `/api/terminals/{id}` (first-message auth), which a collection request cannot exercise.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "an interactive PTY session (create_terminal in open_terminal/main.py)",
  "required": ["id", "created_at", "pid"],
  "properties": {
    "id": {
      "type": "string",
      "description": "8 characters; the WebSocket attach path's `{session_id}`"
    },
    "created_at": {"type": "string", "description": "UTC ISO 8601 with a `Z`"},
    "pid": {"type": "integer"}
  }
}
```

Response `429` (`application/json`) — OPEN_TERMINAL_MAX_SESSIONS live sessions already exist:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "the PTY routes answer errors as `{\"error\"}`, not `{\"detail\"}`",
  "required": ["error"],
  "properties": {
    "error": {"type": "string"}
  }
}
```

Response `503` (`application/json`) — no PTY available (out of PTY devices, or the platform has none):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "the PTY routes answer errors as `{\"error\"}`, not `{\"detail\"}`",
  "required": ["error"],
  "properties": {
    "error": {"type": "string"}
  }
}
```

## `GET /api/terminals`

This X-User-Id + X-Session-Id pair's live sessions; dead ones are reaped.

Response `200` (`application/json`):

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": true,
    "$comment": "an interactive PTY session (create_terminal in open_terminal/main.py)",
    "required": ["id", "created_at", "pid"],
    "properties": {
      "id": {
        "type": "string",
        "description": "8 characters; the WebSocket attach path's `{session_id}`"
      },
      "created_at": {"type": "string", "description": "UTC ISO 8601 with a `Z`"},
      "pid": {"type": "integer"}
    }
  }
}
```

## `GET /api/terminals/:terminalSessionId`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "an interactive PTY session (create_terminal in open_terminal/main.py)",
  "required": ["id", "created_at", "pid"],
  "properties": {
    "id": {
      "type": "string",
      "description": "8 characters; the WebSocket attach path's `{session_id}`"
    },
    "created_at": {"type": "string", "description": "UTC ISO 8601 with a `Z`"},
    "pid": {"type": "integer"}
  }
}
```

Response `404` (`application/json`) — unknown, dead, or another user / session's: `Session not found`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "the PTY routes answer errors as `{\"error\"}`, not `{\"detail\"}`",
  "required": ["error"],
  "properties": {
    "error": {"type": "string"}
  }
}
```

## `DELETE /api/terminals/:terminalSessionId`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["status"],
  "properties": {
    "status": {"const": "deleted"}
  }
}
```

Response `404` (`application/json`) — unknown, or another user / session's: `Session not found`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "the PTY routes answer errors as `{\"error\"}`, not `{\"detail\"}`",
  "required": ["error"],
  "properties": {
    "error": {"type": "string"}
  }
}
```

## `GET /api/terminals/user/output`

The model's `read_user_terminal` tool. 404 `No connected user shell` when this user / session has no attached live PTY.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["output"],
  "properties": {
    "output": {"type": "string", "description": "The shell's recent output, decoded leniently"}
  }
}
```

## `POST /api/terminals/user/input`

The model's `send_user_terminal_input` tool: literal text, no escape decoding. 404 with no connected shell, 409 when it disconnected.

Request body (`application/json`):

```json
{
  "properties": {
    "input": {
      "type": "string",
      "description": "Text to send to the process's stdin. Include newline characters as needed."
    }
  },
  "type": "object",
  "required": ["input"],
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["status"],
  "properties": {
    "status": {"const": "ok"}
  }
}
```
