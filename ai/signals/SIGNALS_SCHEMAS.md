# signals request and response schemas

Generated from [`signals.schemas.json`](signals.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`SIGNALS.md`](SIGNALS.md).

## `GET /health`

Response `200` (`application/json`) — every model is loaded and has run one warm-up inference:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "models", "cache_entries", "example_sets", "queues"],
  "properties": {
    "status": {"const": "ok"},
    "models": {
      "type": "object",
      "description": "Per lane",
      "required": ["pii", "embed", "injection", "language"],
      "propertyNames": {"enum": ["pii", "embed", "injection", "language"]},
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "state", "error", "load_s"],
        "properties": {
          "id": {
            "type": "string",
            "pattern": "^.+@[0-9a-f]{12}$",
            "description": "`<repo>@<first 12 hex of the revision>` (fastText: of its sha256)"
          },
          "state": {"enum": ["loading", "ready", "failed"]},
          "error": {
            "type": ["string", "null"],
            "description": "The exception type its load failed with"
          },
          "load_s": {
            "type": ["number", "null"],
            "minimum": 0,
            "description": "Seconds its load and warm-up took; null while loading"
          }
        }
      }
    },
    "cache_entries": {
      "type": "integer",
      "minimum": 0,
      "description": "Entries in the in-memory feature cache"
    },
    "example_sets": {
      "type": "object",
      "additionalProperties": false,
      "required": ["warming", "ready"],
      "description": "How many example sets are being embedded / are held",
      "properties": {
        "warming": {"type": "integer", "minimum": 0},
        "ready": {"type": "integer", "minimum": 0}
      }
    },
    "queues": {
      "type": "object",
      "description": "Jobs waiting for each ONNX lane's model (language runs inline)",
      "required": ["pii", "embed", "injection"],
      "propertyNames": {"enum": ["pii", "embed", "injection"]},
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["foreground", "background"],
        "properties": {
          "foreground": {"type": "integer", "minimum": 0},
          "background": {"type": "integer", "minimum": 0}
        }
      }
    }
  }
}
```

Response `503` (`application/json`) — `loading` before that (about 5-10 s after a start), or `failed` for good when a model failed to load (its `error` is the exception type):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "models", "cache_entries", "example_sets", "queues"],
  "properties": {
    "status": {"enum": ["loading", "failed"]},
    "models": {
      "type": "object",
      "description": "Per lane",
      "required": ["pii", "embed", "injection", "language"],
      "propertyNames": {"enum": ["pii", "embed", "injection", "language"]},
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "state", "error", "load_s"],
        "properties": {
          "id": {
            "type": "string",
            "pattern": "^.+@[0-9a-f]{12}$",
            "description": "`<repo>@<first 12 hex of the revision>` (fastText: of its sha256)"
          },
          "state": {"enum": ["loading", "ready", "failed"]},
          "error": {
            "type": ["string", "null"],
            "description": "The exception type its load failed with"
          },
          "load_s": {
            "type": ["number", "null"],
            "minimum": 0,
            "description": "Seconds its load and warm-up took; null while loading"
          }
        }
      }
    },
    "cache_entries": {
      "type": "integer",
      "minimum": 0,
      "description": "Entries in the in-memory feature cache"
    },
    "example_sets": {
      "type": "object",
      "additionalProperties": false,
      "required": ["warming", "ready"],
      "description": "How many example sets are being embedded / are held",
      "properties": {
        "warming": {"type": "integer", "minimum": 0},
        "ready": {"type": "integer", "minimum": 0}
      }
    },
    "queues": {
      "type": "object",
      "description": "Jobs waiting for each ONNX lane's model (language runs inline)",
      "required": ["pii", "embed", "injection"],
      "propertyNames": {"enum": ["pii", "embed", "injection"]},
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["foreground", "background"],
        "properties": {
          "foreground": {"type": "integer", "minimum": 0},
          "background": {"type": "integer", "minimum": 0}
        }
      }
    }
  }
}
```

## `GET /models`

Response `200` (`application/json`) — the pins and nothing to tune: no thresholds:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["models", "tasks", "statuses", "pii", "injection", "caps"],
  "properties": {
    "models": {
      "type": "object",
      "description": "Per lane: its pin and its load state",
      "required": ["pii", "embed", "injection", "language"],
      "propertyNames": {"enum": ["pii", "embed", "injection", "language"]},
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "repo", "revision", "url", "licence", "files", "tokens", "state"],
        "properties": {
          "id": {
            "type": "string",
            "pattern": "^.+@[0-9a-f]{12}$",
            "description": "`<repo>@<first 12 hex of the revision>` (fastText: of its sha256)"
          },
          "repo": {
            "type": "string",
            "description": "Hugging Face repo id, or (with `url`) the display id"
          },
          "revision": {
            "type": ["string", "null"],
            "pattern": "^[0-9a-f]{40}$",
            "description": "The commit sha files are fetched at (Hugging Face only)"
          },
          "url": {
            "type": ["string", "null"],
            "description": "A direct download URL instead of Hugging Face (one file)"
          },
          "licence": {"type": "string", "description": "SPDX id"},
          "files": {
            "type": "object",
            "description": "Relative path -> sha256 of every file the lane needs",
            "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
          },
          "tokens": {
            "type": "object",
            "description": "Token caps (request params are clamped to `max`)",
            "additionalProperties": {"type": "integer"}
          },
          "state": {"enum": ["loading", "ready", "failed"]}
        }
      }
    },
    "tasks": {
      "type": "object",
      "additionalProperties": false,
      "required": ["pii", "nearest", "injection", "language"],
      "description": "Which model lane serves each task (`common.signals.TASK_MODELS`)",
      "properties": {
        "pii": {"const": "pii"},
        "nearest": {"const": "embed"},
        "injection": {"const": "injection"},
        "language": {"const": "language"}
      }
    },
    "statuses": {
      "type": "array",
      "items": {"enum": ["ok", "partial", "deadline", "skipped", "warming", "error", "unavailable"]}
    },
    "pii": {
      "type": "object",
      "additionalProperties": false,
      "required": ["vocabulary", "sensitive", "labels", "other_labels"],
      "properties": {
        "vocabulary": {
          "type": "array",
          "items": {
            "enum": [
              "email",
              "phone",
              "street_address",
              "gov_id",
              "card_number",
              "bank_account",
              "account_number",
              "date_of_birth",
              "person",
              "location",
              "other"
            ]
          },
          "description": "`common.pii.ENTITY_TYPES`"
        },
        "sensitive": {
          "type": "array",
          "items": {
            "enum": [
              "email",
              "phone",
              "street_address",
              "gov_id",
              "card_number",
              "bank_account",
              "account_number",
              "date_of_birth",
              "person",
              "location",
              "other"
            ]
          },
          "description": "`common.pii.SENSITIVE_TYPES`"
        },
        "labels": {
          "type": "object",
          "description": "How the NER's labels map onto the vocabulary",
          "additionalProperties": {"type": "string"}
        },
        "other_labels": {"const": "other", "description": "The type of every label not in `labels`"}
      }
    },
    "injection": {
      "type": "object",
      "additionalProperties": false,
      "required": ["windows"],
      "properties": {
        "windows": {
          "type": "array",
          "items": {"const": "head_tail"}
        }
      }
    },
    "caps": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "max_text_chars",
        "max_request_chars",
        "max_texts",
        "max_examples",
        "max_example_chars",
        "max_tokens",
        "max_k",
        "max_deadline_ms",
        "max_body_bytes"
      ],
      "description": "The request caps (past them: a 413, or a 422 for max_tokens / max_k / max_deadline_ms)",
      "properties": {
        "max_text_chars": {"type": "integer", "minimum": 1},
        "max_request_chars": {"type": "integer", "minimum": 1},
        "max_texts": {"type": "integer", "minimum": 1},
        "max_examples": {"type": "integer", "minimum": 1},
        "max_example_chars": {"type": "integer", "minimum": 1},
        "max_tokens": {"type": "integer", "minimum": 1},
        "max_k": {"type": "integer", "minimum": 1},
        "max_deadline_ms": {"type": "integer", "minimum": 1},
        "max_body_bytes": {"type": "integer", "minimum": 1}
      }
    }
  }
}
```

## `GET /metrics`

Response `200` (`text/plain`) — Prometheus text (SIGNALS.md § GET /metrics).

## `POST /signals`

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "description": "Parsed strictly by `common.signals.SignalsRequest.from_json` (a 422 names the field and the rule, never a value). Not expressible here, and also a 422: two texts with one id, a task naming a text id no text has, a task named twice (results are keyed by task).",
  "properties": {
    "request_id": {
      "type": ["string", "null"],
      "minLength": 1,
      "maxLength": 128,
      "description": "For the sidecar's logs only (route-policy sends the decision id)"
    },
    "deadline_ms": {
      "type": ["number", "null"],
      "exclusiveMinimum": 0,
      "maximum": 60000,
      "description": "The answer goes out by then, measured from the parsed request. Omitted / `null`: no cut, every task runs to the end"
    },
    "texts": {
      "type": "array",
      "maxItems": 64,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "text"],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "Caller-chosen, unique in the request (route-policy: `u<k>` = the user text of user message k, `t<k>` = the k-th tool result)"
          },
          "text": {
            "type": "string",
            "maxLength": 20000,
            "description": "Over 20,000 characters (or over 200,000 for all texts together) is a 413"
          }
        }
      },
      "description": "Every text to scan, newest first; more than 64 is a 413"
    },
    "tasks": {
      "type": "array",
      "items": {
        "oneOf": [
          {
            "type": "object",
            "additionalProperties": false,
            "required": ["task"],
            "properties": {
              "task": {"const": "pii"},
              "texts": {
                "type": "array",
                "uniqueItems": true,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": "The ids of the texts this task scans, newest first; each names a text of the request"
              }
            }
          },
          {
            "type": "object",
            "additionalProperties": false,
            "required": ["task", "examples"],
            "properties": {
              "task": {"const": "nearest"},
              "texts": {
                "type": "array",
                "uniqueItems": true,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": "The ids of the texts this task scans, newest first; each names a text of the request"
              },
              "max_tokens": {
                "type": ["integer", "null"],
                "minimum": 1,
                "maximum": 512,
                "description": "Default 256"
              },
              "k": {
                "type": ["integer", "null"],
                "minimum": 1,
                "maximum": 50,
                "description": "Default 5"
              },
              "examples": {
                "type": "object",
                "additionalProperties": false,
                "required": ["fingerprint", "items"],
                "properties": {
                  "fingerprint": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "The sidecar's cache key for the embedded set"
                  },
                  "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1000,
                    "description": "Ids unique; more than 1,000 examples is a 413",
                    "items": {
                      "type": "object",
                      "additionalProperties": false,
                      "required": ["id", "route", "text"],
                      "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "route": {"type": "string", "minLength": 1, "maxLength": 128},
                        "text": {
                          "type": "string",
                          "minLength": 1,
                          "maxLength": 300,
                          "description": "Over 300 characters is a 413"
                        }
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
            "required": ["task"],
            "properties": {
              "task": {"const": "injection"},
              "texts": {
                "type": "array",
                "uniqueItems": true,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": "The ids of the texts this task scans, newest first; each names a text of the request"
              },
              "max_tokens": {
                "type": ["integer", "null"],
                "minimum": 1,
                "maximum": 512,
                "description": "Per window, default 256"
              },
              "windows": {"enum": ["head_tail", null], "description": "`head_tail`, the only mode"}
            }
          },
          {
            "type": "object",
            "additionalProperties": false,
            "required": ["task"],
            "properties": {
              "task": {"const": "language"},
              "texts": {
                "type": "array",
                "uniqueItems": true,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": "The ids of the texts this task scans, newest first; each names a text of the request"
              }
            }
          }
        ]
      },
      "description": "Each task at most once; each takes only its own params"
    }
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["latency_ms", "models", "results"],
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 128,
      "description": "The request's `request_id`; absent when it sent none"
    },
    "latency_ms": {"type": "number", "minimum": 0, "description": "The sidecar's own time"},
    "models": {
      "type": "object",
      "description": "The model behind each task, keyed per `common.signals.TASK_MODELS` (`nearest` -> `embed`)",
      "required": ["pii", "embed", "injection", "language"],
      "propertyNames": {"enum": ["pii", "embed", "injection", "language"]},
      "additionalProperties": {
        "type": "string",
        "pattern": "^.+@[0-9a-f]{12}$",
        "description": "`<repo>@<first 12 hex of the revision>` (fastText: of its sha256)"
      }
    },
    "results": {
      "type": "object",
      "additionalProperties": false,
      "description": "One result per task the request named, keyed by task. `status` -- ok: every text done; partial: some (a text cut by the deadline, or one that failed -- then `error` names the first failure's type); deadline: still running at the deadline; skipped: queued past it, never started; warming: the example set is being embedded; error: it failed, no text has features; unavailable: its model is not loaded. `ms` -- The time from the request's arrival until the task's last text was ready (until the cut, for `partial`); `null` when nothing came back. `error` -- For `error` / `unavailable`, and a `partial` some of whose texts failed: the first failure's exception type, or `loading`; never text. Features carry counts, scores and ids, never text, spans or offsets.",
      "properties": {
        "pii": {
          "type": "object",
          "additionalProperties": false,
          "required": ["status", "ms", "texts"],
          "properties": {
            "status": {
              "enum": ["ok", "partial", "deadline", "skipped", "warming", "error", "unavailable"]
            },
            "ms": {"type": ["number", "null"], "minimum": 0},
            "texts": {
              "type": "object",
              "description": "Features per text id it finished",
              "propertyNames": {"type": "string", "minLength": 1, "maxLength": 128},
              "additionalProperties": {
                "type": "object",
                "additionalProperties": false,
                "required": ["entities", "complete", "cached"],
                "properties": {
                  "entities": {
                    "type": "object",
                    "description": "Entity type (`common.pii.ENTITY_TYPES`) -> how many the NER found in the text, and its best score",
                    "propertyNames": {
                      "enum": [
                        "email",
                        "phone",
                        "street_address",
                        "gov_id",
                        "card_number",
                        "bank_account",
                        "account_number",
                        "date_of_birth",
                        "person",
                        "location",
                        "other"
                      ]
                    },
                    "additionalProperties": {
                      "type": "object",
                      "additionalProperties": false,
                      "required": ["count"],
                      "properties": {
                        "count": {"type": "integer", "minimum": 0},
                        "max_score": {
                          "type": "number",
                          "minimum": 0,
                          "maximum": 1,
                          "description": "Absent when the model gives none"
                        }
                      }
                    }
                  },
                  "complete": {
                    "type": "boolean",
                    "description": "False when only part of the text was scanned by the deadline"
                  },
                  "cached": {
                    "type": "boolean",
                    "description": "The sidecar answered from its cache"
                  }
                }
              }
            },
            "error": {"type": "string", "minLength": 1, "maxLength": 200}
          }
        },
        "nearest": {
          "type": "object",
          "additionalProperties": false,
          "required": ["status", "ms", "texts"],
          "properties": {
            "status": {
              "enum": ["ok", "partial", "deadline", "skipped", "warming", "error", "unavailable"]
            },
            "ms": {"type": ["number", "null"], "minimum": 0},
            "texts": {
              "type": "object",
              "description": "Features per text id it finished",
              "propertyNames": {"type": "string", "minLength": 1, "maxLength": 128},
              "additionalProperties": {
                "type": "object",
                "additionalProperties": false,
                "required": ["neighbours", "routes", "cached"],
                "properties": {
                  "neighbours": {
                    "type": "array",
                    "maxItems": 50,
                    "description": "The k nearest examples, most similar first (example ids, never example text)",
                    "items": {
                      "type": "object",
                      "additionalProperties": false,
                      "required": ["id", "route", "similarity"],
                      "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "route": {"type": "string", "minLength": 1, "maxLength": 128},
                        "similarity": {
                          "type": "number",
                          "minimum": -1,
                          "maximum": 1,
                          "description": "Cosine similarity"
                        }
                      }
                    }
                  },
                  "routes": {
                    "type": "object",
                    "description": "Per route among the k nearest",
                    "propertyNames": {"type": "string", "minLength": 1, "maxLength": 128},
                    "additionalProperties": {
                      "type": "object",
                      "additionalProperties": false,
                      "required": ["share", "best", "count"],
                      "properties": {
                        "share": {
                          "type": "number",
                          "minimum": 0,
                          "maximum": 1,
                          "description": "The route's part of the top-k similarity mass (what route-policy's min_share reads)"
                        },
                        "best": {
                          "type": "number",
                          "minimum": -1,
                          "maximum": 1,
                          "description": "Its best similarity"
                        },
                        "count": {
                          "type": "integer",
                          "minimum": 0,
                          "description": "How many of the k it holds"
                        }
                      }
                    }
                  },
                  "cached": {
                    "type": "boolean",
                    "description": "The sidecar answered from its cache"
                  }
                }
              }
            },
            "error": {"type": "string", "minLength": 1, "maxLength": 200}
          }
        },
        "injection": {
          "type": "object",
          "additionalProperties": false,
          "required": ["status", "ms", "texts"],
          "properties": {
            "status": {
              "enum": ["ok", "partial", "deadline", "skipped", "warming", "error", "unavailable"]
            },
            "ms": {"type": ["number", "null"], "minimum": 0},
            "texts": {
              "type": "object",
              "description": "Features per text id it finished",
              "propertyNames": {"type": "string", "minLength": 1, "maxLength": 128},
              "additionalProperties": {
                "type": "object",
                "additionalProperties": false,
                "required": ["score", "windows", "cached"],
                "properties": {
                  "score": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "The highest injection probability, P(jailbreak), over the windows scored"
                  },
                  "windows": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How many windows were scored"
                  },
                  "cached": {
                    "type": "boolean",
                    "description": "The sidecar answered from its cache"
                  }
                }
              }
            },
            "error": {"type": "string", "minLength": 1, "maxLength": 200}
          }
        },
        "language": {
          "type": "object",
          "additionalProperties": false,
          "required": ["status", "ms", "texts"],
          "properties": {
            "status": {
              "enum": ["ok", "partial", "deadline", "skipped", "warming", "error", "unavailable"]
            },
            "ms": {"type": ["number", "null"], "minimum": 0},
            "texts": {
              "type": "object",
              "description": "Features per text id it finished",
              "propertyNames": {"type": "string", "minLength": 1, "maxLength": 128},
              "additionalProperties": {
                "type": "object",
                "additionalProperties": false,
                "required": ["language", "confidence", "cached"],
                "properties": {
                  "language": {
                    "type": "string",
                    "pattern": "^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8}){0,3}$",
                    "description": "A language code as the model labels it (`en`, `es`, `zh` ...); `und` with confidence 0 for a text with no letters"
                  },
                  "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                  "cached": {
                    "type": "boolean",
                    "description": "The sidecar answered from its cache"
                  }
                }
              }
            },
            "error": {"type": "string", "minLength": 1, "maxLength": 200}
          }
        }
      }
    }
  }
}
```
