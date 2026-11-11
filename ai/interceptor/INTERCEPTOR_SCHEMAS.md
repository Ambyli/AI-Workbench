# interceptor request and response schemas

Generated from [`interceptor.schemas.json`](interceptor.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`INTERCEPTOR.md`](INTERCEPTOR.md).

## `GET /v1/interceptor/health`

Liveness only: no check of Chrome, the profiles volume or the port pool.

Response `200` (`application/json`):

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

## `GET /v1/interceptor/profiles`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["root", "profiles"],
  "properties": {
    "root": {"type": "string", "description": "INTERCEPTOR_PROFILES_ROOT"},
    "profiles": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name", "path", "present", "size_bytes", "login_keys", "login_origins"],
        "description": "One profile's status; a profile with no directory is `present: false`, `size_bytes: 0` (a 200, not a 404)",
        "properties": {
          "name": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
            "description": "Named Chrome profile: a directory under INTERCEPTOR_PROFILES_ROOT (profiles.py `_NAME_RE`)"
          },
          "path": {
            "type": "string",
            "description": "Its directory, `<INTERCEPTOR_PROFILES_ROOT>/<name>`"
          },
          "present": {"type": "boolean", "description": "The directory exists and is not empty"},
          "size_bytes": {"type": "integer", "minimum": 0},
          "sentinel_present": {
            "type": "boolean",
            "description": "The `session_ok` sentinel an upload writes: only a profile with it boots headless (absent when the directory does not exist)"
          },
          "login_keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The credential reference names the profile's `login_actions` may use (`${username}`, `${password}`, …), from INTERCEPTOR_LOGINS_DIR/<name>.json; [] with no logins file. Never a value"
          },
          "login_origins": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The logins file's `allowed_origins`: the origins a credential fill may type into; [] with no logins file"
          },
          "login_error": {
            "type": "string",
            "description": "Why the profile's logins file is unusable (present only when it is)"
          }
        }
      },
      "description": "Every directory under the root whose name is a valid profile name, sorted by name"
    }
  }
}
```

## `GET /v1/interceptor/profiles/:profileName`

A name that fails the profile-name pattern is a 400.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["name", "path", "present", "size_bytes", "login_keys", "login_origins"],
  "description": "One profile's status; a profile with no directory is `present: false`, `size_bytes: 0` (a 200, not a 404)",
  "properties": {
    "name": {
      "type": "string",
      "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
      "description": "Named Chrome profile: a directory under INTERCEPTOR_PROFILES_ROOT (profiles.py `_NAME_RE`)"
    },
    "path": {"type": "string", "description": "Its directory, `<INTERCEPTOR_PROFILES_ROOT>/<name>`"},
    "present": {"type": "boolean", "description": "The directory exists and is not empty"},
    "size_bytes": {"type": "integer", "minimum": 0},
    "sentinel_present": {
      "type": "boolean",
      "description": "The `session_ok` sentinel an upload writes: only a profile with it boots headless (absent when the directory does not exist)"
    },
    "login_keys": {
      "type": "array",
      "items": {"type": "string"},
      "description": "The credential reference names the profile's `login_actions` may use (`${username}`, `${password}`, …), from INTERCEPTOR_LOGINS_DIR/<name>.json; [] with no logins file. Never a value"
    },
    "login_origins": {
      "type": "array",
      "items": {"type": "string"},
      "description": "The logins file's `allowed_origins`: the origins a credential fill may type into; [] with no logins file"
    },
    "login_error": {
      "type": "string",
      "description": "Why the profile's logins file is unusable (present only when it is)"
    }
  }
}
```

## `POST /v1/interceptor/profiles/:profileName/refresh`

Wipes the profile's directory first, then extracts the archive and writes the `session_ok` sentinel. A bad name, or an archive that does not unpack, is a 400; no `archive` field is FastAPI's 422.

Request body (`multipart/form-data`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["archive"],
  "properties": {
    "archive": {
      "type": "string",
      "contentMediaType": "application/gzip",
      "description": "A .tgz / .tar.gz of a captured Chrome --user-data-dir (`tarfile.open(mode='r:*')`, so gzip / bzip2 / xz are detected); archive the CONTENTS of the profile dir (`tar czf x.tgz -C <profile_dir> .`)"
    }
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["unpacked", "name", "path", "present", "size_bytes", "sentinel_present"],
  "description": "The profile as unpacked (no `login_keys` / `login_origins` here: GET /profiles/:profileName has them)",
  "properties": {
    "unpacked": {"const": true},
    "name": {
      "type": "string",
      "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
      "description": "Named Chrome profile: a directory under INTERCEPTOR_PROFILES_ROOT (profiles.py `_NAME_RE`)"
    },
    "path": {"type": "string"},
    "present": {"type": "boolean"},
    "size_bytes": {"type": "integer", "minimum": 0},
    "sentinel_present": {"const": true}
  }
}
```

## `DELETE /v1/interceptor/profiles/:profileName`

Idempotent. A name that fails the profile-name pattern is a 400.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["name", "deleted"],
  "properties": {
    "name": {
      "type": "string",
      "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
      "description": "Named Chrome profile: a directory under INTERCEPTOR_PROFILES_ROOT (profiles.py `_NAME_RE`)"
    },
    "deleted": {"type": "boolean", "description": "Whether the directory existed"}
  }
}
```

## `POST /v1/interceptor/capture`

Every error is the generic `{"detail": "<message>"}`: 400 a bad profile name, `url_patterns` / `login_url_patterns` regex, action step or `login_actions` reference (or a missing logins file), 429 the port pool is exhausted, 500 no Chrome binary; a body the model refuses (an unknown step type, an 11th screenshot, an empty `url_patterns` with no screenshot step) is FastAPI's 422.

Request body (`application/json`):

```json
{
  "$comment": "generated from app.CaptureRequest — do not edit",
  "properties": {
    "url": {"description": "URL to navigate to", "type": "string"},
    "url_patterns": {
      "description": "Regex patterns matched against every JSON XHR/fetch URL. Any capture whose URL matches at least one pattern is returned, bucketed by the first pattern that matched it. May be empty ONLY when `actions` has a `screenshot` step (a screenshot-only capture, whose window ends as soon as the actions finish).",
      "items": {"type": "string"},
      "type": "array"
    },
    "profile": {
      "description": "Named Chrome profile under INTERCEPTOR_PROFILES_ROOT. Must be refreshed via POST /profiles/{name}/refresh first if the target requires auth.",
      "type": "string"
    },
    "capture_window_seconds": {
      "default": 20,
      "description": "How long to keep Chrome running to collect captures.",
      "maximum": 600,
      "minimum": 1,
      "type": "integer"
    },
    "keep_open": {
      "default": false,
      "description": "If true, leave Chrome running after the window. Useful for interactive debugging; the job also stays in GET /jobs until the operator manually kills Chrome (or POSTs /jobs/{id}/cancel).",
      "type": "boolean"
    },
    "login_timeout": {"default": 300, "minimum": 1, "type": "integer"},
    "max_matches_per_pattern": {
      "anyOf": [
        {"minimum": 1, "type": "integer"},
        {"type": "null"}
      ],
      "default": null
    },
    "debug_logging": {"default": false, "type": "boolean"},
    "login_url_patterns": {
      "description": "Regex patterns matched (re.search) against the tab URL AFTER navigation, to detect a redirect to a login wall. Omit to use the built-in defaults (login / signin / /auth). Because these are full regexes — not substrings — you can anchor one to a bare domain that a substring couldn't distinguish from an in-app URL, e.g. '^https?://roofix\\.io/?$' matches the logged-out root but not 'roofix.io/project/...'. An empty list disables login detection.",
      "items": {"type": "string"},
      "type": "array"
    },
    "page_script": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "JS evaluated in the page once it is ready (before any `actions`). An EXPRESSION — use an async IIFE for multi-statement code; promises are awaited and the JSON-serialisable result comes back as `actions_report.page_script.value`."
    },
    "actions": {
      "description": "Ordered browser steps (wait_for / fill / click / press / select / wait / evaluate / screenshot / scroll) run once the page is ready, so the page's own code fires the requests `url_patterns` is waiting for. Stops at the first failed step — except a failed `screenshot`, which is recorded and the run continues. At most 10 screenshot steps. `click` / `fill` centre their element, so a `scroll` right before a `screenshot` frames it. See INTERCEPTOR.md § Page scripts and actions and § Screenshots.",
      "items": {
        "discriminator": {
          "mapping": {
            "click": "#/$defs/ClickAction",
            "evaluate": "#/$defs/EvaluateAction",
            "fill": "#/$defs/FillAction",
            "press": "#/$defs/PressAction",
            "screenshot": "#/$defs/ScreenshotAction",
            "scroll": "#/$defs/ScrollAction",
            "select": "#/$defs/SelectAction",
            "wait": "#/$defs/WaitAction",
            "wait_for": "#/$defs/WaitForAction"
          },
          "propertyName": "type"
        },
        "oneOf": [
          {
            "additionalProperties": false,
            "description": "Poll until an element exists (`attached`) or is visible (`visible`).",
            "properties": {
              "type": {"const": "wait_for", "type": "string"},
              "selector": {
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins.",
                "minLength": 1,
                "type": "string"
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "state": {"default": "visible", "enum": ["visible", "attached"], "type": "string"},
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "selector"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Focus the input (descending into a custom element's shadow root to the\nreal <input>/<textarea>), optionally clear it, type `value` with trusted\ninput events, then fire `change` and blur.",
            "properties": {
              "type": {"const": "fill", "type": "string"},
              "selector": {
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins.",
                "minLength": 1,
                "type": "string"
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "value": {"description": "Text to type.", "type": "string"},
              "clear": {
                "default": true,
                "description": "Empty the field before typing.",
                "type": "boolean"
              },
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "selector", "value"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Scroll the element into view and click its centre. `mouse` (default)\nsends real pressed/released mouse events; `js` calls `el.click()`.",
            "properties": {
              "type": {"const": "click", "type": "string"},
              "selector": {
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins.",
                "minLength": 1,
                "type": "string"
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "method": {"default": "mouse", "enum": ["mouse", "js"], "type": "string"},
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "selector"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Send one key (keyDown + keyUp) to the focused element — or, with\n`selector`, to that element after focusing it. `fill` blurs the field when\nit finishes, so pass `selector` to press Enter in a field you just filled.",
            "properties": {
              "type": {"const": "press", "type": "string"},
              "key": {
                "description": "Enter, Tab, Escape, Backspace, Delete, Space, ArrowUp/Down/Left/Right, Home, End, PageUp, PageDown, or a single character.",
                "type": "string"
              },
              "selector": {
                "anyOf": [
                  {"minLength": 1, "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins."
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "key"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Choose an option of a native <select> by value (or, failing that, by\nvisible text) and fire input + change. Not for lightning-combobox — click\nit open and click the option instead.",
            "properties": {
              "type": {"const": "select", "type": "string"},
              "selector": {
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins.",
                "minLength": 1,
                "type": "string"
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "value": {"type": "string"},
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "selector", "value"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Sleep (cancellable).",
            "properties": {
              "type": {"const": "wait", "type": "string"},
              "seconds": {"maximum": 600, "minimum": 0, "type": "number"}
            },
            "required": ["type", "seconds"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Run JS in the page and return its (JSON-serialisable) value. The script\nis evaluated as an EXPRESSION — wrap multi-statement code in an async IIFE:\n`(async () => { …; return x; })()`. Promises are awaited.",
            "properties": {
              "type": {"const": "evaluate", "type": "string"},
              "script": {"minLength": 1, "type": "string"},
              "timeout_s": {
                "default": 15.0,
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "script"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Capture an image of the page as it is at this point in the run — after\na best-effort settle (~5 s) for a step that navigated. The image lands on\nthis step's `actions_report` entry as `value` ({format, mime_type, width,\nheight, full_page, bytes, page_url, data_base64}). A failed screenshot is\nrecorded (ok=false) and the run continues.",
            "properties": {
              "type": {"const": "screenshot", "type": "string"},
              "format": {
                "default": "jpeg",
                "description": "Image encoding. jpeg (default) is ~10x smaller than png for a typical page; png is lossless; webp is smallest but less universally decodable.",
                "enum": ["jpeg", "png", "webp"],
                "type": "string"
              },
              "quality": {
                "default": 80,
                "description": "jpeg/webp compression quality. Ignored for png.",
                "maximum": 100,
                "minimum": 1,
                "type": "integer"
              },
              "full_page": {
                "default": false,
                "description": "Capture the whole scrollable document (clamped to max_height) instead of just the 1920x1080 viewport.",
                "type": "boolean"
              },
              "scale": {
                "default": 1.0,
                "description": "Output scale factor. 0.5 halves both axes and roughly quarters the payload — use it when the image is going into an LLM context and pixel-level detail isn't needed.",
                "exclusiveMinimum": 0,
                "maximum": 2.0,
                "type": "number"
              },
              "max_height": {
                "default": 8000,
                "description": "full_page only: cap on captured document height in CSS px. Chrome refuses clips beyond 16384.",
                "maximum": 16384,
                "minimum": 100,
                "type": "integer"
              },
              "timeout_s": {
                "default": 30.0,
                "description": "Budget for the whole step — settle, layout read and the capture itself (a tall full-page clip can take several seconds).",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Set the scroll position on whichever container actually scrolls — the\ndocument, or (Salesforce Lightning) an inner `overflow-y: auto` element,\nshadow DOM included. Three forms, at least one of selector / to / by:\n`selector` alone frames that element (`block`, default `start` = at the\ntop); `to` jumps to the top or bottom; `by` nudges by CSS px. With a\n`selector`, `to` / `by` act on that element's scroll container. `click`\nand `fill` centre their element, so a `scroll` right before a\n`screenshot` frames it. Nothing to scroll is a no-op, not a failure.\n`value`: {target ('window' | '<div class=…>' | null), element?,\nscroll_top, scroll_height, client_height, at_bottom}.",
            "properties": {
              "type": {"const": "scroll", "type": "string"},
              "selector": {
                "anyOf": [
                  {"minLength": 1, "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins."
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex. Needs `selector`."
              },
              "to": {
                "anyOf": [
                  {"enum": ["top", "bottom"], "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Jump to the top or bottom of the scroll container. Not with `by`."
              },
              "by": {
                "anyOf": [
                  {"maximum": 100000, "minimum": -100000, "type": "integer"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Scroll by this many CSS px (negative is up). Not with `to`."
              },
              "block": {
                "anyOf": [
                  {"enum": ["start", "center", "end", "nearest"], "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Selector-only form: where the element lands in the viewport. Default `start` (top of the frame)."
              },
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails. Bounds only the element lookup.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type"],
            "type": "object"
          }
        ]
      },
      "type": "array"
    },
    "actions_ready_timeout_seconds": {
      "anyOf": [
        {"maximum": 600, "minimum": 1, "type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "How long the readiness gate (tab off about:blank, not on a login URL, document complete, capture hook installed) may wait before the steps are abandoned. Defaults to capture_window_seconds."
    },
    "stop_when_matched": {
      "default": false,
      "description": "End the capture window as soon as every `url_patterns` bucket has at least one match (and the actions, if any, have finished) instead of waiting it out. Requires url_patterns.",
      "type": "boolean"
    },
    "login_actions": {
      "description": "Steps (wait_for / fill / click / press / select / wait / scroll — no evaluate or screenshot) run ONLY if the tab lands on a login wall (`login_url_patterns`), once per capture, to sign in; the session then continues to `url` as it would after a human login. A fill `value` may reference the profile's stored credentials as `${username}` / `${password}` (`$$` = a literal $) — they are resolved server-side from INTERCEPTOR_LOGINS_DIR/<profile>.json, and a fill only types into a page whose origin is in that file's `allowed_origins`. `${` anywhere else is a 400. `capture_window_seconds` must cover the login, the SSO redirect AND the target page load. See INTERCEPTOR.md § Login actions.",
      "items": {
        "discriminator": {
          "mapping": {
            "click": "#/$defs/ClickAction",
            "fill": "#/$defs/FillAction",
            "press": "#/$defs/PressAction",
            "scroll": "#/$defs/ScrollAction",
            "select": "#/$defs/SelectAction",
            "wait": "#/$defs/WaitAction",
            "wait_for": "#/$defs/WaitForAction"
          },
          "propertyName": "type"
        },
        "oneOf": [
          {
            "additionalProperties": false,
            "description": "Poll until an element exists (`attached`) or is visible (`visible`).",
            "properties": {
              "type": {"const": "wait_for", "type": "string"},
              "selector": {
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins.",
                "minLength": 1,
                "type": "string"
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "state": {"default": "visible", "enum": ["visible", "attached"], "type": "string"},
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "selector"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Focus the input (descending into a custom element's shadow root to the\nreal <input>/<textarea>), optionally clear it, type `value` with trusted\ninput events, then fire `change` and blur.",
            "properties": {
              "type": {"const": "fill", "type": "string"},
              "selector": {
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins.",
                "minLength": 1,
                "type": "string"
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "value": {"description": "Text to type.", "type": "string"},
              "clear": {
                "default": true,
                "description": "Empty the field before typing.",
                "type": "boolean"
              },
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "selector", "value"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Scroll the element into view and click its centre. `mouse` (default)\nsends real pressed/released mouse events; `js` calls `el.click()`.",
            "properties": {
              "type": {"const": "click", "type": "string"},
              "selector": {
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins.",
                "minLength": 1,
                "type": "string"
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "method": {"default": "mouse", "enum": ["mouse", "js"], "type": "string"},
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "selector"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Send one key (keyDown + keyUp) to the focused element — or, with\n`selector`, to that element after focusing it. `fill` blurs the field when\nit finishes, so pass `selector` to press Enter in a field you just filled.",
            "properties": {
              "type": {"const": "press", "type": "string"},
              "key": {
                "description": "Enter, Tab, Escape, Backspace, Delete, Space, ArrowUp/Down/Left/Right, Home, End, PageUp, PageDown, or a single character.",
                "type": "string"
              },
              "selector": {
                "anyOf": [
                  {"minLength": 1, "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins."
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "key"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Choose an option of a native <select> by value (or, failing that, by\nvisible text) and fire input + change. Not for lightning-combobox — click\nit open and click the option instead.",
            "properties": {
              "type": {"const": "select", "type": "string"},
              "selector": {
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins.",
                "minLength": 1,
                "type": "string"
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex."
              },
              "value": {"type": "string"},
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type", "selector", "value"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Sleep (cancellable).",
            "properties": {
              "type": {"const": "wait", "type": "string"},
              "seconds": {"maximum": 600, "minimum": 0, "type": "number"}
            },
            "required": ["type", "seconds"],
            "type": "object"
          },
          {
            "additionalProperties": false,
            "description": "Set the scroll position on whichever container actually scrolls — the\ndocument, or (Salesforce Lightning) an inner `overflow-y: auto` element,\nshadow DOM included. Three forms, at least one of selector / to / by:\n`selector` alone frames that element (`block`, default `start` = at the\ntop); `to` jumps to the top or bottom; `by` nudges by CSS px. With a\n`selector`, `to` / `by` act on that element's scroll container. `click`\nand `fill` centre their element, so a `scroll` right before a\n`screenshot` frames it. Nothing to scroll is a no-op, not a failure.\n`value`: {target ('window' | '<div class=…>' | null), element?,\nscroll_top, scroll_height, client_height, at_bottom}.",
            "properties": {
              "type": {"const": "scroll", "type": "string"},
              "selector": {
                "anyOf": [
                  {"minLength": 1, "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "CSS selector, matched in the document AND inside every open shadow root (Salesforce LWC / custom elements), each tree on its own — so `input` finds an input inside a component, but a descendant combinator never crosses a shadow boundary (`lightning-input input` matches nothing; use the host `lightning-input` — fill/select descend into it — or `input[name=...]`). The first VISIBLE match wins."
              },
              "text": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional filter on the matches: substring of the element's trimmed text (including text rendered in its shadow tree), value, aria-label, placeholder or title. Written `/…/flags` it is a JS regex. Needs `selector`."
              },
              "to": {
                "anyOf": [
                  {"enum": ["top", "bottom"], "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Jump to the top or bottom of the scroll container. Not with `by`."
              },
              "by": {
                "anyOf": [
                  {"maximum": 100000, "minimum": -100000, "type": "integer"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Scroll by this many CSS px (negative is up). Not with `to`."
              },
              "block": {
                "anyOf": [
                  {"enum": ["start", "center", "end", "nearest"], "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Selector-only form: where the element lands in the viewport. Default `start` (top of the frame)."
              },
              "timeout_s": {
                "default": 15.0,
                "description": "Seconds to keep polling for the element before the step fails. Bounds only the element lookup.",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "type": "number"
              }
            },
            "required": ["type"],
            "type": "object"
          }
        ]
      },
      "type": "array"
    },
    "login_actions_timeout_seconds": {
      "anyOf": [
        {"maximum": 600, "minimum": 1, "type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Upper bound on one login_actions run — the login page becoming ready plus every step (default 60). It runs inside capture_window_seconds, so the window must also be long enough for the login, the SSO redirect and the target page load."
    }
  },
  "required": ["url", "profile"],
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`) — `status` is the capture session's (`common.cdp_interceptor.ClientState`: `unlinked` / `loading` / `waiting_login` / `ok` / `error`) or `cancelled`; screenshots are base64 on their steps in `actions_report.actions[].value`:

```json
{
  "$comment": "generated from app.CaptureResponse — do not edit",
  "properties": {
    "job_id": {"type": "string"},
    "url": {"type": "string"},
    "status": {"type": "string"},
    "login_wall": {"type": "boolean"},
    "error": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ]
    },
    "matches": {
      "additionalProperties": {
        "items": {
          "properties": {
            "url": {"type": "string"},
            "body": {}
          },
          "required": ["url", "body"],
          "type": "object",
          "additionalProperties": false
        },
        "type": "array"
      },
      "type": "object"
    },
    "captured_urls": {
      "items": {"type": "string"},
      "type": "array"
    },
    "actions_report": {
      "anyOf": [
        {
          "properties": {
            "page_script": {
              "anyOf": [
                {
                  "properties": {
                    "index": {
                      "description": "Position in `actions`; -1 for `page_script`.",
                      "type": "integer"
                    },
                    "type": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "elapsed_ms": {"type": "integer"},
                    "error": {
                      "anyOf": [
                        {"type": "string"},
                        {"type": "null"}
                      ],
                      "default": null,
                      "description": "Why the step failed; 'skipped' when an earlier step failed (or the page never became ready) so it never ran."
                    },
                    "value": {
                      "default": null,
                      "description": "evaluate / page_script: the script's return value. DOM steps: details of the element acted on. screenshot: {format, mime_type, width, height, full_page, bytes, page_url, data_base64} (page_url is where the tab was at that step). scroll: {target, element?, scroll_top, scroll_height, client_height, at_bottom}."
                    }
                  },
                  "required": ["index", "type", "ok", "elapsed_ms"],
                  "type": "object",
                  "additionalProperties": false
                },
                {"type": "null"}
              ],
              "default": null
            },
            "actions": {
              "items": {
                "properties": {
                  "index": {
                    "description": "Position in `actions`; -1 for `page_script`.",
                    "type": "integer"
                  },
                  "type": {"type": "string"},
                  "ok": {"type": "boolean"},
                  "elapsed_ms": {"type": "integer"},
                  "error": {
                    "anyOf": [
                      {"type": "string"},
                      {"type": "null"}
                    ],
                    "default": null,
                    "description": "Why the step failed; 'skipped' when an earlier step failed (or the page never became ready) so it never ran."
                  },
                  "value": {
                    "default": null,
                    "description": "evaluate / page_script: the script's return value. DOM steps: details of the element acted on. screenshot: {format, mime_type, width, height, full_page, bytes, page_url, data_base64} (page_url is where the tab was at that step). scroll: {target, element?, scroll_top, scroll_height, client_height, at_bottom}."
                  }
                },
                "required": ["index", "type", "ok", "elapsed_ms"],
                "type": "object",
                "additionalProperties": false
              },
              "type": "array"
            },
            "aborted_reason": {
              "anyOf": [
                {"type": "string"},
                {"type": "null"}
              ],
              "default": null,
              "description": "null when every step ran (a failed screenshot step does not stop the run, so check each step's `ok`). Otherwise e.g. 'not ready: login page …', 'cancelled', 'actions[2] (click) failed: …', 'capture window ended before actions finished'."
            }
          },
          "type": "object",
          "additionalProperties": false
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "What page_script / actions did — null unless requested. Screenshot images are on their steps' `value`."
    },
    "login_actions_report": {
      "anyOf": [
        {
          "properties": {
            "page_script": {
              "anyOf": [
                {
                  "properties": {
                    "index": {
                      "description": "Position in `actions`; -1 for `page_script`.",
                      "type": "integer"
                    },
                    "type": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "elapsed_ms": {"type": "integer"},
                    "error": {
                      "anyOf": [
                        {"type": "string"},
                        {"type": "null"}
                      ],
                      "default": null,
                      "description": "Why the step failed; 'skipped' when an earlier step failed (or the page never became ready) so it never ran."
                    },
                    "value": {
                      "default": null,
                      "description": "evaluate / page_script: the script's return value. DOM steps: details of the element acted on. screenshot: {format, mime_type, width, height, full_page, bytes, page_url, data_base64} (page_url is where the tab was at that step). scroll: {target, element?, scroll_top, scroll_height, client_height, at_bottom}."
                    }
                  },
                  "required": ["index", "type", "ok", "elapsed_ms"],
                  "type": "object",
                  "additionalProperties": false
                },
                {"type": "null"}
              ],
              "default": null
            },
            "actions": {
              "items": {
                "properties": {
                  "index": {
                    "description": "Position in `actions`; -1 for `page_script`.",
                    "type": "integer"
                  },
                  "type": {"type": "string"},
                  "ok": {"type": "boolean"},
                  "elapsed_ms": {"type": "integer"},
                  "error": {
                    "anyOf": [
                      {"type": "string"},
                      {"type": "null"}
                    ],
                    "default": null,
                    "description": "Why the step failed; 'skipped' when an earlier step failed (or the page never became ready) so it never ran."
                  },
                  "value": {
                    "default": null,
                    "description": "evaluate / page_script: the script's return value. DOM steps: details of the element acted on. screenshot: {format, mime_type, width, height, full_page, bytes, page_url, data_base64} (page_url is where the tab was at that step). scroll: {target, element?, scroll_top, scroll_height, client_height, at_bottom}."
                  }
                },
                "required": ["index", "type", "ok", "elapsed_ms"],
                "type": "object",
                "additionalProperties": false
              },
              "type": "array"
            },
            "aborted_reason": {
              "anyOf": [
                {"type": "string"},
                {"type": "null"}
              ],
              "default": null,
              "description": "null when every step ran (a failed screenshot step does not stop the run, so check each step's `ok`). Otherwise e.g. 'not ready: login page …', 'cancelled', 'actions[2] (click) failed: …', 'capture window ended before actions finished'."
            }
          },
          "type": "object",
          "additionalProperties": false
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "What login_actions did — null unless they were sent AND the capture hit a login wall. A fill reports only `value_length`."
    },
    "ended_early": {
      "default": false,
      "description": "True only when the window ended before capture_window_seconds elapsed: stop_when_matched saw every pattern matched, or — with no url_patterns — the actions finished.",
      "type": "boolean"
    }
  },
  "required": ["job_id", "url", "status", "login_wall", "error", "matches", "captured_urls"],
  "type": "object",
  "additionalProperties": false
}
```

## `GET /v1/interceptor/jobs`

Response `200` (`application/json`):

```json
{
  "description": "The port pool and every capture currently running",
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
  "required": ["max_concurrent", "available"],
  "properties": {
    "jobs": {
      "items": {
        "description": "One in-flight capture: the shared `common.jobs` `JobBase` with the interceptor's own `metadata`. Completed captures are not retained",
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
          "phase": {
            "enum": ["cloning", "capturing", "actions", "cleaning_up"],
            "description": "`cloning` (slow path only, during the profile copy) → `capturing` → `cleaning_up`; `actions` while `page_script` / `actions` run (back to `capturing` when they finish)"
          },
          "metadata": {
            "$comment": "generated from app.InterceptorMetadata — do not edit",
            "description": "Interceptor-api's per-job metadata payload. Serialized into the\n``metadata`` field of ``JobBase`` for ``GET /jobs`` responses.",
            "properties": {
              "profile": {"type": "string"},
              "url": {"type": "string"},
              "port": {"type": "integer"},
              "used_base_profile": {"type": "boolean"},
              "temp_dir": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null
              },
              "actions_done": {
                "anyOf": [
                  {"type": "integer"},
                  {"type": "null"}
                ],
                "default": null
              },
              "actions_total": {
                "anyOf": [
                  {"type": "integer"},
                  {"type": "null"}
                ],
                "default": null
              }
            },
            "required": ["profile", "url", "port", "used_base_profile"],
            "type": "object",
            "additionalProperties": false
          },
          "result": {
            "type": "null",
            "description": "Never set: an in-memory job is gone from the registry once it completes"
          },
          "error": {"type": "null", "description": "Never set, like `result`"}
        }
      }
    },
    "max_concurrent": {
      "type": "integer",
      "description": "INTERCEPTOR_MAX_CONCURRENT: the port pool's size"
    },
    "available": {"type": "integer", "description": "Slots free: `max_concurrent` - `active_count`"}
  }
}
```

## `GET /v1/interceptor/jobs/:jobId`

An id that is not in flight (never existed, or already finished) is a 404.

Response `200` (`application/json`):

```json
{
  "description": "One in-flight capture: the shared `common.jobs` `JobBase` with the interceptor's own `metadata`. Completed captures are not retained",
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
    "phase": {
      "enum": ["cloning", "capturing", "actions", "cleaning_up"],
      "description": "`cloning` (slow path only, during the profile copy) → `capturing` → `cleaning_up`; `actions` while `page_script` / `actions` run (back to `capturing` when they finish)"
    },
    "metadata": {
      "$comment": "generated from app.InterceptorMetadata — do not edit",
      "description": "Interceptor-api's per-job metadata payload. Serialized into the\n``metadata`` field of ``JobBase`` for ``GET /jobs`` responses.",
      "properties": {
        "profile": {"type": "string"},
        "url": {"type": "string"},
        "port": {"type": "integer"},
        "used_base_profile": {"type": "boolean"},
        "temp_dir": {
          "anyOf": [
            {"type": "string"},
            {"type": "null"}
          ],
          "default": null
        },
        "actions_done": {
          "anyOf": [
            {"type": "integer"},
            {"type": "null"}
          ],
          "default": null
        },
        "actions_total": {
          "anyOf": [
            {"type": "integer"},
            {"type": "null"}
          ],
          "default": null
        }
      },
      "required": ["profile", "url", "port", "used_base_profile"],
      "type": "object",
      "additionalProperties": false
    },
    "result": {
      "type": "null",
      "description": "Never set: an in-memory job is gone from the registry once it completes"
    },
    "error": {"type": "null", "description": "Never set, like `result`"}
  }
}
```

## `POST /v1/interceptor/jobs/:jobId/cancel`

The POST /capture caller then gets a normal response with `status: "cancelled"`. 404 an id not in flight; 409 a capture already in `cleaning_up`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["job_id", "cancelled", "was_phase"],
  "properties": {
    "job_id": {"type": "string"},
    "cancelled": {"const": true},
    "was_phase": {
      "enum": ["cloning", "capturing", "actions"],
      "description": "The phase the capture was in when the cancel landed"
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
        "name": {"const": "Interceptor"},
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
      "minItems": 4,
      "maxItems": 4,
      "items": {
        "type": "object",
        "additionalProperties": true,
        "$comment": "mcp.types.Tool: the SDK allows extra keys",
        "required": ["name", "inputSchema"],
        "properties": {
          "name": {"enum": ["capture_url", "list_profiles", "list_jobs", "get_job"]},
          "description": {
            "type": "string",
            "description": "The tool's docstring: what the model reads"
          },
          "inputSchema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema: the arguments of `MCP <name>`"
          },
          "outputSchema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema of `structuredContent`: `{\"type\": \"object\", \"additionalProperties\": true}` for the three `-> dict` tools; none for `capture_url`"
          },
          "_meta": {"type": "object", "additionalProperties": true, "description": "FastMCP's own"}
        }
      }
    },
    "nextCursor": {"type": "string"}
  }
}
```

## `MCP capture_url`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message): POST /capture's body, except that a screenshot step's `value` has no `data_base64` -- `result.content` is that JSON as text, then a text label (`actions[i] screenshot — <page_url>, WxH`) and an image block per screenshot taken. Errors are data, never a JSON-RPC error: what POST /capture answers with a 400 / 422 / 429 / 500 comes back as this shape with `status: "error"`, `job_id: ""` and `error` saying why.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app.mcp:capture_url — do not edit",
  "additionalProperties": false,
  "properties": {
    "url": {"type": "string", "description": "Fully-qualified URL to navigate to (https://…)."},
    "profile": {
      "type": "string",
      "description": "Named profile under ``INTERCEPTOR_PROFILES_ROOT``. Discover\navailable names via the ``list_profiles`` tool. Profiles are\nuploaded out-of-band by an operator (see INTERCEPTOR.md)."
    },
    "url_patterns": {
      "anyOf": [
        {
          "items": {"type": "string"},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "List of regex patterns. Each intercepted XHR/fetch URL\nis matched with ``re.search`` against every pattern; the response\nbody lands in the bucket of the first pattern it matches. May be\nomitted only when ``actions`` has a ``screenshot`` step — then the\ncall ends as soon as the actions finish."
    },
    "capture_window_seconds": {
      "default": 20,
      "type": "integer",
      "description": "How long to run Chrome to collect captures\n(default 20). Increase if the page fires XHRs late."
    },
    "login_timeout": {
      "default": 300,
      "type": "integer",
      "description": "Max seconds to wait for a login redirect to resolve\nbefore returning ``login_wall: true``."
    },
    "max_matches_per_pattern": {
      "anyOf": [
        {"type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Cap on how many bodies to return per pattern."
    },
    "page_script": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "JavaScript run in the page once it has loaded, before\nany ``actions``. It is evaluated as an EXPRESSION; for several\nstatements use an async IIFE, ``(async () => { ...; return x;\n})()``. Promises are awaited; the JSON-serialisable result comes\nback as ``actions_report.page_script.value``. Use it to inspect\nthe DOM (e.g. list the form fields before writing ``actions``).\nDo NOT use it to call the site's API with ``fetch()`` — sites that\nsign their requests (CSRF tokens) refuse hand-made calls; drive\nthe form with ``actions`` instead."
    },
    "actions": {
      "anyOf": [
        {
          "items": {"additionalProperties": true, "type": "object"},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Ordered steps run after the page is ready (off about:blank,\nnot on a login URL, fully loaded, capture hook installed). Each is\nan object with a ``type``:\n``{\"type\": \"wait_for\", \"selector\", \"text\"?, \"state\"?: \"visible\"|\"attached\", \"timeout_s\"?}``,\n``{\"type\": \"fill\", \"selector\", \"value\", \"text\"?, \"clear\"?: true, \"timeout_s\"?}``,\n``{\"type\": \"click\", \"selector\", \"text\"?, \"method\"?: \"mouse\"|\"js\", \"timeout_s\"?}``,\n``{\"type\": \"press\", \"key\": \"Enter\"|\"Tab\"|\"Escape\"|…|<one char>, \"selector\"?, \"text\"?}``,\n``{\"type\": \"select\", \"selector\", \"value\", \"text\"?}`` (native <select> only),\n``{\"type\": \"wait\", \"seconds\"}``,\n``{\"type\": \"evaluate\", \"script\", \"timeout_s\"?}``,\n``{\"type\": \"screenshot\", \"format\"?: \"jpeg\"|\"png\"|\"webp\", \"quality\"?: 1-100, \"full_page\"?: false, \"scale\"?: 0-2, \"max_height\"?: 100-16384, \"timeout_s\"?: 30}``,\n``{\"type\": \"scroll\", \"selector\"?, \"text\"?, \"to\"?: \"top\"|\"bottom\", \"by\"?: <px>, \"block\"?: \"start\"|\"center\"|\"end\"|\"nearest\"}`` (at least one of selector / to / by; to and by exclusive; block only with selector alone).\n``selector`` is CSS matched in the document AND inside every open\nshadow root (Salesforce Lightning / web components), each tree\nseparately — so a descendant combinator never crosses into a\ncomponent (``lightning-input input`` finds nothing; use\n``lightning-input`` or ``input[name=x]``). First visible match\nwins; ``text`` narrows it by visible text / value /\naria-label / placeholder / title (substring, or ``/regex/flags``).\n``fill`` types into the real <input> inside a component host with\ntrusted keyboard input; ``click`` sends a real mouse click at the\nelement's centre. ``timeout_s`` defaults to 15. Steps stop at the\nfirst failure — read ``actions_report`` to see which and why.\n``screenshot`` captures the page as it is at that point (after a\nshort settle if the previous step navigated); put one after a\nstep to see what it did. Each comes back as an image block, in\nstep order, labelled ``actions[i] screenshot — <page_url>, WxH``,\nand its metadata ({format, mime_type, width, height, full_page,\nbytes, page_url}) is the step's ``value``. A failed screenshot is\nrecorded (``ok: false``) and the steps after it still run. At most\n10 per call; prefer ``\"scale\": 0.5`` — it quarters the image and\nis plenty to read a page. ``full_page`` captures the whole\ndocument (up to ``max_height`` px) instead of the 1920x1080\nviewport.\n``scroll`` sets where the page is scrolled: ``selector`` alone\nputs that element at the top of the frame (``block``); ``to`` /\n``by`` (CSS px, negative is up) move the page's main scroller —\nthe document, or the inner container a Salesforce Lightning page\nactually scrolls — or, with a ``selector``, that element's scroll\ncontainer. ``click`` and ``fill`` centre their element, so a\n``scroll`` right before a ``screenshot`` frames it. Nothing to\nscroll is a no-op (``target: null``), not a failure; the\n``value`` reports ``scroll_top`` / ``scroll_height`` /\n``at_bottom``."
    },
    "stop_when_matched": {
      "default": false,
      "type": "boolean",
      "description": "End as soon as every pattern has at least one\nmatch and the actions have finished, instead of waiting out\n``capture_window_seconds``. A click finishes when the mouse is\nreleased, not when its request completes — if the page also makes\nmatching calls on its own, end ``actions`` with a step that waits\nfor the response you want — a ``wait_for`` on the element that\nshows the result, or an ``evaluate`` that polls\n``window._capturedResponses`` for a new entry (INTERCEPTOR.md §\nWorked example) — plus a short ``wait`` so it reaches the service."
    },
    "login_url_patterns": {
      "anyOf": [
        {
          "items": {"type": "string"},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Regexes ``re.search``-matched against the tab URL\nafter navigation to spot a redirect to a login page. Omit for the\ndefaults (``login``, ``signin``, ``/auth``); a list REPLACES them,\nso include them yourself if you still want them. Add the site's\nSSO host when it doesn't match those (Enphase:\n``sso\\.enphaseenergy\\.com``) — otherwise an expired session\nlooks like an empty result instead of ``login_wall: true``. An\nempty list disables login detection."
    },
    "actions_ready_timeout_seconds": {
      "anyOf": [
        {"type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "How long the actions wait for the\npage to be ready (loaded, off any login page) before giving up.\nDefaults to ``capture_window_seconds``."
    },
    "login_actions": {
      "anyOf": [
        {
          "items": {"additionalProperties": true, "type": "object"},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Steps that run ONLY if the page redirects to a login\nwall (``login_url_patterns``), to sign in with the profile's\nstored service account; the capture then continues to ``url``.\nSame step objects as ``actions`` except ``evaluate`` and\n``screenshot``. For the\ncredentials write REFERENCES — ``\"value\": \"${username}\"`` and\n``\"value\": \"${password}\"`` — which the server fills in from its\nown credentials file; ``list_profiles`` shows the reference names\n(``login_keys``) each profile has. NEVER ask a user for a real\nusername or password and never put one in a step. Enphase:\n``login_url_patterns=[\"login\", \"signin\", \"/auth\",\n\"sso\\.enphaseenergy\\.com\"]``, ``login_actions=[{\"type\": \"fill\",\n\"selector\": \"#username\", \"value\": \"${username}\"},\n{\"type\": \"fill\", \"selector\": \"#password\", \"value\":\n\"${password}\"}, {\"type\": \"click\", \"selector\":\n\"input[type=submit].button\"}]``. Raise\n``capture_window_seconds`` (~120) so the login, the redirect and\nthe page load all fit; the result says what happened in\n``login_actions_report``."
    }
  },
  "required": ["url", "profile"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "$comment": "generated from app.CaptureResponse — do not edit",
  "properties": {
    "job_id": {"type": "string"},
    "url": {"type": "string"},
    "status": {"type": "string"},
    "login_wall": {"type": "boolean"},
    "error": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ]
    },
    "matches": {
      "additionalProperties": {
        "items": {
          "properties": {
            "url": {"type": "string"},
            "body": {}
          },
          "required": ["url", "body"],
          "type": "object",
          "additionalProperties": false
        },
        "type": "array"
      },
      "type": "object"
    },
    "captured_urls": {
      "items": {"type": "string"},
      "type": "array"
    },
    "actions_report": {
      "anyOf": [
        {
          "properties": {
            "page_script": {
              "anyOf": [
                {
                  "properties": {
                    "index": {
                      "description": "Position in `actions`; -1 for `page_script`.",
                      "type": "integer"
                    },
                    "type": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "elapsed_ms": {"type": "integer"},
                    "error": {
                      "anyOf": [
                        {"type": "string"},
                        {"type": "null"}
                      ],
                      "default": null,
                      "description": "Why the step failed; 'skipped' when an earlier step failed (or the page never became ready) so it never ran."
                    },
                    "value": {
                      "default": null,
                      "description": "evaluate / page_script: the script's return value. DOM steps: details of the element acted on. screenshot: {format, mime_type, width, height, full_page, bytes, page_url, data_base64} (page_url is where the tab was at that step). scroll: {target, element?, scroll_top, scroll_height, client_height, at_bottom}."
                    }
                  },
                  "required": ["index", "type", "ok", "elapsed_ms"],
                  "type": "object",
                  "additionalProperties": false
                },
                {"type": "null"}
              ],
              "default": null
            },
            "actions": {
              "items": {
                "properties": {
                  "index": {
                    "description": "Position in `actions`; -1 for `page_script`.",
                    "type": "integer"
                  },
                  "type": {"type": "string"},
                  "ok": {"type": "boolean"},
                  "elapsed_ms": {"type": "integer"},
                  "error": {
                    "anyOf": [
                      {"type": "string"},
                      {"type": "null"}
                    ],
                    "default": null,
                    "description": "Why the step failed; 'skipped' when an earlier step failed (or the page never became ready) so it never ran."
                  },
                  "value": {
                    "default": null,
                    "description": "evaluate / page_script: the script's return value. DOM steps: details of the element acted on. screenshot: {format, mime_type, width, height, full_page, bytes, page_url, data_base64} (page_url is where the tab was at that step). scroll: {target, element?, scroll_top, scroll_height, client_height, at_bottom}."
                  }
                },
                "required": ["index", "type", "ok", "elapsed_ms"],
                "type": "object",
                "additionalProperties": false
              },
              "type": "array"
            },
            "aborted_reason": {
              "anyOf": [
                {"type": "string"},
                {"type": "null"}
              ],
              "default": null,
              "description": "null when every step ran (a failed screenshot step does not stop the run, so check each step's `ok`). Otherwise e.g. 'not ready: login page …', 'cancelled', 'actions[2] (click) failed: …', 'capture window ended before actions finished'."
            }
          },
          "type": "object",
          "additionalProperties": false
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "What page_script / actions did — null unless requested. Screenshot images are on their steps' `value`."
    },
    "login_actions_report": {
      "anyOf": [
        {
          "properties": {
            "page_script": {
              "anyOf": [
                {
                  "properties": {
                    "index": {
                      "description": "Position in `actions`; -1 for `page_script`.",
                      "type": "integer"
                    },
                    "type": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "elapsed_ms": {"type": "integer"},
                    "error": {
                      "anyOf": [
                        {"type": "string"},
                        {"type": "null"}
                      ],
                      "default": null,
                      "description": "Why the step failed; 'skipped' when an earlier step failed (or the page never became ready) so it never ran."
                    },
                    "value": {
                      "default": null,
                      "description": "evaluate / page_script: the script's return value. DOM steps: details of the element acted on. screenshot: {format, mime_type, width, height, full_page, bytes, page_url, data_base64} (page_url is where the tab was at that step). scroll: {target, element?, scroll_top, scroll_height, client_height, at_bottom}."
                    }
                  },
                  "required": ["index", "type", "ok", "elapsed_ms"],
                  "type": "object",
                  "additionalProperties": false
                },
                {"type": "null"}
              ],
              "default": null
            },
            "actions": {
              "items": {
                "properties": {
                  "index": {
                    "description": "Position in `actions`; -1 for `page_script`.",
                    "type": "integer"
                  },
                  "type": {"type": "string"},
                  "ok": {"type": "boolean"},
                  "elapsed_ms": {"type": "integer"},
                  "error": {
                    "anyOf": [
                      {"type": "string"},
                      {"type": "null"}
                    ],
                    "default": null,
                    "description": "Why the step failed; 'skipped' when an earlier step failed (or the page never became ready) so it never ran."
                  },
                  "value": {
                    "default": null,
                    "description": "evaluate / page_script: the script's return value. DOM steps: details of the element acted on. screenshot: {format, mime_type, width, height, full_page, bytes, page_url, data_base64} (page_url is where the tab was at that step). scroll: {target, element?, scroll_top, scroll_height, client_height, at_bottom}."
                  }
                },
                "required": ["index", "type", "ok", "elapsed_ms"],
                "type": "object",
                "additionalProperties": false
              },
              "type": "array"
            },
            "aborted_reason": {
              "anyOf": [
                {"type": "string"},
                {"type": "null"}
              ],
              "default": null,
              "description": "null when every step ran (a failed screenshot step does not stop the run, so check each step's `ok`). Otherwise e.g. 'not ready: login page …', 'cancelled', 'actions[2] (click) failed: …', 'capture window ended before actions finished'."
            }
          },
          "type": "object",
          "additionalProperties": false
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "What login_actions did — null unless they were sent AND the capture hit a login wall. A fill reports only `value_length`."
    },
    "ended_early": {
      "default": false,
      "description": "True only when the window ended before capture_window_seconds elapsed: stop_when_matched saw every pattern matched, or — with no url_patterns — the actions finished.",
      "type": "boolean"
    }
  },
  "required": ["job_id", "url", "status", "login_wall", "error", "matches", "captured_urls"],
  "type": "object",
  "additionalProperties": false
}
```

## `MCP list_profiles`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message), the same as GET /profiles; `result.content[0].text` carries it as JSON.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app.mcp:list_profiles — do not edit",
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
  "required": ["root", "profiles"],
  "properties": {
    "root": {"type": "string", "description": "INTERCEPTOR_PROFILES_ROOT"},
    "profiles": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name", "path", "present", "size_bytes", "login_keys", "login_origins"],
        "description": "One profile's status; a profile with no directory is `present: false`, `size_bytes: 0` (a 200, not a 404)",
        "properties": {
          "name": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
            "description": "Named Chrome profile: a directory under INTERCEPTOR_PROFILES_ROOT (profiles.py `_NAME_RE`)"
          },
          "path": {
            "type": "string",
            "description": "Its directory, `<INTERCEPTOR_PROFILES_ROOT>/<name>`"
          },
          "present": {"type": "boolean", "description": "The directory exists and is not empty"},
          "size_bytes": {"type": "integer", "minimum": 0},
          "sentinel_present": {
            "type": "boolean",
            "description": "The `session_ok` sentinel an upload writes: only a profile with it boots headless (absent when the directory does not exist)"
          },
          "login_keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The credential reference names the profile's `login_actions` may use (`${username}`, `${password}`, …), from INTERCEPTOR_LOGINS_DIR/<name>.json; [] with no logins file. Never a value"
          },
          "login_origins": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The logins file's `allowed_origins`: the origins a credential fill may type into; [] with no logins file"
          },
          "login_error": {
            "type": "string",
            "description": "Why the profile's logins file is unusable (present only when it is)"
          }
        }
      },
      "description": "Every directory under the root whose name is a valid profile name, sorted by name"
    }
  }
}
```

## `MCP list_jobs`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message), the same as GET /jobs; `result.content[0].text` carries it as JSON.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app.mcp:list_jobs — do not edit",
  "additionalProperties": false,
  "properties": {},
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "description": "The port pool and every capture currently running",
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
  "required": ["max_concurrent", "available"],
  "properties": {
    "jobs": {
      "items": {
        "description": "One in-flight capture: the shared `common.jobs` `JobBase` with the interceptor's own `metadata`. Completed captures are not retained",
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
          "phase": {
            "enum": ["cloning", "capturing", "actions", "cleaning_up"],
            "description": "`cloning` (slow path only, during the profile copy) → `capturing` → `cleaning_up`; `actions` while `page_script` / `actions` run (back to `capturing` when they finish)"
          },
          "metadata": {
            "$comment": "generated from app.InterceptorMetadata — do not edit",
            "description": "Interceptor-api's per-job metadata payload. Serialized into the\n``metadata`` field of ``JobBase`` for ``GET /jobs`` responses.",
            "properties": {
              "profile": {"type": "string"},
              "url": {"type": "string"},
              "port": {"type": "integer"},
              "used_base_profile": {"type": "boolean"},
              "temp_dir": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null
              },
              "actions_done": {
                "anyOf": [
                  {"type": "integer"},
                  {"type": "null"}
                ],
                "default": null
              },
              "actions_total": {
                "anyOf": [
                  {"type": "integer"},
                  {"type": "null"}
                ],
                "default": null
              }
            },
            "required": ["profile", "url", "port", "used_base_profile"],
            "type": "object",
            "additionalProperties": false
          },
          "result": {
            "type": "null",
            "description": "Never set: an in-memory job is gone from the registry once it completes"
          },
          "error": {"type": "null", "description": "Never set, like `result`"}
        }
      }
    },
    "max_concurrent": {
      "type": "integer",
      "description": "INTERCEPTOR_MAX_CONCURRENT: the port pool's size"
    },
    "available": {"type": "integer", "description": "Slots free: `max_concurrent` - `active_count`"}
  }
}
```

## `MCP get_job`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message): GET /jobs/:jobId's body, or -- where that endpoint answers 404 -- an error object; `result.content[0].text` carries it as JSON.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app.mcp:get_job — do not edit",
  "additionalProperties": false,
  "properties": {
    "job_id": {"type": "string"}
  },
  "required": ["job_id"],
  "type": "object"
}
```

Result (`result.structuredContent`):

```json
{
  "oneOf": [
    {
      "description": "One in-flight capture: the shared `common.jobs` `JobBase` with the interceptor's own `metadata`. Completed captures are not retained",
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
        "phase": {
          "enum": ["cloning", "capturing", "actions", "cleaning_up"],
          "description": "`cloning` (slow path only, during the profile copy) → `capturing` → `cleaning_up`; `actions` while `page_script` / `actions` run (back to `capturing` when they finish)"
        },
        "metadata": {
          "$comment": "generated from app.InterceptorMetadata — do not edit",
          "description": "Interceptor-api's per-job metadata payload. Serialized into the\n``metadata`` field of ``JobBase`` for ``GET /jobs`` responses.",
          "properties": {
            "profile": {"type": "string"},
            "url": {"type": "string"},
            "port": {"type": "integer"},
            "used_base_profile": {"type": "boolean"},
            "temp_dir": {
              "anyOf": [
                {"type": "string"},
                {"type": "null"}
              ],
              "default": null
            },
            "actions_done": {
              "anyOf": [
                {"type": "integer"},
                {"type": "null"}
              ],
              "default": null
            },
            "actions_total": {
              "anyOf": [
                {"type": "integer"},
                {"type": "null"}
              ],
              "default": null
            }
          },
          "required": ["profile", "url", "port", "used_base_profile"],
          "type": "object",
          "additionalProperties": false
        },
        "result": {
          "type": "null",
          "description": "Never set: an in-memory job is gone from the registry once it completes"
        },
        "error": {"type": "null", "description": "Never set, like `result`"}
      }
    },
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["error"],
      "properties": {
        "error": {
          "type": "string",
          "description": "`no active job '<id>'`: unknown, or already finished"
        }
      }
    }
  ]
}
```
