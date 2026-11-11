# classifier request and response schemas

Generated from [`classifier.schemas.json`](classifier.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`API.md`](API.md).

## `GET /v1/classifier/health`

Liveness only.

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

## `GET /v1/classifier/models`

Never a key value.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["default", "models"],
  "properties": {
    "default": {"type": "string", "description": "The registry default"},
    "models": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One models.yaml entry (`llm.models.ModelSpec.public`)",
        "additionalProperties": false,
        "required": [
          "id",
          "description",
          "served_name",
          "endpoint",
          "authenticated",
          "available",
          "unavailable_reason",
          "vision",
          "max_images_per_prompt",
          "supports",
          "reasoning",
          "text_char_budget",
          "max_tokens",
          "timeout_s",
          "max_concurrent",
          "in_flight",
          "confidence"
        ],
        "properties": {
          "id": {
            "type": "string",
            "description": "What a request's `model` and a criterion's `options.model` name"
          },
          "description": {"type": "string"},
          "served_name": {"type": "string", "description": "Sent as the body's `model`"},
          "endpoint": {
            "type": "object",
            "description": "Where a request's pages and text GO",
            "additionalProperties": false,
            "required": ["scheme", "host", "port"],
            "properties": {
              "scheme": {"type": "string"},
              "host": {"type": ["string", "null"]},
              "port": {"type": ["integer", "null"]}
            }
          },
          "authenticated": {
            "type": "boolean",
            "description": "Whether a key is sent (never the key)"
          },
          "available": {"type": "boolean"},
          "unavailable_reason": {"type": ["string", "null"]},
          "vision": {"type": "boolean"},
          "max_images_per_prompt": {"type": "integer", "minimum": 0},
          "supports": {
            "type": "object",
            "additionalProperties": false,
            "required": ["boxes", "references", "contrastive_references", "text_only_scoring"],
            "properties": {
              "boxes": {"type": "boolean"},
              "references": {"type": "boolean"},
              "contrastive_references": {"type": "boolean"},
              "text_only_scoring": {"type": "boolean"}
            }
          },
          "reasoning": {
            "type": "object",
            "additionalProperties": false,
            "required": ["style"],
            "properties": {
              "style": {"enum": ["chat_template_kwargs", "system_line", "none"]},
              "key": {"type": "string"},
              "value": {"type": "string"}
            }
          },
          "text_char_budget": {"type": "integer"},
          "max_tokens": {
            "type": "object",
            "additionalProperties": false,
            "required": ["score", "box", "select", "describe"],
            "properties": {
              "score": {"type": "integer"},
              "box": {"type": "integer"},
              "select": {"type": "integer"},
              "describe": {"type": "integer"}
            }
          },
          "timeout_s": {"type": "number"},
          "max_concurrent": {"type": "integer", "minimum": 1},
          "in_flight": {"type": "integer", "minimum": 0, "description": "Requests in flight now"},
          "confidence": {
            "type": "object",
            "additionalProperties": false,
            "required": ["method", "headline", "calibrated"],
            "properties": {
              "method": {"enum": ["logprobs", "sampling", "stated"]},
              "headline": {"enum": ["stated", "calibrated"]},
              "calibrated": {"type": "boolean", "description": "A calibration is fitted"}
            }
          }
        }
      },
      "description": "In models.yaml order"
    }
  }
}
```

## `GET /v1/classifier/criterion-types`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "description": "Every criterion type's options (JSON Schema, defaults, caps) and the shape of its `detail` — the live answer for THIS container",
  "additionalProperties": false,
  "required": [
    "shared_fields",
    "aggregate",
    "types",
    "reference",
    "box_loop",
    "model",
    "detector_configured",
    "aggregate_detail"
  ],
  "properties": {
    "shared_fields": {
      "type": "object",
      "additionalProperties": {"type": "string"}
    },
    "aggregate": {
      "type": "object",
      "additionalProperties": false,
      "required": ["levels", "rules", "defaults", "excluded", "text_scope_document"],
      "properties": {
        "levels": {
          "type": "object",
          "additionalProperties": {"type": "string"}
        },
        "rules": {
          "type": "object",
          "additionalProperties": {"type": "string"}
        },
        "defaults": {
          "type": "object",
          "additionalProperties": {
            "type": "object",
            "additionalProperties": false,
            "required": ["pages", "documents"],
            "properties": {
              "pages": {"type": "string"},
              "documents": {"type": "string"}
            }
          }
        },
        "excluded": {"type": "string"},
        "text_scope_document": {"type": "string"}
      }
    },
    "types": {
      "type": "object",
      "additionalProperties": false,
      "required": ["llm", "text", "detector", "cv"],
      "properties": {
        "llm": {
          "type": "object",
          "additionalProperties": false,
          "required": ["options_schema", "defaults", "caps", "result"],
          "properties": {
            "options_schema": {
              "type": "object",
              "additionalProperties": true,
              "description": "A JSON Schema: the type's options model (pydantic)"
            },
            "defaults": {
              "type": "object",
              "additionalProperties": true,
              "description": "The type's options as resolved for a criterion named `<name>`"
            },
            "caps": {
              "type": "object",
              "additionalProperties": {"type": "number"},
              "description": "The server's caps"
            },
            "result": {
              "type": "object",
              "description": "A type's `detail` declaration (`analysis.result_specs.ResultSpec.as_dict`)",
              "additionalProperties": false,
              "required": ["metric", "metric_from", "fields", "notes"],
              "properties": {
                "metric": {"type": "string"},
                "metric_from": {"enum": ["detail", "measurements", "score"]},
                "fields": {
                  "type": "object",
                  "additionalProperties": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["kind", "description"],
                    "properties": {
                      "kind": {
                        "enum": ["number", "integer", "string", "boolean", "array", "object"]
                      },
                      "description": {"type": "string"},
                      "when": {"type": "string", "description": "Present only in this case"},
                      "stable": {"const": true},
                      "nullable": {"const": true}
                    }
                  }
                },
                "notes": {
                  "type": "array",
                  "items": {"type": "string"}
                }
              }
            }
          }
        },
        "text": {
          "type": "object",
          "additionalProperties": false,
          "required": ["options_schema", "defaults", "caps", "result"],
          "properties": {
            "options_schema": {
              "type": "object",
              "additionalProperties": true,
              "description": "A JSON Schema: the type's options model (pydantic)"
            },
            "defaults": {
              "type": "object",
              "additionalProperties": true,
              "description": "The type's options as resolved for a criterion named `<name>`"
            },
            "caps": {
              "type": "object",
              "additionalProperties": {"type": "number"},
              "description": "The server's caps"
            },
            "result": {
              "type": "object",
              "description": "A type's `detail` declaration (`analysis.result_specs.ResultSpec.as_dict`)",
              "additionalProperties": false,
              "required": ["metric", "metric_from", "fields", "notes"],
              "properties": {
                "metric": {"type": "string"},
                "metric_from": {"enum": ["detail", "measurements", "score"]},
                "fields": {
                  "type": "object",
                  "additionalProperties": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["kind", "description"],
                    "properties": {
                      "kind": {
                        "enum": ["number", "integer", "string", "boolean", "array", "object"]
                      },
                      "description": {"type": "string"},
                      "when": {"type": "string", "description": "Present only in this case"},
                      "stable": {"const": true},
                      "nullable": {"const": true}
                    }
                  }
                },
                "notes": {
                  "type": "array",
                  "items": {"type": "string"}
                }
              }
            }
          }
        },
        "detector": {
          "type": "object",
          "additionalProperties": false,
          "required": ["options_schema", "defaults", "caps", "result"],
          "properties": {
            "options_schema": {
              "type": "object",
              "additionalProperties": true,
              "description": "A JSON Schema: the type's options model (pydantic)"
            },
            "defaults": {
              "type": "object",
              "additionalProperties": true,
              "description": "The type's options as resolved for a criterion named `<name>`"
            },
            "caps": {
              "type": "object",
              "additionalProperties": {"type": "number"},
              "description": "The server's caps"
            },
            "result": {
              "type": "object",
              "description": "A type's `detail` declaration (`analysis.result_specs.ResultSpec.as_dict`)",
              "additionalProperties": false,
              "required": ["metric", "metric_from", "fields", "notes"],
              "properties": {
                "metric": {"type": "string"},
                "metric_from": {"enum": ["detail", "measurements", "score"]},
                "fields": {
                  "type": "object",
                  "additionalProperties": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["kind", "description"],
                    "properties": {
                      "kind": {
                        "enum": ["number", "integer", "string", "boolean", "array", "object"]
                      },
                      "description": {"type": "string"},
                      "when": {"type": "string", "description": "Present only in this case"},
                      "stable": {"const": true},
                      "nullable": {"const": true}
                    }
                  }
                },
                "notes": {
                  "type": "array",
                  "items": {"type": "string"}
                }
              }
            }
          }
        },
        "cv": {
          "type": "object",
          "additionalProperties": false,
          "required": ["options_schema", "defaults", "caps", "result"],
          "properties": {
            "options_schema": {
              "type": "object",
              "additionalProperties": true,
              "description": "A JSON Schema: the type's options model (pydantic)"
            },
            "defaults": {
              "type": "object",
              "additionalProperties": true,
              "description": "The type's options as resolved for a criterion named `<name>`"
            },
            "caps": {
              "type": "object",
              "additionalProperties": {"type": "number"},
              "description": "The server's caps"
            },
            "result": {
              "type": "object",
              "description": "A type's `detail` declaration (`analysis.result_specs.ResultSpec.as_dict`)",
              "additionalProperties": false,
              "required": ["metric", "metric_from", "fields", "notes"],
              "properties": {
                "metric": {"type": "string"},
                "metric_from": {"enum": ["detail", "measurements", "score"]},
                "fields": {
                  "type": "object",
                  "additionalProperties": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["kind", "description"],
                    "properties": {
                      "kind": {
                        "enum": ["number", "integer", "string", "boolean", "array", "object"]
                      },
                      "description": {"type": "string"},
                      "when": {"type": "string", "description": "Present only in this case"},
                      "stable": {"const": true},
                      "nullable": {"const": true}
                    }
                  }
                },
                "notes": {
                  "type": "array",
                  "items": {"type": "string"}
                }
              }
            }
          }
        }
      }
    },
    "reference": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "options_schema",
        "defaults",
        "applies_to",
        "requires",
        "match",
        "caps",
        "requires_model",
        "position"
      ],
      "properties": {
        "options_schema": {
          "type": "object",
          "additionalProperties": true,
          "description": "A JSON Schema: `ReferenceOptions`"
        },
        "defaults": {
          "type": "object",
          "additionalProperties": true,
          "description": "`ReferenceOptions` resolved for `<name>`"
        },
        "applies_to": {"type": "string"},
        "requires": {"type": "string"},
        "match": {"type": "string"},
        "caps": {
          "type": "object",
          "additionalProperties": false,
          "required": ["max_per_request", "max_per_criterion"],
          "properties": {
            "max_per_request": {"type": "integer"},
            "max_per_criterion": {"type": "integer"}
          }
        },
        "requires_model": {"type": "string"},
        "position": {
          "type": "object",
          "additionalProperties": false,
          "required": ["min_iou", "max_offset", "cap", "requires"],
          "properties": {
            "min_iou": {"type": "number"},
            "max_offset": {"type": "number"},
            "cap": {"type": "integer"},
            "requires": {"type": "string"}
          }
        }
      }
    },
    "box_loop": {
      "type": "object",
      "additionalProperties": false,
      "required": ["applies_to", "options", "precedence", "service_defaults", "marks_max", "score"],
      "properties": {
        "applies_to": {"type": "string"},
        "options": {
          "type": "object",
          "additionalProperties": {"type": "string"}
        },
        "precedence": {"type": "string"},
        "service_defaults": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "refine",
            "verify_partial",
            "verify_min_span",
            "reference.boxes",
            "reference.text"
          ],
          "properties": {
            "refine": {"enum": ["grid", "marks"]},
            "verify_partial": {"type": "boolean"},
            "verify_min_span": {"type": "number"},
            "reference.boxes": {
              "type": "object",
              "additionalProperties": false,
              "required": ["ask", "refine", "verify"],
              "properties": {
                "ask": {"type": "boolean"},
                "refine": {"type": "boolean"},
                "verify": {"type": "boolean"}
              }
            },
            "reference.text": {"type": "boolean"}
          }
        },
        "marks_max": {"type": "integer"},
        "score": {"type": "string"}
      }
    },
    "model": {
      "type": "object",
      "additionalProperties": false,
      "required": ["applies_to", "precedence", "default", "models", "catalog", "identity"],
      "properties": {
        "applies_to": {"type": "string"},
        "precedence": {"type": "string"},
        "default": {"type": "string"},
        "models": {
          "type": "array",
          "items": {"type": "string"}
        },
        "catalog": {"type": "string"},
        "identity": {"type": "string"}
      }
    },
    "detector_configured": {"type": "boolean", "description": "DETECTOR_URL is set"},
    "aggregate_detail": {
      "type": "object",
      "additionalProperties": {"type": "string"}
    }
  }
}
```

## `GET /v1/classifier/hints`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["hints"],
  "properties": {
    "hints": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["heading", "rubric", "extra"],
        "properties": {
          "heading": {"type": "string"},
          "rubric": {"type": "string"},
          "extra": {"type": "string"}
        }
      },
      "description": "Each hint value and the LLM rubric it selects"
    }
  }
}
```

## `GET /v1/classifier/cv-detectors`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["detectors", "total_names"],
  "properties": {
    "detectors": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One detector function and the names that select it, with its declaration (`cv.result.DetectorSpec.as_dict`)",
        "additionalProperties": false,
        "required": ["function", "names"],
        "properties": {
          "function": {"type": "string", "description": "The OpenCV function"},
          "names": {
            "type": "array",
            "items": {"type": "string"}
          },
          "technique": {"type": "string"},
          "metric": {"type": "string"},
          "measurements": {
            "type": "object",
            "additionalProperties": {
              "type": "object",
              "additionalProperties": false,
              "required": ["unit", "description"],
              "properties": {
                "unit": {"type": "string"},
                "description": {"type": "string"}
              }
            }
          },
          "thresholds": {
            "type": "object",
            "additionalProperties": {"type": "string"}
          },
          "parameters": {
            "type": "array",
            "items": {"type": "string"}
          },
          "states": {
            "type": "object",
            "additionalProperties": {"type": "string"}
          },
          "regions": {"type": ["string", "null"], "description": "What its regions are"}
        }
      },
      "description": "One per detector function, by function name"
    },
    "total_names": {"type": "integer", "minimum": 0}
  }
}
```

## `GET /v1/classifier/document-kinds`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "description": "The upload kinds, the text-match modes, the live OCR and detector status, the limits — the honest answer for THIS container",
  "additionalProperties": false,
  "required": [
    "kinds",
    "unsupported",
    "text_match_modes",
    "inputs",
    "ocr",
    "limits",
    "regions",
    "references"
  ],
  "properties": {
    "kinds": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "kind",
          "extensions",
          "content_types",
          "detection",
          "pages",
          "has_page_images",
          "native_text",
          "notes"
        ],
        "properties": {
          "kind": {"enum": ["image", "pdf", "svg", "txt", "docx"]},
          "extensions": {
            "type": "array",
            "items": {"type": "string"}
          },
          "content_types": {
            "type": "array",
            "items": {"type": "string"}
          },
          "detection": {"type": "string"},
          "pages": {"type": "string"},
          "has_page_images": {"type": "boolean"},
          "native_text": {"type": "boolean"},
          "notes": {"type": "string"},
          "formats": {
            "type": "array",
            "items": {"type": "string"}
          },
          "external_images": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "fetch",
              "max_images",
              "max_bytes",
              "timeout_s",
              "accepted",
              "redirects",
              "when"
            ],
            "properties": {
              "fetch": {"type": "boolean"},
              "max_images": {"type": "integer"},
              "max_bytes": {"type": "integer"},
              "timeout_s": {"type": "number"},
              "accepted": {
                "type": "array",
                "items": {"type": "string"}
              },
              "redirects": {"type": "string"},
              "when": {"type": "string"}
            }
          }
        }
      }
    },
    "unsupported": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["kind", "reason", "detection"],
        "properties": {
          "kind": {"type": "string"},
          "reason": {"type": "string"},
          "detection": {"type": "string"}
        }
      }
    },
    "text_match_modes": {
      "type": "object",
      "additionalProperties": {"type": "string"}
    },
    "inputs": {
      "type": "object",
      "additionalProperties": false,
      "required": ["json", "json_shape", "multipart"],
      "properties": {
        "json": {
          "type": "array",
          "items": {"type": "string"}
        },
        "json_shape": {"type": "string"},
        "multipart": {
          "type": "array",
          "items": {"type": "string"}
        }
      }
    },
    "ocr": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "engine",
        "available",
        "min_native_chars",
        "modes",
        "default",
        "set_on",
        "text_layer_artifact"
      ],
      "properties": {
        "engine": {"type": "string"},
        "available": {"type": "boolean"},
        "min_native_chars": {"type": "integer"},
        "modes": {
          "type": "array",
          "items": {"type": "string"}
        },
        "default": {"type": "string"},
        "set_on": {"type": "string"},
        "text_layer_artifact": {"type": "string"}
      }
    },
    "limits": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "max_items",
        "items",
        "pdf_render_dpi",
        "svg_max_render_pixels",
        "max_concurrent_jobs",
        "queue_limit",
        "queue",
        "max_units_per_job",
        "ocr_workers",
        "models",
        "default_model"
      ],
      "properties": {
        "max_items": {"type": "integer"},
        "items": {"type": "string"},
        "pdf_render_dpi": {"type": "integer"},
        "svg_max_render_pixels": {"type": "integer"},
        "max_concurrent_jobs": {"type": "integer"},
        "queue_limit": {
          "type": ["integer", "null"],
          "description": "CLASSIFIER_QUEUE_LIMIT; null: no limit"
        },
        "queue": {"type": "string"},
        "max_units_per_job": {"type": "integer"},
        "ocr_workers": {"type": "integer"},
        "models": {"type": "string"},
        "default_model": {
          "type": "object",
          "additionalProperties": false,
          "required": ["id", "llm_text_char_budget", "images_per_llm_prompt", "max_concurrent"],
          "properties": {
            "id": {"type": "string"},
            "llm_text_char_budget": {"type": "integer"},
            "images_per_llm_prompt": {"type": "integer"},
            "max_concurrent": {"type": "integer"}
          }
        }
      }
    },
    "regions": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "always_stored",
        "layers",
        "layers_rendered",
        "sources",
        "llm_boxes",
        "detector",
        "inline_max_per_criterion",
        "artifact_dir",
        "artifact_max_bytes_per_item",
        "artifact_ttl_hours",
        "sweep_interval_seconds"
      ],
      "properties": {
        "always_stored": {"const": true},
        "layers": {
          "type": "array",
          "items": {"type": "string"}
        },
        "layers_rendered": {"type": "string"},
        "sources": {
          "type": "array",
          "items": {"type": "string"}
        },
        "llm_boxes": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "set_on",
            "max_attempts",
            "verify_pass",
            "min_area",
            "max_area",
            "presence_min_score",
            "grid"
          ],
          "properties": {
            "set_on": {"type": "string"},
            "max_attempts": {"type": "integer"},
            "verify_pass": {"type": "integer"},
            "min_area": {"type": "number"},
            "max_area": {"type": "number"},
            "presence_min_score": {"type": "integer"},
            "grid": {"type": "integer"}
          }
        },
        "detector": {
          "type": "object",
          "additionalProperties": false,
          "required": [
            "configured",
            "url_host",
            "min_score",
            "timeout_seconds",
            "max_labels_per_call"
          ],
          "properties": {
            "configured": {"type": "boolean"},
            "url_host": {"type": ["string", "null"]},
            "min_score": {"type": "number"},
            "timeout_seconds": {"type": "number"},
            "max_labels_per_call": {"type": "integer"}
          }
        },
        "inline_max_per_criterion": {"type": "integer"},
        "artifact_dir": {"type": "string"},
        "artifact_max_bytes_per_item": {"type": "integer"},
        "artifact_ttl_hours": {"type": "number"},
        "sweep_interval_seconds": {"type": "number"}
      }
    },
    "references": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "enabled",
        "images_per_llm_prompt",
        "contrastive",
        "per_model",
        "max_count",
        "max_per_request",
        "max_per_criterion",
        "auto",
        "auto_pool_max",
        "auto_min_confidence",
        "applies_to",
        "dir",
        "retention"
      ],
      "properties": {
        "enabled": {"type": "boolean"},
        "images_per_llm_prompt": {"type": "integer"},
        "contrastive": {"type": "boolean"},
        "per_model": {"type": "string"},
        "max_count": {"type": "integer"},
        "max_per_request": {"type": "integer"},
        "max_per_criterion": {"type": "integer"},
        "auto": {"const": true},
        "auto_pool_max": {"type": "integer"},
        "auto_min_confidence": {"type": "number"},
        "applies_to": {"type": "string"},
        "dir": {"type": "string"},
        "retention": {"type": "string"}
      }
    }
  }
}
```

## `POST /v1/classifier/assess`

Every error is the generic `{"detail": "<message>"}` -- the body is parsed by hand, so a validation failure is a 400, never FastAPI's 422: 400 every request rule (§ Request), 409 a listed reference not ready, 415 a Content-Type that is neither JSON nor multipart, 429 + `Retry-After` the queue is full (CLASSIFIER_QUEUE_LIMIT), 500 the payload could not be persisted, 502 a document URL could not be fetched.

Request body (`application/json`):

```json
{
  "$comment": "generated from api.schemas.AssessRequest — do not edit",
  "additionalProperties": false,
  "description": "The whole /assess request — JSON and multipart both land here.\n\nAfter validation ``documents`` is ALWAYS the list and ``document`` is\nNone: the single-document shorthand is normalised into a one-item list,\nso nothing downstream has two shapes to handle.",
  "properties": {
    "documents": {
      "anyOf": [
        {
          "items": {
            "additionalProperties": false,
            "description": "One document: base64 bytes, a URL to fetch, or inline text.\n\nThe kind (a JPEG/PNG/HEIC/HEIF/AVIF photo, PDF, SVG, .txt, .docx) is\ndetermined from the BYTES, never from a filename or content type.\n``text`` is plain text treated as a .txt\ndocument. A URL is SSRF-checked (``common.net``) before it is fetched, at\nsubmit time — the item cap needs the bytes (a PDF counts its pages).",
            "properties": {
              "type": {
                "description": "'base64': data is the file base64-encoded. 'url': data is an http(s) URL. 'text': data is the document text itself.",
                "enum": ["base64", "url", "text"],
                "type": "string"
              },
              "data": {"description": "The payload for `type`.", "minLength": 1, "type": "string"},
              "filename": {
                "anyOf": [
                  {"maxLength": 255, "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Recorded in document_info; never used to detect the kind."
              }
            },
            "required": ["type", "data"],
            "type": "object"
          },
          "minItems": 1,
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "The documents, in order. Every page of every document is one item; the total is capped at CLASSIFIER_MAX_ITEMS."
    },
    "document": {
      "anyOf": [
        {
          "additionalProperties": false,
          "description": "One document: base64 bytes, a URL to fetch, or inline text.\n\nThe kind (a JPEG/PNG/HEIC/HEIF/AVIF photo, PDF, SVG, .txt, .docx) is\ndetermined from the BYTES, never from a filename or content type.\n``text`` is plain text treated as a .txt\ndocument. A URL is SSRF-checked (``common.net``) before it is fetched, at\nsubmit time — the item cap needs the bytes (a PDF counts its pages).",
          "properties": {
            "type": {
              "description": "'base64': data is the file base64-encoded. 'url': data is an http(s) URL. 'text': data is the document text itself.",
              "enum": ["base64", "url", "text"],
              "type": "string"
            },
            "data": {"description": "The payload for `type`.", "minLength": 1, "type": "string"},
            "filename": {
              "anyOf": [
                {"maxLength": 255, "type": "string"},
                {"type": "null"}
              ],
              "default": null,
              "description": "Recorded in document_info; never used to detect the kind."
            }
          },
          "required": ["type", "data"],
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Shorthand for a one-item 'documents' list. Not with 'documents'."
    },
    "criteria": {
      "description": "What to evaluate. Omitted: the criteria of the listed `references`, merged by name — or, without references, the four default quality criteria.",
      "items": {
        "additionalProperties": false,
        "description": "One criterion: shared fields at the top, type-specific ones in options.\n\n``options`` arrives as a plain object and is replaced, during validation,\nby the options model for ``type`` — so every consumer reads\n``c.options.hint`` on an ``llm`` criterion and never a dict.",
        "properties": {
          "name": {
            "description": "The criterion to evaluate; unique within the request.",
            "maxLength": 200,
            "minLength": 1,
            "type": "string"
          },
          "key": {
            "anyOf": [
              {"maxLength": 200, "minLength": 1, "type": "string"},
              {"type": "null"}
            ],
            "default": null,
            "description": "Optional label used ONLY to find reference examples: the criterion is guided by the reference criteria with the same key (case-insensitive), whatever either is named, and never falls back to its name. options.reference.criterion wins over it. Never shown to the model; results, depends_on and prompts stay on `name`. May repeat in a request; unique within one reference."
          },
          "type": {
            "default": "llm",
            "description": "'llm': the vision model scores it (one call per criterion). 'text': deterministic search of the text layer. 'cv': a registered OpenCV detector by name (GET /cv-detectors), with options.fallback when none matches. 'detector': the open-vocabulary detector service, using the name as its text prompt.",
            "enum": ["llm", "text", "cv", "detector"],
            "type": "string"
          },
          "weight": {
            "default": 1.0,
            "description": "Relative weight in the overall score. Default 1.",
            "exclusiveMinimum": 0.0,
            "type": "number"
          },
          "depends_on": {
            "anyOf": [
              {"type": "string"},
              {"type": "null"}
            ],
            "default": null,
            "description": "Name of another SCORED criterion that must PASS first. Until it does, this criterion is not evaluated at all; if it does not, this one is SKIPPED (no model call spent). Chains propagate."
          },
          "score": {
            "default": true,
            "description": "false = locate without judging: the criterion still produces regions but is excluded from the weighted score and the verdict, and its score / verdict / confidence are null. Refused for a criterion that cannot produce geometry.",
            "type": "boolean"
          },
          "options": {
            "default": null,
            "description": "Type-specific options; see GET /criterion-types."
          }
        },
        "required": ["name"],
        "type": "object"
      },
      "minItems": 1,
      "type": "array"
    },
    "references": {
      "anyOf": [
        {
          "items": {"type": "string"},
          "type": "array"
        },
        {"const": "auto", "type": "string"},
        {
          "additionalProperties": false,
          "description": "``references: {\"auto\": true, \"tags\": [...], \"tags_match\": \"all\" | \"any\"}``\n— let the service pick examples from the stored references. ``\"auto\"``\nalone is this with no tag filter.",
          "properties": {
            "auto": {"const": true, "type": "boolean"},
            "tags": {
              "description": "Only references carrying these tags (lowercased).",
              "items": {"type": "string"},
              "type": "array"
            },
            "tags_match": {
              "default": "any",
              "description": "A reference needs all of the tags, or any one.",
              "enum": ["all", "any"],
              "type": "string"
            }
          },
          "required": ["auto"],
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Stored worked examples (POST /references) to show the vision model beside each llm-answered criterion: a list of reference ids, \"auto\", or {\"auto\": true, \"tags\": [...], \"tags_match\": \"all\" | \"any\"}."
    },
    "model": {
      "anyOf": [
        {"maxLength": 64, "minLength": 1, "type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "The job's model — a registry id from GET /models. Used by every llm criterion (and cv llm fallback) without its own options.model, and by the references \"auto\" selection call. Omitted: the registry default."
    }
  },
  "type": "object"
}
```

Request body (`multipart/form-data`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "description": "Each `file` / `image` part and each `text` field is one document, in form order; anything else is refused by name (a 400)",
  "properties": {
    "file": {
      "anyOf": [
        {"type": "string", "contentMediaType": "application/octet-stream"},
        {
          "type": "array",
          "items": {"type": "string"}
        }
      ],
      "description": "A document: JPEG, PNG, HEIC / HEIF / AVIF, PDF (any page count), SVG, .txt or .docx — repeat the part for more"
    },
    "image": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "array",
          "items": {"type": "string"}
        }
      ],
      "description": "The legacy name of `file`"
    },
    "text": {
      "anyOf": [
        {"type": "string", "minLength": 1},
        {
          "type": "array",
          "items": {"type": "string", "minLength": 1}
        }
      ],
      "description": "An inline text document — repeat for more"
    },
    "criteria": {
      "type": "string",
      "contentMediaType": "application/json",
      "description": "JSON array of criterion objects (`CriterionInput`); omit to inherit the references' own"
    },
    "references": {
      "type": "string",
      "description": "JSON array of reference ids, the word `auto`, or a JSON `{\"auto\": true, …}`"
    },
    "model": {
      "type": "string",
      "description": "The job's model: a registry id from GET /models (default: the registry's)"
    }
  }
}
```

Response `202` (`application/json`) — Queued: poll GET /jobs/:jobId:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["job_id", "phase"],
  "properties": {
    "job_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A job's id: 12 hex characters"
    },
    "phase": {"const": "pending"}
  }
}
```

## `GET /v1/classifier/jobs`

Response `200` (`application/json`):

```json
{"$ref": "#/$defs/classifier_jobs"}
```

Uses: [`classifier_jobs`](#def-classifier_jobs), [`jobs_list_response`](#def-jobs_list_response), [`classifier_job`](#def-classifier_job), [`JobBase`](#def-JobBase), [`job_base`](#def-job_base), [`job_id`](#def-job_id), [`job_metadata`](#def-job_metadata), [`assess_result`](#def-assess_result), [`reference_result`](#def-reference_result), [`reference_id`](#def-reference_id), [`progress`](#def-progress), [`document_entry`](#def-document_entry), [`item_row`](#def-item_row), [`breakdown`](#def-breakdown), [`criterion_entry`](#def-criterion_entry), [`job_artifacts`](#def-job_artifacts), [`references_summary`](#def-references_summary), [`criterion_input`](#def-criterion_input), [`job_usage_block`](#def-job_usage_block), [`progress_stage`](#def-progress_stage), [`ocr_layer`](#def-ocr_layer), [`document_layer`](#def-document_layer), [`region`](#def-region), [`criterion_artifacts`](#def-criterion_artifacts), [`localization`](#def-localization), [`unit_entry`](#def-unit_entry), [`llm_detail`](#def-llm_detail), [`text_detail`](#def-text_detail), [`detector_detail`](#def-detector_detail), [`cv_detail`](#def-cv_detail), [`file_label`](#def-file_label), [`layer_links`](#def-layer_links), [`usage_totals_fields`](#def-usage_totals_fields), [`text_link`](#def-text_link), [`box_attempt`](#def-box_attempt), [`detail_llm_mean`](#def-detail_llm_mean), [`detail_llm`](#def-detail_llm), [`aggregate_block`](#def-aggregate_block), [`detail_text_mean`](#def-detail_text_mean), [`detail_text`](#def-detail_text), [`detail_detector_mean`](#def-detail_detector_mean), [`detail_detector`](#def-detail_detector), [`detail_cv_mean`](#def-detail_cv_mean), [`detail_cv`](#def-detail_cv), [`cv_measurements`](#def-cv_measurements), [`measurements_check_blur`](#def-measurements_check_blur), [`measurements_check_exposure`](#def-measurements_check_exposure), [`measurements_detect_faces`](#def-measurements_detect_faces), [`measurements_detect_sky`](#def-measurements_detect_sky), [`measurements_detect_text`](#def-measurements_detect_text), [`measurements_detect_vegetation`](#def-measurements_detect_vegetation), [`measurements_detect_water`](#def-measurements_detect_water) — too large to show inlined (over 64 KiB), so its `$ref`s are kept; each definition is shown once under [Definitions](#definitions).

## `GET /v1/classifier/jobs/:jobId`

`?wait=<s>` holds the request until the job is completed / failed / cancelled (clamped to 60 s; a negative or non-finite one is FastAPI's 422). 404 no such job.

Response `200` (`application/json`):

```json
{"$ref": "#/$defs/classifier_job"}
```

Uses: [`classifier_job`](#def-classifier_job), [`job_base`](#def-job_base), [`job_id`](#def-job_id), [`job_metadata`](#def-job_metadata), [`assess_result`](#def-assess_result), [`reference_result`](#def-reference_result), [`reference_id`](#def-reference_id), [`progress`](#def-progress), [`document_entry`](#def-document_entry), [`item_row`](#def-item_row), [`breakdown`](#def-breakdown), [`criterion_entry`](#def-criterion_entry), [`job_artifacts`](#def-job_artifacts), [`references_summary`](#def-references_summary), [`criterion_input`](#def-criterion_input), [`job_usage_block`](#def-job_usage_block), [`progress_stage`](#def-progress_stage), [`ocr_layer`](#def-ocr_layer), [`document_layer`](#def-document_layer), [`region`](#def-region), [`criterion_artifacts`](#def-criterion_artifacts), [`localization`](#def-localization), [`unit_entry`](#def-unit_entry), [`llm_detail`](#def-llm_detail), [`text_detail`](#def-text_detail), [`detector_detail`](#def-detector_detail), [`cv_detail`](#def-cv_detail), [`file_label`](#def-file_label), [`layer_links`](#def-layer_links), [`usage_totals_fields`](#def-usage_totals_fields), [`text_link`](#def-text_link), [`box_attempt`](#def-box_attempt), [`detail_llm_mean`](#def-detail_llm_mean), [`detail_llm`](#def-detail_llm), [`aggregate_block`](#def-aggregate_block), [`detail_text_mean`](#def-detail_text_mean), [`detail_text`](#def-detail_text), [`detail_detector_mean`](#def-detail_detector_mean), [`detail_detector`](#def-detail_detector), [`detail_cv_mean`](#def-detail_cv_mean), [`detail_cv`](#def-detail_cv), [`cv_measurements`](#def-cv_measurements), [`measurements_check_blur`](#def-measurements_check_blur), [`measurements_check_exposure`](#def-measurements_check_exposure), [`measurements_detect_faces`](#def-measurements_detect_faces), [`measurements_detect_sky`](#def-measurements_detect_sky), [`measurements_detect_text`](#def-measurements_detect_text), [`measurements_detect_vegetation`](#def-measurements_detect_vegetation), [`measurements_detect_water`](#def-measurements_detect_water) — too large to show inlined (over 64 KiB), so its `$ref`s are kept; each definition is shown once under [Definitions](#definitions).

## `GET /v1/classifier/jobs/:jobId/progress`

404 no such job (its history went with it).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "description": "The snapshot plus a page of the job's checkpoint history (`common.jobs.router.build_progress_router`)",
  "additionalProperties": false,
  "required": ["job_id", "phase", "progress", "attempt", "events", "next_after"],
  "properties": {
    "job_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A job's id: 12 hex characters"
    },
    "phase": {"type": "string"},
    "progress": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "object",
          "description": "How far along the job is (`common.jobs.progress`): the snapshot, flushed about once a second (§ Progress)",
          "additionalProperties": false,
          "required": [
            "state",
            "percent",
            "stage",
            "stage_title",
            "label",
            "index",
            "length",
            "attempt",
            "seq",
            "updated_at",
            "stages"
          ],
          "properties": {
            "state": {"enum": ["queued", "running", "done", "failed", "cancelled"]},
            "percent": {
              "type": "number",
              "minimum": 0,
              "maximum": 100,
              "description": "0-100, held just under 100 while the job runs"
            },
            "stage": {"type": ["string", "null"], "description": "The current stage's name"},
            "stage_title": {"type": ["string", "null"]},
            "label": {"type": ["string", "null"], "description": "What is happening now"},
            "index": {
              "type": "integer",
              "minimum": 0,
              "description": "Units done in the current stage"
            },
            "length": {"type": "integer", "minimum": 0, "description": "Units in the current stage"},
            "attempt": {
              "type": "integer",
              "minimum": 0,
              "description": "Which run of the job (a requeue is the next)"
            },
            "seq": {
              "type": "integer",
              "minimum": 0,
              "description": "The snapshot's sequence number in the job's history"
            },
            "updated_at": {"type": "string", "format": "date-time"},
            "stages": {
              "type": "array",
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["name", "title", "weight", "index", "length"],
                "properties": {
                  "name": {"type": "string"},
                  "title": {"type": "string"},
                  "weight": {"type": "number", "minimum": 0},
                  "index": {"type": "integer", "minimum": 0},
                  "length": {"type": "integer", "minimum": 0}
                }
              }
            }
          }
        }
      ]
    },
    "attempt": {
      "type": ["integer", "null"],
      "description": "Which run's history (null: no history yet)"
    },
    "events": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["seq", "at", "stage", "label", "index", "length", "percent", "state"],
        "properties": {
          "seq": {"type": "integer"},
          "at": {"type": "string", "format": "date-time"},
          "stage": {"type": ["string", "null"]},
          "label": {"type": ["string", "null"]},
          "index": {"type": ["integer", "null"]},
          "length": {"type": ["integer", "null"]},
          "percent": {"type": ["number", "null"]},
          "state": {"enum": ["queued", "running", "done", "failed", "cancelled"]}
        }
      },
      "description": "A page of the history, oldest first"
    },
    "next_after": {"type": "integer", "description": "The `after` for the next page"}
  }
}
```

## `POST /v1/classifier/jobs/:jobId/cancel`

404 no such job; 409 it had already ended (`already_completed` / `already_failed` / `already_cancelled`).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["job_id", "cancelled", "was_phase"],
  "properties": {
    "job_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A job's id: 12 hex characters"
    },
    "cancelled": {"const": true},
    "was_phase": {"enum": ["staging", "pending", "processing"]}
  }
}
```

## `DELETE /v1/classifier/jobs/:jobId`

Deletes the row, its artifact directory, its model-usage rows and its progress history; a running job stops like a cancel. 404 no such job.

Response `204`: no body — no body.

## `POST /v1/classifier/references`

Queues the creation job. Every error is the generic body (parsed by hand): 400 a request rule, 404 / 409 / 410 a `from_job` that is unknown / not completed / swept, 409 CLASSIFIER_REFERENCE_MAX_COUNT reached, 415 another Content-Type, 429 the queue is full.

Request body (`application/json`):

```json
{
  "$comment": "generated from api.reference_schemas.ReferenceRequest — do not edit",
  "additionalProperties": false,
  "description": "The whole POST /references body — JSON and multipart both land here.",
  "properties": {
    "title": {
      "anyOf": [
        {"maxLength": 120, "minLength": 1, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "description": {
      "anyOf": [
        {"maxLength": 500, "minLength": 1, "type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Omitted: generated from the page (CLASSIFIER_REFERENCE_DESCRIBE)."
    },
    "tags": {
      "anyOf": [
        {
          "items": {"type": "string"},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Lowercased; filter GET /references and `auto` by them."
    },
    "document": {
      "anyOf": [
        {
          "additionalProperties": false,
          "description": "One document: base64 bytes, a URL to fetch, or inline text.\n\nThe kind (a JPEG/PNG/HEIC/HEIF/AVIF photo, PDF, SVG, .txt, .docx) is\ndetermined from the BYTES, never from a filename or content type.\n``text`` is plain text treated as a .txt\ndocument. A URL is SSRF-checked (``common.net``) before it is fetched, at\nsubmit time — the item cap needs the bytes (a PDF counts its pages).",
          "properties": {
            "type": {
              "description": "'base64': data is the file base64-encoded. 'url': data is an http(s) URL. 'text': data is the document text itself.",
              "enum": ["base64", "url", "text"],
              "type": "string"
            },
            "data": {"description": "The payload for `type`.", "minLength": 1, "type": "string"},
            "filename": {
              "anyOf": [
                {"maxLength": 255, "type": "string"},
                {"type": "null"}
              ],
              "default": null,
              "description": "Recorded in document_info; never used to detect the kind."
            }
          },
          "required": ["type", "data"],
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "The example: a JPEG/PNG/HEIC/HEIF/AVIF photo, an SVG, or a PDF (one page of it)."
    },
    "page": {
      "anyOf": [
        {"minimum": 0, "type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Which PDF page (0-based). Default 0; 0 for an image."
    },
    "from_job": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "A completed /assess job whose item to save instead."
    },
    "from_job_item": {
      "anyOf": [
        {"minimum": 0, "type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "description": "That job's global item index. Default 0."
    },
    "criteria": {
      "anyOf": [
        {
          "items": {
            "additionalProperties": false,
            "description": "One criterion: shared fields at the top, type-specific ones in options.\n\n``options`` arrives as a plain object and is replaced, during validation,\nby the options model for ``type`` — so every consumer reads\n``c.options.hint`` on an ``llm`` criterion and never a dict.",
            "properties": {
              "name": {
                "description": "The criterion to evaluate; unique within the request.",
                "maxLength": 200,
                "minLength": 1,
                "type": "string"
              },
              "key": {
                "anyOf": [
                  {"maxLength": 200, "minLength": 1, "type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Optional label used ONLY to find reference examples: the criterion is guided by the reference criteria with the same key (case-insensitive), whatever either is named, and never falls back to its name. options.reference.criterion wins over it. Never shown to the model; results, depends_on and prompts stay on `name`. May repeat in a request; unique within one reference."
              },
              "type": {
                "default": "llm",
                "description": "'llm': the vision model scores it (one call per criterion). 'text': deterministic search of the text layer. 'cv': a registered OpenCV detector by name (GET /cv-detectors), with options.fallback when none matches. 'detector': the open-vocabulary detector service, using the name as its text prompt.",
                "enum": ["llm", "text", "cv", "detector"],
                "type": "string"
              },
              "weight": {
                "default": 1.0,
                "description": "Relative weight in the overall score. Default 1.",
                "exclusiveMinimum": 0.0,
                "type": "number"
              },
              "depends_on": {
                "anyOf": [
                  {"type": "string"},
                  {"type": "null"}
                ],
                "default": null,
                "description": "Name of another SCORED criterion that must PASS first. Until it does, this criterion is not evaluated at all; if it does not, this one is SKIPPED (no model call spent). Chains propagate."
              },
              "score": {
                "default": true,
                "description": "false = locate without judging: the criterion still produces regions but is excluded from the weighted score and the verdict, and its score / verdict / confidence are null. Refused for a criterion that cannot produce geometry.",
                "type": "boolean"
              },
              "options": {
                "default": null,
                "description": "Type-specific options; see GET /criterion-types."
              }
            },
            "required": ["name"],
            "type": "object"
          },
          "minItems": 1,
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Required with `document`; with `from_job`, omitted means the job's own."
    },
    "breakdown": {
      "additionalProperties": {
        "additionalProperties": false,
        "description": "One criterion's expected answer: the score, and optionally its verdict\n(which must agree with the score) and a reason.",
        "properties": {
          "score": {
            "description": "The expected 1-10 score.",
            "maximum": 10,
            "minimum": 1,
            "type": "integer"
          },
          "verdict": {
            "anyOf": [
              {"enum": ["PASS", "MARGINAL", "FAIL"], "type": "string"},
              {"type": "null"}
            ],
            "default": null,
            "description": "Optional; must be the score's verdict (7-10 PASS, 4-6 MARGINAL, 1-3 FAIL)."
          },
          "reason": {
            "anyOf": [
              {"maxLength": 1000, "type": "string"},
              {"type": "null"}
            ],
            "default": null,
            "description": "Why — shown to the model as the example's caption."
          }
        },
        "required": ["score"],
        "type": "object"
      },
      "description": "{criterion name: {score, verdict?, reason?}} — the answer key. Missing names are classified by the creation job.",
      "type": "object"
    },
    "regions": {
      "additionalProperties": {
        "items": {
          "additionalProperties": false,
          "description": "One region: ``{\"box\": [x1, y1, x2, y2]}`` or ``{\"polygon\": [[x, y], ...]}``,\nin ``region_units``.",
          "properties": {
            "box": {
              "anyOf": [
                {
                  "items": {"type": "number"},
                  "type": "array"
                },
                {"type": "null"}
              ],
              "default": null,
              "description": "[x1, y1, x2, y2]"
            },
            "polygon": {
              "anyOf": [
                {
                  "items": {
                    "items": {"type": "number"},
                    "type": "array"
                  },
                  "type": "array"
                },
                {"type": "null"}
              ],
              "default": null,
              "description": "Three or more [x, y] vertices."
            }
          },
          "type": "object"
        },
        "type": "array"
      },
      "description": "{criterion name: [{box} | {polygon}]} — where. A missing name is located automatically; [] means the whole page is the example.",
      "type": "object"
    },
    "region_units": {
      "default": "px",
      "description": "'px': original page pixels. 'grid': 0-1000 on both axes (recommended for a PDF page, whose pixel size depends on the render DPI).",
      "enum": ["px", "grid"],
      "type": "string"
    },
    "model": {
      "anyOf": [
        {"maxLength": 64, "minLength": 1, "type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "The creation job's model — a registry id from GET /models, and a VISION model: it scores, locates and describes the page. Omitted: the registry default. A criterion's options.model still wins for it."
    }
  },
  "type": "object"
}
```

Request body (`multipart/form-data`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "description": "The JSON body's fields as form values; anything else is refused by name (a 400)",
  "properties": {
    "file": {
      "type": "string",
      "description": "The ONE example page: a JPEG / PNG / HEIC / HEIF / AVIF photo, an SVG, or a PDF with `page`"
    },
    "image": {"type": "string", "description": "The legacy name of `file`"},
    "page": {"type": "string", "description": "An integer: the page of a PDF"},
    "from_job": {"type": "string"},
    "from_job_item": {"type": "string", "description": "An integer"},
    "criteria": {
      "type": "string",
      "contentMediaType": "application/json",
      "description": "JSON array: the same CriterionInput list /assess takes"
    },
    "breakdown": {
      "type": "string",
      "contentMediaType": "application/json",
      "description": "JSON object: criterion name → {score, verdict?, reason?}"
    },
    "regions": {
      "type": "string",
      "contentMediaType": "application/json",
      "description": "JSON object: criterion name → [{box | polygon}]"
    },
    "region_units": {"enum": ["px", "grid"]},
    "title": {"type": "string"},
    "description": {"type": "string"},
    "tags": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "array",
          "items": {"type": "string"}
        }
      ],
      "description": "Repeat the field, or one JSON array"
    },
    "model": {"type": "string"}
  }
}
```

Response `202` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["reference_id", "status", "job_id"],
  "properties": {
    "reference_id": {
      "type": "string",
      "pattern": "^r[0-9a-f]{12}$",
      "description": "A reference's id: `r` and 12 hex characters"
    },
    "status": {"const": "pending"},
    "job_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A job's id: 12 hex characters"
    }
  }
}
```

## `GET /v1/classifier/references`

Newest first. 400 an unknown `status`; FastAPI's 422 a `limit` / `offset` out of range.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["references", "total", "limit", "offset"],
  "properties": {
    "references": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One reference, without its record (`references.model.Reference.summary`)",
        "additionalProperties": false,
        "required": [
          "reference_id",
          "status",
          "title",
          "description",
          "description_source",
          "tags",
          "job_id",
          "source",
          "error",
          "created_at",
          "updated_at",
          "criteria"
        ],
        "properties": {
          "reference_id": {
            "type": "string",
            "pattern": "^r[0-9a-f]{12}$",
            "description": "A reference's id: `r` and 12 hex characters"
          },
          "status": {"enum": ["pending", "ready", "failed"]},
          "title": {"type": ["string", "null"]},
          "description": {"type": ["string", "null"]},
          "description_source": {"type": ["string", "null"]},
          "tags": {
            "type": "array",
            "items": {"type": "string"}
          },
          "job_id": {
            "anyOf": [
              {"type": "null"},
              {
                "type": "string",
                "pattern": "^[0-9a-f]{12}$",
                "description": "A job's id: 12 hex characters"
              }
            ]
          },
          "source": {
            "type": "object",
            "additionalProperties": false,
            "required": ["kind", "job_id", "item"],
            "properties": {
              "kind": {"type": "string", "description": "document | job"},
              "job_id": {"type": ["string", "null"]},
              "item": {"type": ["integer", "null"]}
            }
          },
          "error": {"type": ["string", "null"]},
          "created_at": {"type": "string"},
          "updated_at": {"type": "string"},
          "criteria": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "name",
                "key",
                "type",
                "guides_llm",
                "usable",
                "verdict",
                "score",
                "region_source"
              ],
              "properties": {
                "name": {"type": "string"},
                "key": {"type": ["string", "null"]},
                "type": {"type": ["string", "null"]},
                "guides_llm": {"type": "boolean"},
                "usable": {"type": "boolean"},
                "verdict": {
                  "anyOf": [
                    {"type": "null"},
                    {"enum": ["PASS", "MARGINAL", "FAIL"]}
                  ]
                },
                "score": {"type": ["integer", "null"]},
                "region_source": {"type": ["string", "null"]}
              }
            }
          }
        }
      }
    },
    "total": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    "offset": {"type": "integer", "minimum": 0}
  }
}
```

## `GET /v1/classifier/references/:referenceId`

404 no such reference.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "description": "One reference: its summary, the frozen record and its file URLs",
  "allOf": [
    {
      "properties": {
        "reference_id": {
          "type": "string",
          "pattern": "^r[0-9a-f]{12}$",
          "description": "A reference's id: `r` and 12 hex characters"
        },
        "status": {"enum": ["pending", "ready", "failed"]},
        "title": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "description_source": {"type": ["string", "null"]},
        "tags": {
          "type": "array",
          "items": {"type": "string"}
        },
        "job_id": {
          "anyOf": [
            {"type": "null"},
            {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A job's id: 12 hex characters"
            }
          ]
        },
        "source": {
          "type": "object",
          "additionalProperties": false,
          "required": ["kind", "job_id", "item"],
          "properties": {
            "kind": {"type": "string", "description": "document | job"},
            "job_id": {"type": ["string", "null"]},
            "item": {"type": ["integer", "null"]}
          }
        },
        "error": {"type": ["string", "null"]},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
        "criteria": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "name",
              "key",
              "type",
              "guides_llm",
              "usable",
              "verdict",
              "score",
              "region_source"
            ],
            "properties": {
              "name": {"type": "string"},
              "key": {"type": ["string", "null"]},
              "type": {"type": ["string", "null"]},
              "guides_llm": {"type": "boolean"},
              "usable": {"type": "boolean"},
              "verdict": {
                "anyOf": [
                  {"type": "null"},
                  {"enum": ["PASS", "MARGINAL", "FAIL"]}
                ]
              },
              "score": {"type": ["integer", "null"]},
              "region_source": {"type": ["string", "null"]}
            }
          }
        }
      }
    }
  ],
  "properties": {
    "record": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "object",
          "additionalProperties": true,
          "description": "The frozen record (record.json): the criteria, the expected answers, the regions and the source page — § References"
        }
      ]
    },
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name", "bytes", "content_type", "url"],
        "properties": {
          "name": {"type": "string"},
          "bytes": {"type": "integer", "minimum": 0},
          "content_type": {"type": "string"},
          "url": {"type": "string"}
        }
      },
      "description": "The reference's files (record.json, the page, the composites)"
    }
  },
  "required": [
    "reference_id",
    "status",
    "title",
    "description",
    "description_source",
    "tags",
    "job_id",
    "source",
    "error",
    "created_at",
    "updated_at",
    "criteria",
    "record",
    "files"
  ],
  "unevaluatedProperties": false
}
```

## `GET /v1/classifier/references/:referenceId/files/:referenceFile`

404 no such reference or file.

Response `200` (`*/*`) — The file, typed by its name: record.json `application/json`, the page and composites `image/jpeg` / `image/png`.

## `PATCH /v1/classifier/references/:referenceId`

Only `title`, `description`, `tags` and `criteria_keys` are editable. 400 an invalid patch (parsed by hand); 404 no such reference; 409 a `criteria_keys` edit the record refuses.

Request body (`application/json`):

```json
{
  "$comment": "generated from api.reference_schemas.ReferencePatch — do not edit",
  "additionalProperties": false,
  "description": "PATCH /references/{id}: the metadata, and the criteria's keys.\n\nAn explicit null clears a title or description. ``criteria_keys`` maps a\ncriterion name to its new key (null clears it): a key changes how the\nreference is FOUND, not what it teaches, so it sits with the lookup\nmetadata — and a reference saved before keys can be keyed without being\nre-created. Everything else (the criteria themselves, answers, regions,\npage) is immutable — a new answer key is a new reference. Here only the\nkey grammar is checked (``api.schemas.clean_key`` and the length cap);\nthat each name is a criterion, that the reference is ready and that its\nkeys stay unique need the record — ``api.references.patch_reference``.",
  "properties": {
    "title": {
      "anyOf": [
        {"maxLength": 120, "minLength": 1, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "description": {
      "anyOf": [
        {"maxLength": 500, "minLength": 1, "type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Omitted: generated from the page (CLASSIFIER_REFERENCE_DESCRIBE)."
    },
    "tags": {
      "anyOf": [
        {
          "items": {"type": "string"},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "Lowercased; filter GET /references and `auto` by them."
    },
    "criteria_keys": {
      "anyOf": [
        {
          "additionalProperties": {
            "anyOf": [
              {"maxLength": 200, "type": "string"},
              {"type": "null"}
            ]
          },
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null,
      "description": "{criterion name: key | null} — set (or, with null, clear) each named criterion's reference-matching key. Ready references only; keys stay unique within the reference."
    }
  },
  "type": "object"
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "description": "One reference: its summary, the frozen record and its file URLs",
  "allOf": [
    {
      "properties": {
        "reference_id": {
          "type": "string",
          "pattern": "^r[0-9a-f]{12}$",
          "description": "A reference's id: `r` and 12 hex characters"
        },
        "status": {"enum": ["pending", "ready", "failed"]},
        "title": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "description_source": {"type": ["string", "null"]},
        "tags": {
          "type": "array",
          "items": {"type": "string"}
        },
        "job_id": {
          "anyOf": [
            {"type": "null"},
            {
              "type": "string",
              "pattern": "^[0-9a-f]{12}$",
              "description": "A job's id: 12 hex characters"
            }
          ]
        },
        "source": {
          "type": "object",
          "additionalProperties": false,
          "required": ["kind", "job_id", "item"],
          "properties": {
            "kind": {"type": "string", "description": "document | job"},
            "job_id": {"type": ["string", "null"]},
            "item": {"type": ["integer", "null"]}
          }
        },
        "error": {"type": ["string", "null"]},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
        "criteria": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "name",
              "key",
              "type",
              "guides_llm",
              "usable",
              "verdict",
              "score",
              "region_source"
            ],
            "properties": {
              "name": {"type": "string"},
              "key": {"type": ["string", "null"]},
              "type": {"type": ["string", "null"]},
              "guides_llm": {"type": "boolean"},
              "usable": {"type": "boolean"},
              "verdict": {
                "anyOf": [
                  {"type": "null"},
                  {"enum": ["PASS", "MARGINAL", "FAIL"]}
                ]
              },
              "score": {"type": ["integer", "null"]},
              "region_source": {"type": ["string", "null"]}
            }
          }
        }
      }
    }
  ],
  "properties": {
    "record": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "object",
          "additionalProperties": true,
          "description": "The frozen record (record.json): the criteria, the expected answers, the regions and the source page — § References"
        }
      ]
    },
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name", "bytes", "content_type", "url"],
        "properties": {
          "name": {"type": "string"},
          "bytes": {"type": "integer", "minimum": 0},
          "content_type": {"type": "string"},
          "url": {"type": "string"}
        }
      },
      "description": "The reference's files (record.json, the page, the composites)"
    }
  },
  "required": [
    "reference_id",
    "status",
    "title",
    "description",
    "description_source",
    "tags",
    "job_id",
    "source",
    "error",
    "created_at",
    "updated_at",
    "criteria",
    "record",
    "files"
  ],
  "unevaluatedProperties": false
}
```

## `DELETE /v1/classifier/references/:referenceId`

404 no such reference; 409 a queued or running job reads it.

Response `204`: no body — no body.

## `GET /v1/classifier/jobs/:jobId/artifacts`

`?criterion=<slug>` (repeatable) scopes it and adds `layers` and `filter`. 400 an unknown slug, 404 an unknown or unfinished job, 410 its directory was swept.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "description": "manifest.json, its files labelled (`regions.artifacts.FileLabeler`)",
  "additionalProperties": false,
  "required": [
    "job_id",
    "created_at",
    "expires_at",
    "options",
    "criteria",
    "page_geometry",
    "files",
    "total_bytes",
    "dropped",
    "notes",
    "items"
  ],
  "properties": {
    "job_id": {
      "type": "string",
      "pattern": "^[0-9a-f]{12}$",
      "description": "A job's id: 12 hex characters"
    },
    "created_at": {"type": "string", "format": "date-time"},
    "expires_at": {"type": "string", "format": "date-time"},
    "options": {
      "type": "object",
      "additionalProperties": false,
      "required": ["layers", "layers_rendered", "byte_cap"],
      "properties": {
        "layers": {
          "type": "array",
          "items": {"type": "string"}
        },
        "layers_rendered": {"type": "string"},
        "byte_cap": {
          "type": "object",
          "additionalProperties": false,
          "required": ["per_item", "items", "total"],
          "properties": {
            "per_item": {"type": "integer"},
            "items": {"type": "integer"},
            "total": {"type": "integer"}
          }
        }
      }
    },
    "criteria": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["slug", "count", "sources", "text_layers"],
        "properties": {
          "slug": {"type": "string"},
          "count": {"type": "integer", "minimum": 0},
          "sources": {
            "type": "array",
            "items": {"type": "string"}
          },
          "text_layers": {
            "type": "array",
            "items": {"type": "string"}
          }
        }
      }
    },
    "page_geometry": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["page", "width", "height", "working_scale", "pdf_points"],
        "properties": {
          "page": {"type": "integer"},
          "width": {"type": "integer"},
          "height": {"type": "integer"},
          "working_scale": {"type": "number"},
          "pdf_points": {
            "anyOf": [
              {"type": "null"},
              {
                "type": "array",
                "items": {"type": "number"}
              }
            ]
          }
        }
      }
    },
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One file of the artifact directory, labelled (`regions.artifacts.FileLabeler`); `format` only for a layer or a text file",
        "additionalProperties": false,
        "required": ["name", "bytes", "content_type", "url", "kind", "item", "document", "criteria"],
        "properties": {
          "name": {"type": "string"},
          "bytes": {"type": "integer", "minimum": 0},
          "content_type": {"type": "string"},
          "url": {"type": "string"},
          "kind": {
            "type": "string",
            "description": "What the file is: base image, layer, regions, text, manifest, references"
          },
          "format": {"type": "string"},
          "item": {"type": ["integer", "null"]},
          "document": {"type": ["integer", "null"]},
          "criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The criteria the file belongs to"
          }
        }
      },
      "description": "What is on disk NOW (lazily rendered layers once fetched)"
    },
    "total_bytes": {"type": "integer", "minimum": 0},
    "dropped": {
      "type": "array",
      "items": {"type": "string"}
    },
    "notes": {
      "type": "array",
      "items": {"type": "string"}
    },
    "items": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["document", "page", "filename"],
        "properties": {
          "document": {"type": "integer"},
          "page": {"type": "integer"},
          "filename": {"type": "string"}
        }
      },
      "description": "Item n → where the page came from"
    },
    "layers": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": {
          "type": "object",
          "description": "Layer URLs (rendered on first fetch)",
          "additionalProperties": false,
          "required": [],
          "properties": {
            "svg": {"type": "string"},
            "png": {"type": "string"},
            "preview": {"type": "string"}
          }
        }
      },
      "description": "`?criterion=` only: item → criterion slug → its layer URLs (items it has hits on)"
    },
    "filter": {
      "type": "object",
      "description": "`?criterion=` only: the slugs asked for and the criteria they name",
      "additionalProperties": false,
      "required": ["criterion", "criteria"],
      "properties": {
        "criterion": {
          "type": "array",
          "items": {"type": "string"}
        },
        "criteria": {
          "type": "array",
          "items": {"type": "string"}
        }
      }
    },
    "zip_url": {"type": "string", "description": "`?criterion=` only: the scoped zip"}
  }
}
```

## `GET /v1/classifier/jobs/:jobId/artifacts/:artifactName`

Rendered on first fetch when it is a layer. `criterion` / `source` / `attempt` / `accepted` filter regions.json and the combined layers. 400 an unknown slug or a filter on a name that is not a layer, 404 an unknown or unfinished job or a missing file, 410 its directory was swept.

Response `200` (`*/*`) — The file, typed by its name: `.json` (regions.json, manifest.json, text.*.json) `application/json`, `.svg` `image/svg+xml`, `.png` `image/png`, `.jpg` `image/jpeg`, `text.*.txt` (the text a criterion searched) `text/plain`.

## `GET /v1/classifier/jobs/:jobId/artifacts.zip`

`?criterion=<slug>` (repeatable) zips only those criteria's files. 400 an unknown slug, 404 an unknown or unfinished job, 410 its directory was swept.

Response `200` (`application/zip`) — Every file on disk, streamed; `Content-Disposition: attachment; filename="<job_id>-artifacts.zip"`.

## `DELETE /v1/classifier/jobs/:jobId/artifacts`

Removes the directory; the job row stays. 404 an unknown job, or one with no directory.

Response `204`: no body — no body.

## `GET /v1/classifier/jobs/:jobId/usage`

The calls outlive the job (only DELETE /jobs/:jobId removes them). 404 neither the job nor any call is known.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "job_id",
    "job_present",
    "job_type",
    "totals",
    "by_kind",
    "by_outcome",
    "by_criterion",
    "by_model",
    "calls"
  ],
  "properties": {
    "job_id": {"type": "string"},
    "job_present": {"type": "boolean", "description": "The job row still exists"},
    "job_type": {"type": ["string", "null"]},
    "totals": {
      "type": "object",
      "allOf": [
        {
          "properties": {
            "calls": {"type": "integer", "minimum": 0},
            "errors": {"type": "integer", "minimum": 0},
            "seconds": {"type": "number", "minimum": 0},
            "prompt_tokens": {"type": ["integer", "null"]},
            "cached_tokens": {"type": ["integer", "null"]},
            "completion_tokens": {"type": ["integer", "null"]},
            "reasoning_tokens": {"type": ["integer", "null"]},
            "total_tokens": {"type": ["integer", "null"]}
          }
        }
      ],
      "properties": {
        "models": {
          "type": "array",
          "items": {"type": "string"}
        },
        "first_call_at": {"type": ["string", "null"], "format": "date-time"},
        "last_call_at": {"type": ["string", "null"], "format": "date-time"}
      },
      "required": [
        "calls",
        "errors",
        "seconds",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
        "models",
        "first_call_at",
        "last_call_at"
      ],
      "unevaluatedProperties": false
    },
    "by_kind": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "description": "A totals block: the token sums are null when no call reported them",
        "additionalProperties": false,
        "required": [
          "calls",
          "errors",
          "seconds",
          "prompt_tokens",
          "cached_tokens",
          "completion_tokens",
          "reasoning_tokens",
          "total_tokens"
        ],
        "properties": {
          "calls": {"type": "integer", "minimum": 0},
          "errors": {"type": "integer", "minimum": 0},
          "seconds": {"type": "number", "minimum": 0},
          "prompt_tokens": {"type": ["integer", "null"]},
          "cached_tokens": {"type": ["integer", "null"]},
          "completion_tokens": {"type": ["integer", "null"]},
          "reasoning_tokens": {"type": ["integer", "null"]},
          "total_tokens": {"type": ["integer", "null"]}
        }
      }
    },
    "by_outcome": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "description": "A totals block: the token sums are null when no call reported them",
        "additionalProperties": false,
        "required": [
          "calls",
          "errors",
          "seconds",
          "prompt_tokens",
          "cached_tokens",
          "completion_tokens",
          "reasoning_tokens",
          "total_tokens"
        ],
        "properties": {
          "calls": {"type": "integer", "minimum": 0},
          "errors": {"type": "integer", "minimum": 0},
          "seconds": {"type": "number", "minimum": 0},
          "prompt_tokens": {"type": ["integer", "null"]},
          "cached_tokens": {"type": ["integer", "null"]},
          "completion_tokens": {"type": ["integer", "null"]},
          "reasoning_tokens": {"type": ["integer", "null"]},
          "total_tokens": {"type": ["integer", "null"]}
        }
      }
    },
    "by_criterion": {
      "type": "array",
      "items": {
        "type": "object",
        "allOf": [
          {
            "properties": {
              "calls": {"type": "integer", "minimum": 0},
              "errors": {"type": "integer", "minimum": 0},
              "seconds": {"type": "number", "minimum": 0},
              "prompt_tokens": {"type": ["integer", "null"]},
              "cached_tokens": {"type": ["integer", "null"]},
              "completion_tokens": {"type": ["integer", "null"]},
              "reasoning_tokens": {"type": ["integer", "null"]},
              "total_tokens": {"type": ["integer", "null"]}
            }
          }
        ],
        "properties": {
          "criterion": {"type": ["string", "null"]}
        },
        "required": [
          "calls",
          "errors",
          "seconds",
          "prompt_tokens",
          "cached_tokens",
          "completion_tokens",
          "reasoning_tokens",
          "total_tokens",
          "criterion"
        ],
        "unevaluatedProperties": false
      }
    },
    "by_model": {
      "type": "array",
      "items": {
        "type": "object",
        "allOf": [
          {
            "properties": {
              "calls": {"type": "integer", "minimum": 0},
              "errors": {"type": "integer", "minimum": 0},
              "seconds": {"type": "number", "minimum": 0},
              "prompt_tokens": {"type": ["integer", "null"]},
              "cached_tokens": {"type": ["integer", "null"]},
              "completion_tokens": {"type": ["integer", "null"]},
              "reasoning_tokens": {"type": ["integer", "null"]},
              "total_tokens": {"type": ["integer", "null"]}
            }
          }
        ],
        "properties": {
          "model": {"type": ["string", "null"]}
        },
        "required": [
          "calls",
          "errors",
          "seconds",
          "prompt_tokens",
          "cached_tokens",
          "completion_tokens",
          "reasoning_tokens",
          "total_tokens",
          "model"
        ],
        "unevaluatedProperties": false
      }
    },
    "calls": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One `llm_calls` row",
        "additionalProperties": false,
        "required": [
          "id",
          "job_id",
          "job_type",
          "request_id",
          "started_at",
          "label",
          "kind",
          "criterion",
          "item",
          "document",
          "attempt",
          "model_requested",
          "model_reported",
          "api_url",
          "max_tokens",
          "outcome",
          "http_status",
          "error",
          "finish_reason",
          "seconds",
          "prompt_tokens",
          "cached_tokens",
          "completion_tokens",
          "reasoning_tokens",
          "total_tokens",
          "usage",
          "model_id"
        ],
        "properties": {
          "id": {"type": "integer"},
          "job_id": {"type": ["string", "null"]},
          "job_type": {"type": ["string", "null"]},
          "request_id": {"type": ["string", "null"]},
          "started_at": {"type": "string", "format": "date-time"},
          "label": {"type": ["string", "null"]},
          "kind": {"type": "string", "description": "score | box_* | select | describe | …"},
          "criterion": {"type": ["string", "null"]},
          "item": {"type": ["integer", "null"]},
          "document": {"type": ["integer", "null"]},
          "attempt": {"type": ["integer", "null"]},
          "model_requested": {"type": ["string", "null"]},
          "model_reported": {"type": ["string", "null"]},
          "api_url": {"type": ["string", "null"]},
          "max_tokens": {"type": ["integer", "null"]},
          "outcome": {"type": "string", "description": "ok | error | …"},
          "http_status": {"type": ["integer", "null"]},
          "error": {"type": ["string", "null"]},
          "finish_reason": {"type": ["string", "null"]},
          "seconds": {"type": "number", "minimum": 0},
          "prompt_tokens": {"type": ["integer", "null"]},
          "cached_tokens": {"type": ["integer", "null"]},
          "completion_tokens": {"type": ["integer", "null"]},
          "reasoning_tokens": {"type": ["integer", "null"]},
          "total_tokens": {"type": ["integer", "null"]},
          "usage": {
            "anyOf": [
              {"type": "null"},
              {
                "type": "object",
                "additionalProperties": true,
                "description": "The response's `usage` block as the model sent it"
              }
            ]
          },
          "model_id": {
            "type": ["string", "null"],
            "description": "The registry id (null on rows written before the registry)"
          }
        }
      },
      "description": "Every call, in the order made"
    }
  }
}
```

## `GET /v1/classifier/usage`

400 an unreadable `since` / `until`, or `since` not before `until`.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["filter", "totals", "by_kind", "by_model", "by_day"],
  "properties": {
    "filter": {
      "type": "object",
      "additionalProperties": false,
      "required": ["since", "until", "job_type", "model"],
      "properties": {
        "since": {"type": ["string", "null"]},
        "until": {"type": ["string", "null"]},
        "job_type": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]}
      }
    },
    "totals": {
      "type": "object",
      "allOf": [
        {
          "properties": {
            "calls": {"type": "integer", "minimum": 0},
            "errors": {"type": "integer", "minimum": 0},
            "seconds": {"type": "number", "minimum": 0},
            "prompt_tokens": {"type": ["integer", "null"]},
            "cached_tokens": {"type": ["integer", "null"]},
            "completion_tokens": {"type": ["integer", "null"]},
            "reasoning_tokens": {"type": ["integer", "null"]},
            "total_tokens": {"type": ["integer", "null"]}
          }
        }
      ],
      "properties": {
        "models": {
          "type": "array",
          "items": {"type": "string"}
        },
        "jobs": {"type": "integer", "minimum": 0}
      },
      "required": [
        "calls",
        "errors",
        "seconds",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
        "models",
        "jobs"
      ],
      "unevaluatedProperties": false
    },
    "by_kind": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "description": "A totals block: the token sums are null when no call reported them",
        "additionalProperties": false,
        "required": [
          "calls",
          "errors",
          "seconds",
          "prompt_tokens",
          "cached_tokens",
          "completion_tokens",
          "reasoning_tokens",
          "total_tokens"
        ],
        "properties": {
          "calls": {"type": "integer", "minimum": 0},
          "errors": {"type": "integer", "minimum": 0},
          "seconds": {"type": "number", "minimum": 0},
          "prompt_tokens": {"type": ["integer", "null"]},
          "cached_tokens": {"type": ["integer", "null"]},
          "completion_tokens": {"type": ["integer", "null"]},
          "reasoning_tokens": {"type": ["integer", "null"]},
          "total_tokens": {"type": ["integer", "null"]}
        }
      }
    },
    "by_model": {
      "type": "array",
      "items": {
        "type": "object",
        "allOf": [
          {
            "properties": {
              "calls": {"type": "integer", "minimum": 0},
              "errors": {"type": "integer", "minimum": 0},
              "seconds": {"type": "number", "minimum": 0},
              "prompt_tokens": {"type": ["integer", "null"]},
              "cached_tokens": {"type": ["integer", "null"]},
              "completion_tokens": {"type": ["integer", "null"]},
              "reasoning_tokens": {"type": ["integer", "null"]},
              "total_tokens": {"type": ["integer", "null"]}
            }
          }
        ],
        "properties": {
          "model": {"type": ["string", "null"]}
        },
        "required": [
          "calls",
          "errors",
          "seconds",
          "prompt_tokens",
          "cached_tokens",
          "completion_tokens",
          "reasoning_tokens",
          "total_tokens",
          "model"
        ],
        "unevaluatedProperties": false
      }
    },
    "by_day": {
      "type": "array",
      "items": {
        "type": "object",
        "allOf": [
          {
            "properties": {
              "calls": {"type": "integer", "minimum": 0},
              "errors": {"type": "integer", "minimum": 0},
              "seconds": {"type": "number", "minimum": 0},
              "prompt_tokens": {"type": ["integer", "null"]},
              "cached_tokens": {"type": ["integer", "null"]},
              "completion_tokens": {"type": ["integer", "null"]},
              "reasoning_tokens": {"type": ["integer", "null"]},
              "total_tokens": {"type": ["integer", "null"]}
            }
          }
        ],
        "properties": {
          "day": {"type": "string", "description": "YYYY-MM-DD, UTC"}
        },
        "required": [
          "calls",
          "errors",
          "seconds",
          "prompt_tokens",
          "cached_tokens",
          "completion_tokens",
          "reasoning_tokens",
          "total_tokens",
          "day"
        ],
        "unevaluatedProperties": false
      }
    }
  }
}
```

## Definitions

The definitions (`#/$defs/<name>`) that the schemas above too large to show inlined (over 64 KiB) point to, each shown once, with its own `$ref`s kept. Each such schema's `Uses:` line lists the ones it reaches.

### <a id="def-aggregate_block"></a>`aggregate_block`

```json
{
  "type": "object",
  "description": "`detail.aggregate`: how several members were combined (`AGGREGATE_BLOCK`)",
  "additionalProperties": false,
  "required": ["rule", "level", "from", "values"],
  "properties": {
    "rule": {"enum": ["any", "worst", "all", "mean", "sum"]},
    "level": {"enum": ["pages", "documents"]},
    "from": {
      "type": ["string", "null"],
      "description": "The member whose detail this is (any / worst)"
    },
    "values": {
      "type": "object",
      "additionalProperties": {"type": ["number", "null"]},
      "description": "Each member's value, by member label (`item n` / `document n`)"
    }
  }
}
```

### <a id="def-assess_result"></a>`assess_result`

```json
{
  "type": "object",
  "description": "An assess job's result, `schema_version: 3` (§ Result shape)",
  "additionalProperties": false,
  "required": [
    "schema_version",
    "documents",
    "items",
    "assessment",
    "verdict",
    "page_geometry",
    "detector",
    "artifacts",
    "references",
    "request"
  ],
  "properties": {
    "schema_version": {"const": 3},
    "documents": {
      "type": "array",
      "items": {"$ref": "#/$defs/document_entry"},
      "description": "One per document, in request order"
    },
    "items": {
      "type": "array",
      "items": {"$ref": "#/$defs/item_row"},
      "description": "One per item"
    },
    "assessment": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "overall_score",
        "overall_verdict",
        "complete",
        "weighted_score_breakdown",
        "per_criterion_scores"
      ],
      "properties": {
        "overall_score": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
        "overall_verdict": {
          "anyOf": [
            {"type": "null"},
            {"enum": ["PASS", "MARGINAL", "FAIL"]}
          ]
        },
        "complete": {"type": "boolean", "description": "false when any scored criterion errored"},
        "weighted_score_breakdown": {
          "anyOf": [
            {"type": "null"},
            {"$ref": "#/$defs/breakdown"}
          ]
        },
        "per_criterion_scores": {
          "type": "object",
          "additionalProperties": {"$ref": "#/$defs/criterion_entry"},
          "description": "By criterion name, in request order"
        }
      }
    },
    "verdict": {
      "anyOf": [
        {"type": "null"},
        {"enum": ["PASS", "MARGINAL", "FAIL"]}
      ]
    },
    "page_geometry": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["item", "page", "width", "height", "working_scale", "pdf_points"],
        "properties": {
          "item": {"type": "integer"},
          "page": {"type": "integer", "description": "Equals `item`"},
          "width": {"type": ["integer", "null"]},
          "height": {"type": ["integer", "null"]},
          "working_scale": {"type": ["number", "null"]},
          "pdf_points": {
            "anyOf": [
              {"type": "null"},
              {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2
              }
            ]
          }
        }
      },
      "description": "One per item; nulls for an item with no page image"
    },
    "detector": {
      "type": "object",
      "description": "What the open-vocabulary detector did for the whole job",
      "additionalProperties": false,
      "required": [
        "configured",
        "url_host",
        "min_score",
        "timeout_seconds",
        "max_labels_per_call",
        "model",
        "device",
        "calls",
        "labels",
        "detections",
        "elapsed_ms",
        "errors",
        "used"
      ],
      "properties": {
        "configured": {"type": "boolean"},
        "url_host": {"type": ["string", "null"]},
        "min_score": {"type": "number"},
        "timeout_seconds": {"type": "number"},
        "max_labels_per_call": {"type": "integer"},
        "model": {"type": ["string", "null"]},
        "device": {"type": ["string", "null"]},
        "calls": {"type": "integer", "minimum": 0},
        "labels": {"type": "integer", "minimum": 0},
        "detections": {"type": "integer", "minimum": 0},
        "elapsed_ms": {"type": "number", "minimum": 0},
        "errors": {
          "type": "array",
          "items": {"type": "string"}
        },
        "used": {"type": "boolean"}
      }
    },
    "artifacts": {
      "anyOf": [
        {"type": "null"},
        {"$ref": "#/$defs/job_artifacts"}
      ]
    },
    "references": {
      "anyOf": [
        {"type": "null"},
        {"$ref": "#/$defs/references_summary"}
      ]
    },
    "request": {
      "type": "object",
      "additionalProperties": false,
      "required": ["criteria", "model"],
      "properties": {
        "criteria": {
          "type": "array",
          "items": {"$ref": "#/$defs/criterion_input"},
          "description": "The criteria exactly as validated"
        },
        "model": {"type": "string", "description": "The job's model, resolved"}
      }
    },
    "usage": {"$ref": "#/$defs/job_usage_block"}
  }
}
```

### <a id="def-box_attempt"></a>`box_attempt`

```json
{
  "type": "object",
  "description": "One box-loop attempt (`llm.boxes.BoxAttempt.as_dict`): keys that never applied are left out",
  "additionalProperties": false,
  "required": ["attempt", "bbox_grid", "bbox_px", "valid", "accepted"],
  "properties": {
    "item": {
      "type": "integer",
      "description": "The unit's item: present exactly when the record merges several items or documents"
    },
    "attempt": {"type": "integer", "minimum": 1},
    "bbox_grid": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "array",
          "items": {"type": "number"},
          "minItems": 4,
          "maxItems": 4
        }
      ]
    },
    "bbox_px": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "array",
          "items": {"type": "number"},
          "minItems": 4,
          "maxItems": 4
        }
      ]
    },
    "valid": {"type": "boolean"},
    "accepted": {"type": "boolean"},
    "reject": {"type": "string", "description": "Why the box was rejected (the retry's feedback)"},
    "verify_score": {"type": "integer"},
    "verify_reason": {"type": "string"},
    "verify_partial": {"type": "boolean"},
    "detector_iou": {"type": "number"},
    "coarse_bbox_grid": {
      "type": "array",
      "items": {"type": "number"}
    },
    "refine_window_grid": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "array",
          "items": {"type": "number"}
        }
      ]
    },
    "refined": {"type": "boolean"},
    "refine_reject": {"type": "string"},
    "refine_mode": {"enum": ["marks", "grid"]},
    "marks_shown": {"type": "integer"},
    "marks_lines": {"enum": ["native", "ocr", "window"]},
    "marks": {
      "type": "array",
      "items": {"type": "integer"}
    },
    "marks_fallback": {"type": "string"}
  }
}
```

### <a id="def-breakdown"></a>`breakdown`

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "formula",
    "total_weight",
    "weighted_sum",
    "unrounded_average",
    "final_score",
    "partial",
    "excluded",
    "per_criterion"
  ],
  "properties": {
    "formula": {"const": "sum(score * weight) / total_weight"},
    "total_weight": {"type": "number"},
    "weighted_sum": {"type": "number"},
    "unrounded_average": {"type": "number"},
    "final_score": {"type": "integer"},
    "partial": {
      "type": "boolean",
      "description": "Some scored criterion errored: this is the score of what succeeded"
    },
    "excluded": {
      "type": "object",
      "additionalProperties": {"type": "string"},
      "description": "Criterion name → why it was left out"
    },
    "per_criterion": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["score", "weight", "contribution"],
        "properties": {
          "score": {"type": "integer"},
          "weight": {"type": "number"},
          "contribution": {"type": "number"}
        }
      }
    }
  }
}
```

### <a id="def-classifier_job"></a>`classifier_job`

```json
{
  "description": "One job: the shared `common.jobs` `JobBase` with the classifier's metadata and result",
  "allOf": [
    {"$ref": "#/$defs/job_base"}
  ],
  "properties": {
    "job_id": {"$ref": "#/$defs/job_id"},
    "phase": {
      "enum": ["staging", "pending", "processing", "completed", "failed", "cancelled"],
      "description": "`staging` (being written) → `pending` (queued) → `processing` → `completed` | `failed` | `cancelled`"
    },
    "metadata": {"$ref": "#/$defs/job_metadata"},
    "result": {
      "oneOf": [
        {"type": "null"},
        {"$ref": "#/$defs/assess_result"},
        {"$ref": "#/$defs/reference_result"}
      ],
      "description": "Set when the job completed: the assessment (an assess job) or the reference it built"
    },
    "error": {"type": ["string", "null"], "description": "Why a failed job failed"}
  }
}
```

### <a id="def-classifier_jobs"></a>`classifier_jobs`

```json
{
  "description": "The 20 most recently updated jobs, finished ones included (PostgresRegistry.list_all)",
  "allOf": [
    {"$ref": "#/$defs/jobs_list_response"}
  ],
  "properties": {
    "jobs": {
      "items": {"$ref": "#/$defs/classifier_job"}
    },
    "max_concurrent": {"type": "null", "description": "The registry tracks no ceiling"},
    "available": {"type": "null"}
  },
  "required": ["max_concurrent", "available"]
}
```

### <a id="def-criterion_artifacts"></a>`criterion_artifacts`

```json
{
  "type": "object",
  "description": "Per-criterion links; null when there is neither geometry nor text",
  "additionalProperties": false,
  "required": [],
  "properties": {
    "slug": {"type": "string", "description": "The criterion's file-name slug"},
    "regions_url": {"type": "string", "description": "regions.json scoped to the criterion"},
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["item", "count", "layers", "attempts"],
        "properties": {
          "item": {"type": "integer"},
          "count": {"type": "integer", "minimum": 0},
          "layers": {"$ref": "#/$defs/layer_links"},
          "attempts": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": ["attempt", "accepted"],
              "properties": {
                "attempt": {"type": "integer"},
                "accepted": {"type": "boolean"},
                "svg": {"type": "string"},
                "png": {"type": "string"},
                "preview": {"type": "string"}
              }
            },
            "description": "One per enforcement-loop attempt with a drawable box on the item"
          }
        }
      },
      "description": "Per item it found something on"
    },
    "text": {
      "type": "array",
      "items": {"$ref": "#/$defs/text_link"},
      "description": "One link per text layer its units read"
    }
  }
}
```

### <a id="def-criterion_entry"></a>`criterion_entry`

```json
{
  "type": "object",
  "description": "One criterion: the same keys for every type and status (§ Result shape)",
  "additionalProperties": false,
  "required": [
    "status",
    "type",
    "key",
    "method",
    "scored",
    "score",
    "verdict",
    "confidence",
    "reason",
    "detail",
    "regions",
    "regions_truncated",
    "artifacts",
    "localization",
    "options_used",
    "error",
    "complete",
    "aggregate_used",
    "items"
  ],
  "properties": {
    "status": {
      "enum": ["ok", "error", "skipped"],
      "description": "`ok` when at least one unit answered, `error` when none did and one errored, `skipped` when every unit was skipped"
    },
    "type": {"enum": ["llm", "text", "detector", "cv"]},
    "key": {"type": ["string", "null"], "description": "The criterion's `key` as sent (stripped)"},
    "method": {
      "anyOf": [
        {"type": "null"},
        {
          "enum": ["llm", "text", "detector", "cv"],
          "description": "The path that answered (a cv fallback's); null when skipped by a dependency"
        }
      ]
    },
    "scored": {"type": "boolean", "description": "The criterion's `score` flag"},
    "score": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
    "verdict": {
      "anyOf": [
        {"type": "null"},
        {"enum": ["PASS", "MARGINAL", "FAIL"]}
      ]
    },
    "confidence": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
    "reason": {"type": "string"},
    "detail": {
      "description": "Structured diagnostics: its method's declared shape (GET /criterion-types `result`), or null"
    },
    "regions": {
      "type": "array",
      "items": {"$ref": "#/$defs/region"},
      "description": "The union over every item, capped at CLASSIFIER_INLINE_REGIONS_MAX"
    },
    "regions_truncated": {"type": "boolean"},
    "artifacts": {
      "anyOf": [
        {"type": "null"},
        {"$ref": "#/$defs/criterion_artifacts"}
      ]
    },
    "localization": {
      "anyOf": [
        {"type": "null"},
        {"$ref": "#/$defs/localization"}
      ]
    },
    "options_used": {
      "type": "object",
      "additionalProperties": true,
      "description": "Options after defaults and caps, `aggregate` resolved — the keys of the type's options (GET /criterion-types `types.<type>.defaults`)"
    },
    "error": {"type": ["string", "null"]},
    "complete": {"type": "boolean", "description": "false when any of its units errored"},
    "aggregate_used": {
      "type": "object",
      "description": "The rules applied; `pages: null` plus a `note` for a document-scope text criterion",
      "additionalProperties": false,
      "required": ["pages", "documents"],
      "properties": {
        "pages": {"type": ["string", "null"]},
        "documents": {"type": "string"},
        "note": {"type": "string"}
      }
    },
    "items": {
      "type": "array",
      "items": {"$ref": "#/$defs/unit_entry"},
      "description": "One entry per unit"
    }
  },
  "allOf": [
    {
      "if": {
        "properties": {
          "method": {"const": "llm"}
        },
        "required": ["method"]
      },
      "then": {
        "properties": {
          "detail": {"$ref": "#/$defs/llm_detail"},
          "items": {
            "items": {
              "properties": {
                "detail": {"$ref": "#/$defs/llm_detail"}
              }
            }
          }
        }
      }
    },
    {
      "if": {
        "properties": {
          "method": {"const": "text"}
        },
        "required": ["method"]
      },
      "then": {
        "properties": {
          "detail": {"$ref": "#/$defs/text_detail"},
          "items": {
            "items": {
              "properties": {
                "detail": {"$ref": "#/$defs/text_detail"}
              }
            }
          }
        }
      }
    },
    {
      "if": {
        "properties": {
          "method": {"const": "detector"}
        },
        "required": ["method"]
      },
      "then": {
        "properties": {
          "detail": {"$ref": "#/$defs/detector_detail"},
          "items": {
            "items": {
              "properties": {
                "detail": {"$ref": "#/$defs/detector_detail"}
              }
            }
          }
        }
      }
    },
    {
      "if": {
        "properties": {
          "method": {"const": "cv"}
        },
        "required": ["method"]
      },
      "then": {
        "properties": {
          "detail": {"$ref": "#/$defs/cv_detail"},
          "items": {
            "items": {
              "properties": {
                "detail": {"$ref": "#/$defs/cv_detail"}
              }
            }
          }
        }
      }
    },
    {
      "if": {
        "properties": {
          "method": {"type": "null"}
        },
        "required": ["method"]
      },
      "then": {
        "properties": {
          "detail": {"type": "null"}
        }
      }
    }
  ]
}
```

### <a id="def-criterion_input"></a>`criterion_input`

```json
{
  "$comment": "generated from api.schemas.CriterionInput — do not edit",
  "additionalProperties": false,
  "description": "One criterion: shared fields at the top, type-specific ones in options.\n\n``options`` arrives as a plain object and is replaced, during validation,\nby the options model for ``type`` — so every consumer reads\n``c.options.hint`` on an ``llm`` criterion and never a dict.",
  "properties": {
    "name": {
      "description": "The criterion to evaluate; unique within the request.",
      "maxLength": 200,
      "minLength": 1,
      "type": "string"
    },
    "key": {
      "anyOf": [
        {"maxLength": 200, "minLength": 1, "type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Optional label used ONLY to find reference examples: the criterion is guided by the reference criteria with the same key (case-insensitive), whatever either is named, and never falls back to its name. options.reference.criterion wins over it. Never shown to the model; results, depends_on and prompts stay on `name`. May repeat in a request; unique within one reference."
    },
    "type": {
      "default": "llm",
      "description": "'llm': the vision model scores it (one call per criterion). 'text': deterministic search of the text layer. 'cv': a registered OpenCV detector by name (GET /cv-detectors), with options.fallback when none matches. 'detector': the open-vocabulary detector service, using the name as its text prompt.",
      "enum": ["llm", "text", "cv", "detector"],
      "type": "string"
    },
    "weight": {
      "default": 1.0,
      "description": "Relative weight in the overall score. Default 1.",
      "exclusiveMinimum": 0.0,
      "type": "number"
    },
    "depends_on": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null,
      "description": "Name of another SCORED criterion that must PASS first. Until it does, this criterion is not evaluated at all; if it does not, this one is SKIPPED (no model call spent). Chains propagate."
    },
    "score": {
      "default": true,
      "description": "false = locate without judging: the criterion still produces regions but is excluded from the weighted score and the verdict, and its score / verdict / confidence are null. Refused for a criterion that cannot produce geometry.",
      "type": "boolean"
    },
    "options": {"default": null, "description": "Type-specific options; see GET /criterion-types."}
  },
  "required": ["name"],
  "type": "object"
}
```

### <a id="def-cv_detail"></a>`cv_detail`

```json
{
  "description": "An `cv` detail, or null (skipped, errored)",
  "anyOf": [
    {"type": "null"},
    {
      "if": {
        "properties": {
          "aggregate": {
            "properties": {
              "rule": {"const": "mean"}
            },
            "required": ["rule"]
          }
        },
        "required": ["aggregate"]
      },
      "then": {"$ref": "#/$defs/detail_cv_mean"},
      "else": {"$ref": "#/$defs/detail_cv"},
      "properties": {
        "aggregate": {"$ref": "#/$defs/aggregate_block"}
      },
      "allOf": [
        {"$ref": "#/$defs/cv_measurements"}
      ]
    }
  ]
}
```

### <a id="def-cv_measurements"></a>`cv_measurements`

```json
{
  "$comment": "generated from cv.REGISTRY — do not edit",
  "description": "A cv `detail`'s `measurements` are its own detector's (GET /cv-detectors).",
  "properties": {
    "detector": {
      "enum": [
        "check_blur",
        "check_exposure",
        "detect_faces",
        "detect_sky",
        "detect_text",
        "detect_vegetation",
        "detect_water"
      ]
    }
  },
  "allOf": [
    {
      "if": {
        "properties": {
          "detector": {"const": "check_blur"}
        },
        "required": ["detector"]
      },
      "then": {
        "properties": {
          "measurements": {"$ref": "#/$defs/measurements_check_blur"}
        }
      }
    },
    {
      "if": {
        "properties": {
          "detector": {"const": "check_exposure"}
        },
        "required": ["detector"]
      },
      "then": {
        "properties": {
          "measurements": {"$ref": "#/$defs/measurements_check_exposure"}
        }
      }
    },
    {
      "if": {
        "properties": {
          "detector": {"const": "detect_faces"}
        },
        "required": ["detector"]
      },
      "then": {
        "properties": {
          "measurements": {"$ref": "#/$defs/measurements_detect_faces"}
        }
      }
    },
    {
      "if": {
        "properties": {
          "detector": {"const": "detect_sky"}
        },
        "required": ["detector"]
      },
      "then": {
        "properties": {
          "measurements": {"$ref": "#/$defs/measurements_detect_sky"}
        }
      }
    },
    {
      "if": {
        "properties": {
          "detector": {"const": "detect_text"}
        },
        "required": ["detector"]
      },
      "then": {
        "properties": {
          "measurements": {"$ref": "#/$defs/measurements_detect_text"}
        }
      }
    },
    {
      "if": {
        "properties": {
          "detector": {"const": "detect_vegetation"}
        },
        "required": ["detector"]
      },
      "then": {
        "properties": {
          "measurements": {"$ref": "#/$defs/measurements_detect_vegetation"}
        }
      }
    },
    {
      "if": {
        "properties": {
          "detector": {"const": "detect_water"}
        },
        "required": ["detector"]
      },
      "then": {
        "properties": {
          "measurements": {"$ref": "#/$defs/measurements_detect_water"}
        }
      }
    }
  ]
}
```

### <a id="def-detail_cv"></a>`detail_cv`

```json
{
  "$comment": "generated from analysis.result_specs.specs()['cv'] — do not edit",
  "type": "object",
  "description": "`detail` of a `cv` criterion",
  "additionalProperties": false,
  "required": ["metric", "value", "detector", "measurements", "thresholds", "parameters", "image"],
  "properties": {
    "metric": {"type": "string", "description": "Name of the headline number"},
    "value": {
      "type": "number",
      "description": "The headline number (the mean of the members' values under `mean`)"
    },
    "detector": {"type": "string", "description": "The OpenCV function that ran"},
    "measurements": {
      "type": "object",
      "description": "Everything the detector counted — its keys: GET /cv-detectors",
      "additionalProperties": true
    },
    "thresholds": {
      "type": "object",
      "description": "The lines that decide the verdict",
      "additionalProperties": true
    },
    "parameters": {
      "type": "object",
      "description": "Config values it measured with",
      "additionalProperties": true
    },
    "state": {
      "type": "string",
      "description": "A categorical outcome — its values: GET /cv-detectors (present only when the detector declares states)"
    },
    "image": {
      "type": "object",
      "description": "The working-image frame the *_px measurements are in",
      "additionalProperties": true
    },
    "aggregate": {
      "type": "object",
      "description": "How several members were combined — see `aggregate_detail` (present only when the criterion ran on more than one item or document)",
      "additionalProperties": true
    }
  }
}
```

### <a id="def-detail_cv_mean"></a>`detail_cv_mean`

```json
{
  "$comment": "generated from analysis.result_specs.specs()['cv'], the stable keys a `mean` keeps — do not edit",
  "type": "object",
  "description": "`detail` of a `cv` criterion aggregated by `mean`: only the stable keys, `metric`, `value` (the members' mean) and `aggregate`",
  "additionalProperties": false,
  "required": ["metric", "value", "detector", "thresholds", "parameters", "image", "aggregate"],
  "properties": {
    "metric": {"type": "string", "description": "Name of the headline number"},
    "value": {
      "type": "number",
      "description": "The headline number (the mean of the members' values under `mean`)"
    },
    "detector": {"type": "string", "description": "The OpenCV function that ran"},
    "thresholds": {
      "type": "object",
      "description": "The lines that decide the verdict",
      "additionalProperties": true
    },
    "parameters": {
      "type": "object",
      "description": "Config values it measured with",
      "additionalProperties": true
    },
    "image": {
      "type": "object",
      "description": "The working-image frame the *_px measurements are in",
      "additionalProperties": true
    },
    "aggregate": {
      "type": "object",
      "description": "How several members were combined — see `aggregate_detail`",
      "additionalProperties": true
    }
  }
}
```

### <a id="def-detail_detector"></a>`detail_detector`

```json
{
  "$comment": "generated from analysis.result_specs.specs()['detector'] — do not edit",
  "type": "object",
  "description": "`detail` of a `detector` criterion",
  "additionalProperties": false,
  "required": ["metric", "value", "detector_matches", "best_score", "min_score"],
  "properties": {
    "metric": {"type": "string", "description": "Name of the headline number"},
    "value": {
      "type": "number",
      "description": "The headline number (the mean of the members' values under `mean`)"
    },
    "detector_matches": {"type": "integer", "description": "Boxes found at or above min_score"},
    "best_score": {"type": "number", "description": "The best box's confidence, 0-1; 0 when none"},
    "min_score": {"type": "number", "description": "The confidence floor used"},
    "aggregate": {
      "type": "object",
      "description": "How several members were combined — see `aggregate_detail` (present only when the criterion ran on more than one item or document)",
      "additionalProperties": true
    }
  }
}
```

### <a id="def-detail_detector_mean"></a>`detail_detector_mean`

```json
{
  "$comment": "generated from analysis.result_specs.specs()['detector'], the stable keys a `mean` keeps — do not edit",
  "type": "object",
  "description": "`detail` of a `detector` criterion aggregated by `mean`: only the stable keys, `metric`, `value` (the members' mean) and `aggregate`",
  "additionalProperties": false,
  "required": ["metric", "value", "min_score", "aggregate"],
  "properties": {
    "metric": {"type": "string", "description": "Name of the headline number"},
    "value": {
      "type": "number",
      "description": "The headline number (the mean of the members' values under `mean`)"
    },
    "min_score": {"type": "number", "description": "The confidence floor used"},
    "aggregate": {
      "type": "object",
      "description": "How several members were combined — see `aggregate_detail`",
      "additionalProperties": true
    }
  }
}
```

### <a id="def-detail_llm"></a>`detail_llm`

```json
{
  "$comment": "generated from analysis.result_specs.specs()['llm'] — do not edit",
  "type": "object",
  "description": "`detail` of a `llm` criterion",
  "additionalProperties": false,
  "required": ["metric", "value", "hint", "model", "image_sent", "text_sent", "confidence"],
  "properties": {
    "metric": {"type": "string", "description": "Name of the headline number"},
    "value": {
      "type": ["number", "null"],
      "description": "The model's score, 1-10 — null for a `score: false` criterion, whose judgement is not reported"
    },
    "hint": {"type": "string", "description": "The rubric the model was asked to use"},
    "model": {
      "type": "string",
      "description": "The registry id of the model that answered (GET /models): options.model, else the request's model, else the registry default"
    },
    "image_sent": {
      "type": "boolean",
      "description": "Whether the page image was attached (never, for a text-only model)"
    },
    "text_sent": {
      "type": "object",
      "description": "The text attached: `{chars, truncated, budget, source}`",
      "additionalProperties": true
    },
    "confidence": {
      "type": ["object", "null"],
      "description": "How sure the model was, measured per models.yaml `confidence.method` (llm.confidence): `{method, source: logprobs | sampling | stated, headline: stated | calibrated, stated, stated_defaulted, calibrated, verdict_probability, band: {PASS, MARGINAL, FAIL}, distribution: {\"1\"..\"10\"}, expected_score, entropy_bits, certainty, coverage, unresolved: [{values, mass}], samples: null | {asked, answered, agreement, stated_mean}, note}` — the distribution keys are null when source is `stated`; the whole block is null for a `score: false` criterion",
      "additionalProperties": true
    },
    "reference": {
      "type": "object",
      "description": "What the request's references did for this unit: `{applied, mode, combine, examples: [{reference_id, criterion, expected, polarity, region, call}], calls: [{call, images, score, verdict, confidence, verdict_probability, reason, chosen, error}], position: null | {status, iou, center_offset, min_iou, max_offset, reference_id, capped_from}, note}` (present only when the request listed `references` (applied: false when no example matched))",
      "additionalProperties": true
    },
    "aggregate": {
      "type": "object",
      "description": "How several members were combined — see `aggregate_detail` (present only when the criterion ran on more than one item or document)",
      "additionalProperties": true
    }
  }
}
```

### <a id="def-detail_llm_mean"></a>`detail_llm_mean`

```json
{
  "$comment": "generated from analysis.result_specs.specs()['llm'], the stable keys a `mean` keeps — do not edit",
  "type": "object",
  "description": "`detail` of a `llm` criterion aggregated by `mean`: only the stable keys, `metric`, `value` (the members' mean) and `aggregate`",
  "additionalProperties": false,
  "required": ["metric", "value", "hint", "model", "image_sent", "aggregate"],
  "properties": {
    "metric": {"type": "string", "description": "Name of the headline number"},
    "value": {
      "type": ["number", "null"],
      "description": "The model's score, 1-10 — null for a `score: false` criterion, whose judgement is not reported"
    },
    "hint": {"type": "string", "description": "The rubric the model was asked to use"},
    "model": {
      "type": "string",
      "description": "The registry id of the model that answered (GET /models): options.model, else the request's model, else the registry default"
    },
    "image_sent": {
      "type": "boolean",
      "description": "Whether the page image was attached (never, for a text-only model)"
    },
    "aggregate": {
      "type": "object",
      "description": "How several members were combined — see `aggregate_detail`",
      "additionalProperties": true
    }
  }
}
```

### <a id="def-detail_text"></a>`detail_text`

```json
{
  "$comment": "generated from analysis.result_specs.specs()['text'] — do not edit",
  "type": "object",
  "description": "`detail` of a `text` criterion",
  "additionalProperties": false,
  "required": [
    "metric",
    "value",
    "found",
    "count",
    "best_ratio",
    "mode",
    "pattern",
    "snippets",
    "searched_chars",
    "case_sensitive",
    "min_count",
    "fuzzy_threshold",
    "text_source"
  ],
  "properties": {
    "metric": {"type": "string", "description": "Name of the headline number"},
    "value": {
      "type": "number",
      "description": "The headline number (the mean of the members' values under `mean`)"
    },
    "found": {"type": "boolean", "description": "Whether count reached min_count"},
    "count": {"type": "integer", "description": "Hits found (summed across members under `sum`)"},
    "best_ratio": {
      "type": "number",
      "description": "Best similarity seen, 0-1 — 1.0 for a literal/regex hit"
    },
    "mode": {"type": "string", "description": "The match mode used"},
    "pattern": {"type": "string", "description": "What was searched for"},
    "snippets": {"type": "array", "description": "Up to 8 context excerpts around the hits"},
    "searched_chars": {"type": "integer", "description": "Characters searched"},
    "case_sensitive": {"type": "boolean", "description": "Whether matching was case-sensitive"},
    "min_count": {"type": "integer", "description": "Hits required to PASS"},
    "fuzzy_threshold": {
      "type": ["number", "null"],
      "description": "Similarity a fuzzy window must reach; null for other modes"
    },
    "text_source": {
      "type": "string",
      "description": "native | ocr | none — or `mixed` when summed across members that differ"
    },
    "scope": {
      "type": "string",
      "description": "`document` when the pages were searched joined (present only when options.scope is document)"
    },
    "separator": {
      "type": "string",
      "description": "What joined the pages (present only when options.scope is document)"
    },
    "items_with_hits": {
      "type": "array",
      "description": "Items the hits landed on (present only when options.scope is document)"
    },
    "aggregate": {
      "type": "object",
      "description": "How several members were combined — see `aggregate_detail` (present only when the criterion ran on more than one item or document)",
      "additionalProperties": true
    }
  }
}
```

### <a id="def-detail_text_mean"></a>`detail_text_mean`

```json
{
  "$comment": "generated from analysis.result_specs.specs()['text'], the stable keys a `mean` keeps — do not edit",
  "type": "object",
  "description": "`detail` of a `text` criterion aggregated by `mean`: only the stable keys, `metric`, `value` (the members' mean) and `aggregate`",
  "additionalProperties": false,
  "required": [
    "metric",
    "value",
    "mode",
    "pattern",
    "case_sensitive",
    "min_count",
    "fuzzy_threshold",
    "aggregate"
  ],
  "properties": {
    "metric": {"type": "string", "description": "Name of the headline number"},
    "value": {
      "type": "number",
      "description": "The headline number (the mean of the members' values under `mean`)"
    },
    "mode": {"type": "string", "description": "The match mode used"},
    "pattern": {"type": "string", "description": "What was searched for"},
    "case_sensitive": {"type": "boolean", "description": "Whether matching was case-sensitive"},
    "min_count": {"type": "integer", "description": "Hits required to PASS"},
    "fuzzy_threshold": {
      "type": ["number", "null"],
      "description": "Similarity a fuzzy window must reach; null for other modes"
    },
    "scope": {
      "type": "string",
      "description": "`document` when the pages were searched joined (present only when options.scope is document)"
    },
    "separator": {
      "type": "string",
      "description": "What joined the pages (present only when options.scope is document)"
    },
    "aggregate": {
      "type": "object",
      "description": "How several members were combined — see `aggregate_detail`",
      "additionalProperties": true
    }
  }
}
```

### <a id="def-detector_detail"></a>`detector_detail`

```json
{
  "description": "An `detector` detail, or null (skipped, errored)",
  "anyOf": [
    {"type": "null"},
    {
      "if": {
        "properties": {
          "aggregate": {
            "properties": {
              "rule": {"const": "mean"}
            },
            "required": ["rule"]
          }
        },
        "required": ["aggregate"]
      },
      "then": {"$ref": "#/$defs/detail_detector_mean"},
      "else": {"$ref": "#/$defs/detail_detector"},
      "properties": {
        "aggregate": {"$ref": "#/$defs/aggregate_block"}
      }
    }
  ]
}
```

### <a id="def-document_entry"></a>`document_entry`

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["index", "filename", "kind", "pages", "items", "warnings", "document_info"],
  "properties": {
    "index": {"type": "integer", "minimum": 0},
    "filename": {"type": "string"},
    "kind": {"enum": ["image", "pdf", "svg", "txt", "docx"]},
    "pages": {"type": "integer", "minimum": 1},
    "items": {
      "type": "array",
      "items": {"type": "integer", "minimum": 0},
      "description": "Its global item indices"
    },
    "warnings": {
      "type": "array",
      "items": {"type": "string"},
      "description": "How the document was read that is worth knowing but is not an error (an SVG's external references); [] otherwise"
    },
    "document_info": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "content_type",
        "size_bytes",
        "has_image",
        "native_text_chars",
        "ocr",
        "llm_text_char_budget"
      ],
      "properties": {
        "content_type": {"type": "string"},
        "size_bytes": {"type": "integer", "minimum": 0},
        "has_image": {"type": "boolean", "description": "Any page has an image"},
        "native_text_chars": {"type": "integer", "minimum": 0},
        "ocr": {
          "type": "object",
          "additionalProperties": false,
          "required": ["engine", "min_native_chars", "layers", "document_layers"],
          "properties": {
            "engine": {"type": "string"},
            "min_native_chars": {"type": "integer"},
            "layers": {
              "type": "array",
              "items": {"$ref": "#/$defs/ocr_layer"}
            },
            "document_layers": {
              "type": "array",
              "items": {"$ref": "#/$defs/document_layer"}
            }
          }
        },
        "llm_text_char_budget": {"type": "integer", "description": "The job model's text budget"}
      }
    }
  }
}
```

### <a id="def-document_layer"></a>`document_layer`

```json
{
  "type": "object",
  "description": "The joined text a `scope: \"document\"` search read",
  "additionalProperties": false,
  "required": ["key", "source", "chars", "file"],
  "properties": {
    "key": {"type": "string"},
    "source": {"type": "string"},
    "chars": {"type": "integer", "minimum": 0},
    "file": {"type": "string", "description": "`text.d{index}.<key>.json`"}
  }
}
```

### <a id="def-file_label"></a>`file_label`

```json
{
  "type": "object",
  "description": "One file of the artifact directory, labelled (`regions.artifacts.FileLabeler`); `format` only for a layer or a text file",
  "additionalProperties": false,
  "required": ["name", "bytes", "content_type", "url", "kind", "item", "document", "criteria"],
  "properties": {
    "name": {"type": "string"},
    "bytes": {"type": "integer", "minimum": 0},
    "content_type": {"type": "string"},
    "url": {"type": "string"},
    "kind": {
      "type": "string",
      "description": "What the file is: base image, layer, regions, text, manifest, references"
    },
    "format": {"type": "string"},
    "item": {"type": ["integer", "null"]},
    "document": {"type": ["integer", "null"]},
    "criteria": {
      "type": "array",
      "items": {"type": "string"},
      "description": "The criteria the file belongs to"
    }
  }
}
```

### <a id="def-item_row"></a>`item_row`

```json
{
  "type": "object",
  "description": "One item's own weighted answer, from its per-criterion unit results (§ Scores per item, and overall)",
  "additionalProperties": false,
  "required": [
    "item",
    "document",
    "page",
    "filename",
    "overall_score",
    "overall_verdict",
    "complete"
  ],
  "properties": {
    "item": {"type": "integer", "minimum": 0},
    "document": {"type": "integer", "minimum": 0},
    "page": {"type": "integer", "minimum": 0, "description": "Within its document"},
    "filename": {"type": "string"},
    "overall_score": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
    "overall_verdict": {
      "anyOf": [
        {"type": "null"},
        {"enum": ["PASS", "MARGINAL", "FAIL"]}
      ]
    },
    "complete": {"type": "boolean"}
  }
}
```

### <a id="def-job_artifacts"></a>`job_artifacts`

```json
{
  "type": "object",
  "description": "The job's artifact directory",
  "additionalProperties": false,
  "required": ["dir", "files", "items", "zip_url", "total_bytes", "expires_at", "dropped", "notes"],
  "properties": {
    "dir": {"type": "string"},
    "files": {
      "type": "array",
      "items": {"$ref": "#/$defs/file_label"}
    },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["item", "layers"],
        "properties": {
          "item": {"type": "integer"},
          "layers": {"$ref": "#/$defs/layer_links"}
        }
      },
      "description": "Each item with a page image: its combined layer URLs"
    },
    "zip_url": {"type": "string"},
    "total_bytes": {"type": "integer", "minimum": 0},
    "expires_at": {"type": "string", "format": "date-time"},
    "dropped": {
      "type": "array",
      "items": {"type": "string"},
      "description": "Files the byte cap removed"
    },
    "notes": {
      "type": "array",
      "items": {"type": "string"}
    }
  }
}
```

### <a id="def-job_base"></a>`job_base`

```json
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
```

### <a id="def-job_id"></a>`job_id`

```json
{"type": "string", "pattern": "^[0-9a-f]{12}$", "description": "A job's id: 12 hex characters"}
```

### <a id="def-job_metadata"></a>`job_metadata`

```json
{
  "type": "object",
  "description": "`ClassifierMetadata` (left-out keys when unset) plus the progress snapshot",
  "additionalProperties": false,
  "required": ["type", "request_id"],
  "properties": {
    "type": {
      "enum": ["assess", "reference", "compare", "locate"],
      "description": "assess (POST /assess) or reference (the creation job POST /references queues); a row written by an older container may still say compare or locate (the worker fails it)"
    },
    "request_id": {
      "type": "string",
      "description": "The correlation id of the request that queued the job"
    },
    "reference_id": {
      "type": "string",
      "pattern": "^r[0-9a-f]{12}$",
      "description": "A reference job: the reference it builds"
    },
    "references": {
      "type": "array",
      "items": {"$ref": "#/$defs/reference_id"},
      "description": "An assess job: the reference ids it reads"
    },
    "progress": {"$ref": "#/$defs/progress"}
  }
}
```

### <a id="def-job_usage_block"></a>`job_usage_block`

```json
{
  "description": "What the job's model requests cost: GET /jobs/{id}/usage's `totals`, plus `usage_url` (added after the runner returns; left out when the read failed)",
  "type": "object",
  "allOf": [
    {"$ref": "#/$defs/usage_totals_fields"}
  ],
  "properties": {
    "models": {
      "type": "array",
      "items": {"type": "string"}
    },
    "first_call_at": {"type": ["string", "null"], "format": "date-time"},
    "last_call_at": {"type": ["string", "null"], "format": "date-time"},
    "usage_url": {"type": "string"}
  },
  "required": [
    "calls",
    "errors",
    "seconds",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "total_tokens",
    "models",
    "first_call_at",
    "last_call_at",
    "usage_url"
  ],
  "unevaluatedProperties": false
}
```

### <a id="def-JobBase"></a>`JobBase`

```json
{
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
```

### <a id="def-jobs_list_response"></a>`jobs_list_response`

```json
{
  "$comment": "generated from common.jobs.model.JobsListResponse — do not edit",
  "description": "Response body for ``GET /jobs``.\n\n``max_concurrent`` and ``available`` are populated by backends that have\na concurrency ceiling (like the in-memory backend fronted by a port pool);\npersistent backends leave them ``None``.",
  "properties": {
    "active_count": {"type": "integer"},
    "jobs": {
      "items": {"$ref": "#/$defs/JobBase"},
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
```

### <a id="def-layer_links"></a>`layer_links`

```json
{
  "type": "object",
  "description": "Layer URLs (rendered on first fetch)",
  "additionalProperties": false,
  "required": [],
  "properties": {
    "svg": {"type": "string"},
    "png": {"type": "string"},
    "preview": {"type": "string"}
  }
}
```

### <a id="def-llm_detail"></a>`llm_detail`

```json
{
  "description": "An `llm` detail, or null (skipped, errored)",
  "anyOf": [
    {"type": "null"},
    {
      "if": {
        "properties": {
          "aggregate": {
            "properties": {
              "rule": {"const": "mean"}
            },
            "required": ["rule"]
          }
        },
        "required": ["aggregate"]
      },
      "then": {"$ref": "#/$defs/detail_llm_mean"},
      "else": {"$ref": "#/$defs/detail_llm"},
      "properties": {
        "aggregate": {"$ref": "#/$defs/aggregate_block"}
      }
    }
  ]
}
```

### <a id="def-localization"></a>`localization`

```json
{
  "description": "The llm enforcement loop's record; `{attempts: [], accepted_attempt: null, calls: 0}` when it did not run (§ The LLM enforcement loop). With one item it is that item's record verbatim; with several (pages of a document, or documents) it is merged",
  "oneOf": [
    {
      "type": "object",
      "description": "One unit's record (a single item): attempts untagged, `accepted_attempt` the accepted attempt's number or null, no `accepted`",
      "additionalProperties": false,
      "required": ["attempts", "accepted_attempt", "calls"],
      "properties": {
        "attempts": {
          "type": "array",
          "items": {
            "allOf": [
              {"$ref": "#/$defs/box_attempt"},
              {
                "not": {"required": ["item"]}
              }
            ]
          }
        },
        "accepted_attempt": {"type": ["integer", "null"]},
        "calls": {"type": "integer", "minimum": 0},
        "references": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "item",
              "applied",
              "ask_images",
              "examples",
              "ask",
              "refine",
              "verify",
              "note"
            ],
            "properties": {
              "item": {"type": "integer"},
              "applied": {"type": "boolean"},
              "ask_images": {
                "anyOf": [
                  {"type": "null"},
                  {"enum": ["composite", "crop"]}
                ]
              },
              "examples": {
                "type": "array",
                "items": {
                  "type": "object",
                  "additionalProperties": true,
                  "description": "{reference_id, criterion, polarity, composite}"
                }
              },
              "ask": {"type": "boolean"},
              "refine": {"type": "boolean"},
              "verify": {"type": "boolean"},
              "note": {"type": ["string", "null"]}
            }
          },
          "description": "What the loop was shown, one entry per unit whose loop ran (only with references and a guide)"
        }
      }
    },
    {
      "type": "object",
      "description": "Merged across several items (a document's pages) or documents: every attempt tagged with its `item`, `accepted_attempt` null, `accepted` listing each accepted box",
      "additionalProperties": false,
      "required": ["attempts", "accepted_attempt", "accepted", "calls"],
      "properties": {
        "attempts": {
          "type": "array",
          "items": {
            "allOf": [
              {"$ref": "#/$defs/box_attempt"},
              {"required": ["item"]}
            ]
          }
        },
        "accepted_attempt": {"type": "null"},
        "accepted": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["item", "attempt"],
            "properties": {
              "item": {"type": "integer"},
              "attempt": {"type": "integer"}
            }
          },
          "description": "Each accepted box, by item and that item's attempt number"
        },
        "calls": {"type": "integer", "minimum": 0},
        "references": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "item",
              "applied",
              "ask_images",
              "examples",
              "ask",
              "refine",
              "verify",
              "note"
            ],
            "properties": {
              "item": {"type": "integer"},
              "applied": {"type": "boolean"},
              "ask_images": {
                "anyOf": [
                  {"type": "null"},
                  {"enum": ["composite", "crop"]}
                ]
              },
              "examples": {
                "type": "array",
                "items": {
                  "type": "object",
                  "additionalProperties": true,
                  "description": "{reference_id, criterion, polarity, composite}"
                }
              },
              "ask": {"type": "boolean"},
              "refine": {"type": "boolean"},
              "verify": {"type": "boolean"},
              "note": {"type": ["string", "null"]}
            }
          },
          "description": "What the loop was shown, one entry per unit whose loop ran (only with references and a guide)"
        }
      }
    }
  ]
}
```

### <a id="def-measurements_check_blur"></a>`measurements_check_blur`

```json
{
  "$comment": "generated from cv.quality.check_blur — do not edit",
  "type": "object",
  "description": "`measurements` of the detector (Laplacian variance)",
  "additionalProperties": false,
  "properties": {
    "laplacian_variance": {
      "type": "number",
      "description": "Variance of the Laplacian of the greyscale page — higher is sharper (unit: variance)"
    }
  }
}
```

### <a id="def-measurements_check_exposure"></a>`measurements_check_exposure`

```json
{
  "$comment": "generated from cv.quality.check_exposure — do not edit",
  "type": "object",
  "description": "`measurements` of the detector (Mean pixel intensity)",
  "additionalProperties": false,
  "properties": {
    "mean_intensity": {
      "type": "number",
      "description": "Mean greyscale value of the page, 0 (black) to 255 (white) (unit: intensity)"
    }
  }
}
```

### <a id="def-measurements_detect_faces"></a>`measurements_detect_faces`

```json
{
  "$comment": "generated from cv.features.detect_faces — do not edit",
  "type": "object",
  "description": "`measurements` of the detector (OpenCV Haar cascade)",
  "additionalProperties": false,
  "properties": {
    "faces_count": {
      "type": "number",
      "description": "Faces the score is based on: the strict pass's, else the loose pass's (unit: count)"
    },
    "faces_high_count": {
      "type": "number",
      "description": "Faces found by the strict pass (min_neighbors_high); null when unavailable (unit: count)"
    },
    "faces_low_count": {
      "type": "number",
      "description": "Faces found by the loose pass (min_neighbors_low); null when it did not run (unit: count)"
    }
  }
}
```

### <a id="def-measurements_detect_sky"></a>`measurements_detect_sky`

```json
{
  "$comment": "generated from cv.features.detect_sky — do not edit",
  "type": "object",
  "description": "`measurements` of the detector (Upper-region blue/grey analysis)",
  "additionalProperties": false,
  "properties": {
    "sky_ratio": {
      "type": "number",
      "description": "Share of the analysed top band that is sky-coloured (unit: ratio)"
    },
    "sky_px": {
      "type": "number",
      "description": "Sky-coloured pixels in the band (blue or grey) (unit: px)"
    },
    "blue_px": {"type": "number", "description": "Pixels matching the clear-blue range (unit: px)"},
    "grey_px": {
      "type": "number",
      "description": "Pixels matching the overcast-grey range (unit: px)"
    },
    "analysed_px": {
      "type": "number",
      "description": "Pixels in the top band (top_fraction of the page) (unit: px)"
    }
  }
}
```

### <a id="def-measurements_detect_text"></a>`measurements_detect_text`

```json
{
  "$comment": "generated from cv.features.detect_text — do not edit",
  "type": "object",
  "description": "`measurements` of the detector (Sobel edge density per block)",
  "additionalProperties": false,
  "properties": {
    "dense_block_ratio": {
      "type": "number",
      "description": "Share of blocks whose edge density is above block_density (unit: ratio)"
    },
    "dense_blocks_count": {
      "type": "number",
      "description": "Blocks above block_density (unit: count)"
    },
    "total_blocks_count": {
      "type": "number",
      "description": "Blocks the page was divided into (unit: count)"
    },
    "text_regions_count": {
      "type": "number",
      "description": "Merged text regions returned as boxes (unit: count)"
    }
  }
}
```

### <a id="def-measurements_detect_vegetation"></a>`measurements_detect_vegetation`

```json
{
  "$comment": "generated from cv.features.detect_vegetation — do not edit",
  "type": "object",
  "description": "`measurements` of the detector (HSV green masking)",
  "additionalProperties": false,
  "properties": {
    "green_ratio": {
      "type": "number",
      "description": "Share of the page's pixels inside the green mask (unit: ratio)"
    },
    "green_px": {"type": "number", "description": "Pixels inside the green mask (unit: px)"},
    "total_px": {"type": "number", "description": "Pixels in the page (unit: px)"}
  }
}
```

### <a id="def-measurements_detect_water"></a>`measurements_detect_water`

```json
{
  "$comment": "generated from cv.features.detect_water — do not edit",
  "type": "object",
  "description": "`measurements` of the detector (Blue/teal hue + flat-texture)",
  "additionalProperties": false,
  "properties": {
    "water_ratio": {
      "type": "number",
      "description": "Share of the page in blue blobs that passed the flat-texture test (unit: ratio)"
    },
    "water_px": {"type": "number", "description": "Area of those qualifying blobs (unit: px)"},
    "total_px": {"type": "number", "description": "Pixels in the page (unit: px)"},
    "blue_blobs_count": {
      "type": "number",
      "description": "Blue/teal blobs found, of any size (unit: count)"
    },
    "candidates_count": {
      "type": "number",
      "description": "Blobs large enough to test (min_contour_area_px) (unit: count)"
    },
    "flat_count": {
      "type": "number",
      "description": "Candidates flat enough to be water (unit: count)"
    },
    "rejected_textured_count": {
      "type": "number",
      "description": "Candidates rejected as too textured — a blue car, a shirt (unit: count)"
    }
  }
}
```

### <a id="def-ocr_layer"></a>`ocr_layer`

```json
{
  "type": "object",
  "description": "One text layer one item's criteria asked for (`text.p{item}.<key>.json`)",
  "additionalProperties": false,
  "required": ["item", "key", "mode", "ran", "source", "chars", "confidence", "note"],
  "properties": {
    "item": {"type": "integer"},
    "key": {"type": "string", "description": "The text-layer key (the OCR settings)"},
    "mode": {"enum": ["auto", "always", "never"]},
    "ran": {"type": "boolean", "description": "Whether OCR ran for this layer"},
    "source": {"type": "string", "description": "native | ocr | none"},
    "chars": {"type": "integer", "minimum": 0},
    "confidence": {"type": ["number", "null"]},
    "note": {"type": ["string", "null"]}
  }
}
```

### <a id="def-progress"></a>`progress`

```json
{
  "type": "object",
  "description": "How far along the job is (`common.jobs.progress`): the snapshot, flushed about once a second (§ Progress)",
  "additionalProperties": false,
  "required": [
    "state",
    "percent",
    "stage",
    "stage_title",
    "label",
    "index",
    "length",
    "attempt",
    "seq",
    "updated_at",
    "stages"
  ],
  "properties": {
    "state": {"enum": ["queued", "running", "done", "failed", "cancelled"]},
    "percent": {
      "type": "number",
      "minimum": 0,
      "maximum": 100,
      "description": "0-100, held just under 100 while the job runs"
    },
    "stage": {"type": ["string", "null"], "description": "The current stage's name"},
    "stage_title": {"type": ["string", "null"]},
    "label": {"type": ["string", "null"], "description": "What is happening now"},
    "index": {"type": "integer", "minimum": 0, "description": "Units done in the current stage"},
    "length": {"type": "integer", "minimum": 0, "description": "Units in the current stage"},
    "attempt": {
      "type": "integer",
      "minimum": 0,
      "description": "Which run of the job (a requeue is the next)"
    },
    "seq": {
      "type": "integer",
      "minimum": 0,
      "description": "The snapshot's sequence number in the job's history"
    },
    "updated_at": {"type": "string", "format": "date-time"},
    "stages": {
      "type": "array",
      "items": {"$ref": "#/$defs/progress_stage"}
    }
  }
}
```

### <a id="def-progress_stage"></a>`progress_stage`

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["name", "title", "weight", "index", "length"],
  "properties": {
    "name": {"type": "string"},
    "title": {"type": "string"},
    "weight": {"type": "number", "minimum": 0},
    "index": {"type": "integer", "minimum": 0},
    "length": {"type": "integer", "minimum": 0}
  }
}
```

### <a id="def-reference_id"></a>`reference_id`

```json
{
  "type": "string",
  "pattern": "^r[0-9a-f]{12}$",
  "description": "A reference's id: `r` and 12 hex characters"
}
```

### <a id="def-reference_result"></a>`reference_result`

```json
{
  "type": "object",
  "description": "A reference job's result: where the reference is, and the pipeline run (null when the caller supplied everything)",
  "additionalProperties": false,
  "required": [
    "reference_id",
    "status",
    "reference_url",
    "record_url",
    "description",
    "criteria",
    "warnings",
    "artifacts",
    "pipeline"
  ],
  "properties": {
    "reference_id": {"$ref": "#/$defs/reference_id"},
    "status": {"const": "ready"},
    "reference_url": {"type": "string"},
    "record_url": {"type": "string"},
    "description": {"type": ["string", "null"]},
    "criteria": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["expected", "region_source", "usable"],
        "properties": {
          "expected": {
            "anyOf": [
              {"type": "null"},
              {
                "type": "object",
                "additionalProperties": true,
                "description": "{score, verdict, reason, source}"
              }
            ]
          },
          "region_source": {"type": ["string", "null"]},
          "usable": {"type": ["boolean", "null"]}
        }
      }
    },
    "warnings": {
      "type": "array",
      "items": {"type": "string"}
    },
    "artifacts": {
      "anyOf": [
        {"type": "null"},
        {"$ref": "#/$defs/job_artifacts"}
      ]
    },
    "pipeline": {
      "anyOf": [
        {"type": "null"},
        {"$ref": "#/$defs/assess_result"}
      ]
    },
    "usage": {"$ref": "#/$defs/job_usage_block"}
  }
}
```

### <a id="def-references_summary"></a>`references_summary`

```json
{
  "type": "object",
  "description": "What the request's references did (§ Result shape, `references`)",
  "additionalProperties": false,
  "required": [
    "mode",
    "requested",
    "pool",
    "pool_truncated",
    "resolved",
    "inherited",
    "criteria",
    "selection",
    "calls",
    "selection_calls"
  ],
  "properties": {
    "mode": {"enum": ["explicit", "auto"]},
    "requested": {"description": "The ids (or the auto request) as sent"},
    "pool": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "array",
          "items": {"type": "string"}
        }
      ]
    },
    "pool_truncated": {"type": "boolean"},
    "resolved": {
      "type": "array",
      "items": {"type": "string"},
      "description": "The ids used"
    },
    "inherited": {
      "type": "boolean",
      "description": "The criteria were inherited from the references"
    },
    "criteria": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["matched", "matched_by", "use", "combine", "position", "examples"],
        "properties": {
          "matched": {"type": ["string", "null"]},
          "matched_by": {"enum": ["criterion", "key", "name"]},
          "use": {"type": "boolean"},
          "combine": {"type": "string"},
          "position": {"type": "string"},
          "examples": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": ["reference_id", "criterion", "polarity", "expected"],
              "properties": {
                "reference_id": {"$ref": "#/$defs/reference_id"},
                "criterion": {"type": "string"},
                "polarity": {"type": "string"},
                "expected": {
                  "type": "object",
                  "additionalProperties": true,
                  "description": "The expected answer: {score, verdict, reason, source}"
                }
              }
            }
          }
        }
      }
    },
    "selection": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "description": "One item's auto selection: {item, status: ok | failed | skipped, matches: [{id, confidence, reason}], dropped, error, reason}"
      }
    },
    "calls": {"type": "integer", "minimum": 0, "description": "Guided scoring calls"},
    "selection_calls": {"type": "integer", "minimum": 0}
  }
}
```

### <a id="def-region"></a>`region`

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["page", "kind", "points", "label", "score", "source", "attrs"],
  "properties": {
    "page": {"type": "integer", "minimum": 0, "description": "Its item"},
    "kind": {"enum": ["box", "polygon"]},
    "points": {
      "type": "array",
      "items": {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 2,
        "maxItems": 2
      },
      "description": "[[x, y], …] in original page pixels"
    },
    "label": {"type": "string"},
    "score": {"type": ["number", "null"], "description": "The producer's confidence"},
    "source": {"enum": ["cv", "ocr", "pdf-text", "detector", "llm"]},
    "attrs": {
      "type": "object",
      "additionalProperties": true,
      "description": "Free-form extras per source (`common.vision.Region.attrs`): a `text` snippet, `pdf_rect`, the llm loop's `attempt` / `accepted` / `verify_score`, …"
    }
  }
}
```

### <a id="def-text_detail"></a>`text_detail`

```json
{
  "description": "An `text` detail, or null (skipped, errored)",
  "anyOf": [
    {"type": "null"},
    {
      "if": {
        "properties": {
          "aggregate": {
            "properties": {
              "rule": {"const": "mean"}
            },
            "required": ["rule"]
          }
        },
        "required": ["aggregate"]
      },
      "then": {"$ref": "#/$defs/detail_text_mean"},
      "else": {"$ref": "#/$defs/detail_text"},
      "properties": {
        "aggregate": {"$ref": "#/$defs/aggregate_block"}
      }
    }
  ]
}
```

### <a id="def-text_link"></a>`text_link`

```json
{
  "type": "object",
  "description": "A text layer a unit read (`text.p{n}.<key>.json` / `text.d{i}.<key>.json`)",
  "additionalProperties": false,
  "required": ["key", "url", "source", "chars"],
  "properties": {
    "item": {"type": "integer"},
    "document": {"type": "integer"},
    "key": {"type": "string"},
    "url": {"type": "string"},
    "source": {"type": "string"},
    "chars": {"type": "integer", "minimum": 0}
  }
}
```

### <a id="def-unit_entry"></a>`unit_entry`

```json
{
  "type": "object",
  "description": "One unit: one item (or, document scope, one document)",
  "additionalProperties": false,
  "required": [
    "item",
    "document",
    "page",
    "status",
    "score",
    "verdict",
    "confidence",
    "reason",
    "detail",
    "error",
    "regions",
    "text_layer"
  ],
  "properties": {
    "item": {
      "type": ["integer", "null"],
      "description": "The global item; null for a document-scope unit"
    },
    "document": {"type": "integer", "minimum": 0},
    "page": {
      "type": ["integer", "null"],
      "description": "Within its document; null for a document-scope unit"
    },
    "items": {
      "type": "array",
      "items": {"type": "integer"},
      "description": "Document scope: the items it searched"
    },
    "status": {"enum": ["ok", "error", "skipped"]},
    "score": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
    "verdict": {
      "anyOf": [
        {"type": "null"},
        {"enum": ["PASS", "MARGINAL", "FAIL"]}
      ]
    },
    "confidence": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
    "reason": {"type": "string"},
    "detail": {"description": "The criterion's method's detail (see the criterion)"},
    "error": {"type": ["string", "null"]},
    "regions": {
      "type": "integer",
      "minimum": 0,
      "description": "A COUNT of the unit's regions (the geometry is the criterion's `regions`)"
    },
    "text_layer": {
      "anyOf": [
        {"type": "null"},
        {"$ref": "#/$defs/text_link"}
      ]
    },
    "localization": {"$ref": "#/$defs/localization"}
  }
}
```

### <a id="def-usage_totals_fields"></a>`usage_totals_fields`

```json
{
  "properties": {
    "calls": {"type": "integer", "minimum": 0},
    "errors": {"type": "integer", "minimum": 0},
    "seconds": {"type": "number", "minimum": 0},
    "prompt_tokens": {"type": ["integer", "null"]},
    "cached_tokens": {"type": ["integer", "null"]},
    "completion_tokens": {"type": ["integer", "null"]},
    "reasoning_tokens": {"type": ["integer", "null"]},
    "total_tokens": {"type": ["integer", "null"]}
  }
}
```
