# searxng request and response schemas

Generated from [`searxng.schemas.json`](searxng.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`SEARXNG.md`](SEARXNG.md).

## `GET /healthz`

Response `200` (`text/plain`) — `OK`.

## `GET /search`

With `format=json` (allowed by `search.formats` in settings.yml; a format not listed there is a 403); without it the HTML results page. `q` is required: none is a 400. Open WebUI reads `results[].url`, `title` and `content`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": [
    "query",
    "results",
    "answers",
    "corrections",
    "infoboxes",
    "suggestions",
    "unresponsive_engines"
  ],
  "properties": {
    "query": {"type": "string"},
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "description": "A MainResult (or a legacy dict result) as `as_dict()` gives it",
        "required": ["url", "title", "content", "engine"],
        "properties": {
          "url": {"type": ["string", "null"]},
          "title": {"type": "string"},
          "content": {"type": "string", "description": "The snippet"},
          "engine": {"type": "string", "description": "The engine that returned it first"},
          "engines": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every engine that returned it"
          },
          "score": {"type": "number", "description": "The merged rank; `results` is sorted by it"},
          "category": {"type": "string"},
          "publishedDate": {
            "type": ["string", "null"],
            "description": "ISO 8601, when the engine gives one"
          },
          "thumbnail": {"type": "string"},
          "img_src": {"type": "string"},
          "template": {"type": "string", "description": "`default.html`, `images.html`, …"},
          "parsed_url": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "urllib's six parts"
          },
          "positions": {"description": "Its position in each engine's list"}
        }
      }
    },
    "answers": {
      "type": "array",
      "items": {"type": "object", "additionalProperties": true}
    },
    "corrections": {
      "type": "array",
      "items": {"type": "string"}
    },
    "infoboxes": {
      "type": "array",
      "items": {"type": "object", "additionalProperties": true}
    },
    "suggestions": {
      "type": "array",
      "items": {"type": "string"}
    },
    "unresponsive_engines": {
      "type": "array",
      "description": "Engines that failed or timed out (`outgoing.max_request_timeout`), sorted",
      "items": {
        "type": "array",
        "prefixItems": [
          {"type": "string", "description": "The engine"},
          {"type": "string", "description": "Why, translated (`Suspended: …` when suspended)"}
        ],
        "minItems": 2,
        "maxItems": 2
      }
    }
  }
}
```

Response `400` (`application/json`) — no `q` (`No query`), or a bad parameter:

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["error"],
  "properties": {
    "error": {"type": "string"}
  }
}
```

Response `500` (`application/json`) — `search error`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["error"],
  "properties": {
    "error": {"type": "string"}
  }
}
```

## `GET /`

Response `200` (`text/html`) — the search page (a request carrying `q` is redirected to /search, 308).

## `GET /config`

The running instance's settings. It does not list `search.formats`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["categories", "engines", "plugins", "version"],
  "properties": {
    "categories": {
      "type": "array",
      "items": {"type": "string"}
    },
    "engines": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["name", "categories", "enabled"],
        "properties": {
          "name": {"type": "string"},
          "categories": {
            "type": "array",
            "items": {"type": "string"}
          },
          "shortcut": {"type": "string"},
          "enabled": {
            "type": "boolean",
            "description": "false for `duckduckgo` here (settings.yml)"
          },
          "paging": {"type": "boolean"},
          "language_support": {"type": "boolean"},
          "safesearch": {"type": "boolean"},
          "time_range_support": {"type": "boolean"},
          "timeout": {"type": ["number", "null"]}
        }
      }
    },
    "plugins": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "properties": {
          "name": {"type": "string"},
          "enabled": {"type": "boolean"}
        }
      }
    },
    "instance_name": {"type": "string"},
    "default_locale": {"type": "string"},
    "autocomplete": {"type": "string"},
    "safe_search": {"type": "integer"},
    "default_theme": {"type": "string"},
    "version": {"type": "string"},
    "limiter": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "enabled": {"type": "boolean", "description": "false here (`server.limiter`)"}
      }
    },
    "public_instance": {"type": "boolean"}
  }
}
```

## `GET /stats`

HTML only (there is no JSON variant); `/stats/errors` answers the same data as JSON.

Response `200` (`text/html`) — the engine statistics page.
