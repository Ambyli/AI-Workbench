# detector request and response schemas

Generated from [`detector.schemas.json`](detector.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`DETECTOR.md`](DETECTOR.md).

## `GET /v1/detector/health`

Response `200` (`application/json`) — the weights are loaded:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "status",
    "model",
    "family",
    "device",
    "configured_device",
    "loaded",
    "error",
    "default_threshold",
    "max_image_side",
    "max_labels"
  ],
  "properties": {
    "status": {"const": "ok"},
    "model": {"type": "string", "description": "DETECTOR_MODEL"},
    "family": {
      "type": "string",
      "description": "The implementation family the model id resolved to"
    },
    "device": {"enum": ["cuda", "cpu"], "description": "What is actually in use"},
    "configured_device": {
      "type": "string",
      "description": "DETECTOR_DEVICE as configured; `device` falls back to `cpu` when torch sees no GPU"
    },
    "loaded": {"const": true},
    "error": {"type": "null"},
    "default_threshold": {"type": "number", "description": "DETECTOR_DEFAULT_THRESHOLD"},
    "max_image_side": {"type": "integer", "minimum": 64, "description": "DETECTOR_MAX_IMAGE_SIDE"},
    "max_labels": {"type": "integer", "minimum": 1, "description": "DETECTOR_MAX_LABELS"}
  }
}
```

Response `503` (`application/json`) — the weights are not resident, which only follows a failed model load (a bad DETECTOR_MODEL, a Grounding DINO id, no disk): the lifespan loads the weights before uvicorn accepts a connection, so during the first-start load there is no answer at all (the edge answers its own 503 + Retry-After):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "status",
    "model",
    "family",
    "device",
    "configured_device",
    "loaded",
    "error",
    "default_threshold",
    "max_image_side",
    "max_labels"
  ],
  "properties": {
    "status": {"const": "degraded"},
    "model": {"type": "string", "description": "DETECTOR_MODEL"},
    "family": {"type": "null"},
    "device": {"enum": ["cuda", "cpu"], "description": "What is actually in use"},
    "configured_device": {
      "type": "string",
      "description": "DETECTOR_DEVICE as configured; `device` falls back to `cpu` when torch sees no GPU"
    },
    "loaded": {"const": false},
    "error": {"type": "string", "description": "The loader's error, `<ExceptionType>: <message>`"},
    "default_threshold": {"type": "number", "description": "DETECTOR_DEFAULT_THRESHOLD"},
    "max_image_side": {"type": "integer", "minimum": 64, "description": "DETECTOR_MAX_IMAGE_SIDE"},
    "max_labels": {"type": "integer", "minimum": 1, "description": "DETECTOR_MAX_LABELS"}
  }
}
```

## `POST /v1/detector/detect`

Two spellings of the same request, chosen by `Content-Type`; both normalise to the same params. Every error (400 bad labels / threshold, an undecodable, too small or unsupported image, a blocked URL, another Content-Type; 502 an image URL that could not be fetched; 503 the model is not loaded) is the generic `{"detail": "<message>"}`: the body is parsed by hand, so there is no FastAPI 422.

Request body (`multipart/form-data`) — form fields, all text but `file`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["file", "labels"],
  "properties": {
    "file": {
      "type": "string",
      "contentMediaType": "image/*",
      "description": "The image (JPEG / PNG / HEIC / HEIF / AVIF / anything Pillow opens), at least 32 px on its short side"
    },
    "labels": {
      "type": "string",
      "description": "A JSON array (`[\"tree\",\"house\"]`) or a comma list (`tree, house`) or one name; capped at DETECTOR_MAX_LABELS"
    },
    "threshold": {
      "type": "string",
      "description": "Confidence floor 0-1. Default DETECTOR_DEFAULT_THRESHOLD (0.25)"
    },
    "max_per_label": {
      "type": "string",
      "description": "Keep at most this many boxes per label (an integer >= 1), best first"
    }
  }
}
```

Request body (`application/json`) — `image.type: url` is fetched after an SSRF check (`common.net.validate_url`):

```json
{
  "$comment": "generated from models.DetectRequest — do not edit",
  "description": "JSON body for ``POST /detect``.\n\nThe multipart spelling carries the same four fields as form values, with\n`file` in place of `image`.",
  "properties": {
    "image": {
      "description": "An image supplied as base64 bytes or a remote URL.\n\nSame shape as the classifier's ``DocumentInput``, on purpose: a caller\nthat already builds one of those can send it here unchanged. URLs are\nSSRF-checked (``common.net.validate_url``) before any fetch — this\ncontainer sits on ``ai_shared``, so an unchecked URL input is a request\nto proxy into the private network.",
      "properties": {
        "data": {"description": "Base64-encoded image bytes, or an http(s) URL.", "type": "string"},
        "type": {
          "description": "Whether `data` is 'base64' or 'url'.",
          "enum": ["base64", "url"],
          "type": "string"
        }
      },
      "required": ["data", "type"],
      "type": "object",
      "additionalProperties": true
    },
    "labels": {
      "anyOf": [
        {"type": "string"},
        {
          "items": {"type": "string"},
          "type": "array"
        }
      ],
      "description": "Free-text things to look for, e.g. [\"tree\", \"swimming pool\"] or the comma string \"tree, swimming pool\". Each is embedded as its own query, so cost is linear in the count and the list is capped at DETECTOR_MAX_LABELS."
    },
    "threshold": {
      "anyOf": [
        {"maximum": 1.0, "minimum": 0.0, "type": "number"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Confidence floor. Omit for DETECTOR_DEFAULT_THRESHOLD (0.25). Scores are not calibrated probabilities — treat this as a knob to turn, not a percentage."
    },
    "max_per_label": {
      "anyOf": [
        {"minimum": 1, "type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Keep at most this many boxes per label, highest score first. Omit to keep every box above the threshold."
    }
  },
  "required": ["image", "labels"],
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "model",
    "family",
    "device",
    "elapsed_ms",
    "threshold",
    "image",
    "labels",
    "counts",
    "by_label",
    "detections"
  ],
  "properties": {
    "model": {"type": "string", "description": "DETECTOR_MODEL"},
    "family": {
      "type": "string",
      "description": "The implementation family the model id resolved to"
    },
    "device": {"enum": ["cuda", "cpu"], "description": "What is actually in use"},
    "elapsed_ms": {
      "type": "number",
      "minimum": 0,
      "description": "Inference only, not the round trip"
    },
    "threshold": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "The threshold actually applied"
    },
    "image": {
      "type": "object",
      "additionalProperties": false,
      "required": ["width", "height"],
      "description": "The ORIGINAL size (after EXIF transpose) every box is expressed in",
      "properties": {
        "width": {"type": "integer", "minimum": 32},
        "height": {"type": "integer", "minimum": 32}
      }
    },
    "labels": {
      "type": "array",
      "minItems": 1,
      "uniqueItems": true,
      "items": {"type": "string", "minLength": 1},
      "description": "The requested labels, duplicates collapsed, in the caller's order and spelling"
    },
    "counts": {
      "type": "object",
      "description": "Boxes per requested label",
      "additionalProperties": {"type": "integer", "minimum": 0}
    },
    "by_label": {
      "type": "object",
      "description": "Every requested label gets a key, an empty list when it found nothing; best first",
      "additionalProperties": {
        "type": "array",
        "items": {
          "type": "object",
          "additionalProperties": false,
          "required": ["label", "score", "box"],
          "properties": {
            "label": {"type": "string", "description": "The caller's label string, echoed exactly"},
            "score": {
              "type": "number",
              "minimum": 0,
              "maximum": 1,
              "description": "Model confidence: NOT a calibrated probability (4 decimals)"
            },
            "box": {
              "type": "array",
              "minItems": 4,
              "maxItems": 4,
              "items": {"type": "number", "minimum": 0},
              "description": "`[x1, y1, x2, y2]`, top-left origin, ORIGINAL image pixels, clipped to the image (a pixel tenth)"
            }
          }
        }
      }
    },
    "detections": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["label", "score", "box"],
        "properties": {
          "label": {"type": "string", "description": "The caller's label string, echoed exactly"},
          "score": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Model confidence: NOT a calibrated probability (4 decimals)"
          },
          "box": {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {"type": "number", "minimum": 0},
            "description": "`[x1, y1, x2, y2]`, top-left origin, ORIGINAL image pixels, clipped to the image (a pixel tenth)"
          }
        }
      },
      "description": "The same boxes as `by_label`, flat, in score order"
    }
  }
}
```

## `GET /v1/detector/metrics`

Response `200` (`text/plain`) — Prometheus text (prometheus-fastapi-instrumentator's HTTP metrics).

## `MCP tools/list`

The JSON-RPC response's `result` (streamable HTTP at `/mcp/`: one SSE `data:` message when the request accepts `text/event-stream`).

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
          "name": {"const": "detect_objects"},
          "description": {
            "type": "string",
            "description": "The tool's docstring summary: what the model reads"
          },
          "inputSchema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema: the arguments of `MCP detect_objects`"
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

## `MCP detect_objects`

The JSON-RPC response's `result.structuredContent` (one SSE `data:` message); `result.content[0].text` carries the same string and `isError` is false, errors included.

Arguments (`params.arguments` of `tools/call`):

```json
{
  "$comment": "generated from app.mcp:detect_objects — do not edit",
  "additionalProperties": false,
  "properties": {
    "image": {
      "type": "string",
      "description": "An http(s) URL to the image, or its base64-encoded bytes.\nPrefer a URL — base64 of a photo is a very large tool argument."
    },
    "labels": {
      "items": {"type": "string"},
      "type": "array",
      "description": "What to look for, e.g. [\"tree\", \"swimming pool\"]. Cost is\nlinear in the number of labels."
    },
    "threshold": {
      "anyOf": [
        {"type": "number"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Confidence floor 0-1. Omit for the service default (0.25).\nRaise it when you get spurious boxes, lower it when you get none."
    },
    "max_per_label": {
      "anyOf": [
        {"type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Keep at most this many boxes per label, best first."
    }
  },
  "required": ["image", "labels"],
  "type": "object"
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
      "contentMediaType": "application/json",
      "description": "The tool's return value: JSON text (FastMCP wraps a `str` return as `{\"result\": …}`), whose content `contentSchema` describes",
      "contentSchema": {
        "oneOf": [
          {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "model",
              "family",
              "device",
              "elapsed_ms",
              "threshold",
              "image",
              "labels",
              "counts",
              "by_label"
            ],
            "description": "The POST /detect body minus the flat `detections` list",
            "properties": {
              "model": {"type": "string"},
              "family": {"type": "string"},
              "device": {"enum": ["cuda", "cpu"], "description": "What is actually in use"},
              "elapsed_ms": {
                "type": "number",
                "minimum": 0,
                "description": "Inference only, not the round trip"
              },
              "threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "The threshold actually applied"
              },
              "image": {
                "type": "object",
                "additionalProperties": false,
                "required": ["width", "height"],
                "description": "The ORIGINAL size (after EXIF transpose) every box is expressed in",
                "properties": {
                  "width": {"type": "integer", "minimum": 32},
                  "height": {"type": "integer", "minimum": 32}
                }
              },
              "labels": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": true,
                "items": {"type": "string", "minLength": 1},
                "description": "The requested labels, duplicates collapsed, in the caller's order and spelling"
              },
              "counts": {
                "type": "object",
                "description": "Boxes per requested label",
                "additionalProperties": {"type": "integer", "minimum": 0}
              },
              "by_label": {
                "type": "object",
                "description": "Every requested label gets a key, an empty list when it found nothing; best first",
                "additionalProperties": {
                  "type": "array",
                  "items": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["label", "score", "box"],
                    "properties": {
                      "label": {
                        "type": "string",
                        "description": "The caller's label string, echoed exactly"
                      },
                      "score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "Model confidence: NOT a calibrated probability (4 decimals)"
                      },
                      "box": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 4,
                        "items": {"type": "number", "minimum": 0},
                        "description": "`[x1, y1, x2, y2]`, top-left origin, ORIGINAL image pixels, clipped to the image (a pixel tenth)"
                      }
                    }
                  }
                }
              }
            }
          },
          {
            "type": "object",
            "additionalProperties": false,
            "required": ["error", "status"],
            "description": "Errors are returned as data, not as a JSON-RPC error",
            "properties": {
              "error": {"type": "string"},
              "status": {
                "enum": [400, 502, 503],
                "description": "The status the same failure gets on POST /detect: 400 for bad `labels` / `threshold` / `max_per_label`, a blocked URL or an undecodable / too small / unsupported image; 502 an image URL that could not be fetched; 503 the model is not loaded"
              }
            }
          }
        ]
      }
    }
  }
}
```
