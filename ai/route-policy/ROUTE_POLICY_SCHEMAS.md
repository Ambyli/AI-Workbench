# route-policy request and response schemas

Generated from [`route-policy.schemas.json`](route-policy.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`ROUTE_POLICY.md`](ROUTE_POLICY.md).

## `GET /v1/route-policy/health`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "rulesets", "bindings"],
  "properties": {
    "status": {"const": "ok"},
    "rulesets": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "A ruleset version.",
        "additionalProperties": false,
        "required": ["id", "version"],
        "properties": {
          "id": {"type": "string"},
          "version": {"type": "integer", "minimum": 1}
        }
      },
      "description": "The compiled versions this copy has cached"
    },
    "bindings": {"type": "integer", "minimum": 0, "description": "How many bindings it holds"}
  }
}
```

## `GET /v1/route-policy/ready`

Response `200` (`application/json`) — decisions can be logged right now:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "db"],
  "properties": {
    "status": {"const": "ready"},
    "db": {"const": "up"}
  }
}
```

Response `503` (`application/json`) — route-policy-db did not answer `SELECT 1`; the service still decides from its cached rulesets, only logging stops:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "db"],
  "properties": {
    "status": {"const": "not ready"},
    "db": {"const": "down"}
  }
}
```

## `GET /v1/route-policy/metrics`

Response `200` (`text/plain`) — Prometheus text (ROUTE_POLICY.md § Metrics).

## `GET /v1/route-policy/evaluator-types`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["evaluator_types", "combiner_types", "roles"],
  "properties": {
    "evaluator_types": {
      "type": "object",
      "description": "Every registered evaluator type, by name",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["vote_route", "config_schema", "output"],
        "properties": {
          "vote_route": {"enum": ["required", "chosen by the evaluator"]},
          "config_schema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema: the type's pydantic config model (model_json_schema())"
          },
          "output": {
            "type": "object",
            "description": "The DECLARED output (@declares)",
            "additionalProperties": false,
            "required": ["summary", "votes", "relevance", "detail", "notes"],
            "properties": {
              "summary": {"type": "string"},
              "votes": {"type": "string", "description": "When it votes, and for which route"},
              "relevance": {
                "type": ["string", "null"],
                "description": "What one exchange's relevance means; null when the type cannot judge single exchanges"
              },
              "detail": {
                "type": "object",
                "description": "Every detail key with its kind",
                "additionalProperties": {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["kind", "description"],
                  "properties": {
                    "kind": {"enum": ["string", "number", "integer", "boolean", "array", "object"]},
                    "description": {"type": "string"},
                    "nullable": {"const": true},
                    "when": {"type": "string", "description": "Present only in this case"}
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
    },
    "combiner_types": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "additionalProperties": false,
        "required": ["summary", "config_schema"],
        "properties": {
          "summary": {"type": "string"},
          "config_schema": {
            "type": "object",
            "additionalProperties": true,
            "description": "A JSON Schema: the type's pydantic config model (model_json_schema())"
          }
        }
      }
    },
    "roles": {
      "type": "array",
      "items": {"enum": ["vote", "force", "veto", "observe"]}
    }
  }
}
```

## `POST /v1/route-policy/decide`

Request body (`application/json`):

```json
{
  "$comment": "generated from route_policy.api.schemas.DecideRequest — do not edit",
  "properties": {
    "messages": {
      "items": {},
      "type": "array"
    },
    "tools": {
      "anyOf": [
        {
          "items": {},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null
    },
    "backends": {
      "anyOf": [
        {
          "additionalProperties": {
            "description": "One backend of the overflow layer's snapshot (key: its api_base).",
            "properties": {
              "verdict": {"default": "unknown", "maxLength": 32, "type": "string"},
              "waiting": {
                "anyOf": [
                  {"type": "number"},
                  {"type": "null"}
                ],
                "default": null
              },
              "running": {
                "anyOf": [
                  {"type": "number"},
                  {"type": "null"}
                ],
                "default": null
              }
            },
            "type": "object",
            "additionalProperties": true
          },
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null
    },
    "background_task": {"default": false, "type": "boolean"},
    "decision_id": {"format": "uuid", "type": "string"},
    "requested_model": {"maxLength": 256, "minLength": 1, "type": "string"},
    "litellm_call_id": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "chat_id": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "message_id": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "user_id": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "key_alias": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    }
  },
  "required": ["decision_id", "requested_model"],
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`) — the decision; a repeated decision_id gets the FIRST stored one:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["decision_id", "route", "mode", "apply", "ruleset", "votes", "scores", "latency_ms"],
  "properties": {
    "decision_id": {"type": "string", "format": "uuid"},
    "route": {
      "type": "string",
      "description": "The chain alias. A later call of a `pin: turn` turn gets the turn's first route, unless the fresh decision is local-private"
    },
    "mode": {"enum": ["shadow", "enforce"]},
    "apply": {
      "type": "boolean",
      "description": "True only for an `enforce` ruleset whose invariants held; LiteLLM applies `route` only when it is true"
    },
    "ruleset": {
      "type": "object",
      "description": "A ruleset version.",
      "additionalProperties": false,
      "required": ["id", "version"],
      "properties": {
        "id": {"type": "string"},
        "version": {"type": "integer", "minimum": 1}
      }
    },
    "votes": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One vote, in ruleset order, abstains included.",
        "allOf": [
          {
            "$comment": "The keys every vote carries: a fragment (no type, no additionalProperties) that `vote` and `evaluated_vote` close with unevaluatedProperties: false.",
            "description": "One evaluator's part in a decision. An abstain has `route` and `confidence` null and a reason.",
            "required": [
              "evaluator",
              "type",
              "route",
              "weight",
              "confidence",
              "role",
              "abstained",
              "reason",
              "latency_ms"
            ],
            "properties": {
              "evaluator": {"type": "string", "description": "The evaluator's id in the ruleset"},
              "type": {"type": "string", "description": "Its evaluator type (GET /evaluator-types)"},
              "route": {
                "type": ["string", "null"],
                "description": "The route it voted for; null when it abstained"
              },
              "weight": {"type": "number"},
              "confidence": {"type": ["number", "null"], "description": "Null when it abstained"},
              "role": {"enum": ["vote", "force", "veto", "observe"]},
              "abstained": {"type": "boolean"},
              "reason": {"type": "string"},
              "latency_ms": {"type": "number"}
            },
            "if": {
              "properties": {
                "abstained": {"const": true}
              },
              "required": ["abstained"]
            },
            "then": {
              "properties": {
                "route": {"type": "null"},
                "confidence": {"type": "null"}
              }
            },
            "else": {
              "properties": {
                "route": {"type": "string"},
                "confidence": {"type": "number"}
              }
            }
          }
        ],
        "unevaluatedProperties": false
      }
    },
    "scores": {
      "type": "object",
      "description": "Σ weight × confidence per route, over the non-abstaining `vote` and `force` votes.",
      "additionalProperties": {"type": "number"}
    },
    "latency_ms": {"type": "number"}
  }
}
```

## `POST /v1/route-policy/observe`

Request body (`application/json`):

```json
{
  "$comment": "generated from route_policy.api.schemas.ObserveRequest — do not edit",
  "properties": {
    "decision_id": {"format": "uuid", "type": "string"},
    "litellm_call_id": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "response_id": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "served_model_group": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "served_deployment": {
      "anyOf": [
        {"maxLength": 256, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "overflow_reason": {
      "anyOf": [
        {"maxLength": 32, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "finish_reason": {
      "anyOf": [
        {"maxLength": 64, "type": "string"},
        {"type": "null"}
      ],
      "default": null
    },
    "tool_calls": {
      "items": {"type": "string"},
      "maxItems": 256,
      "type": "array"
    },
    "status": {"default": "success", "maxLength": 16, "minLength": 1, "type": "string"},
    "error": {
      "anyOf": [
        {"type": "string"},
        {"type": "null"}
      ],
      "default": null
    }
  },
  "required": ["decision_id"],
  "type": "object",
  "additionalProperties": true
}
```

Response `202` (`application/json`):

```json
{
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["decision_id", "stored"],
      "properties": {
        "decision_id": {"type": "string", "format": "uuid"},
        "stored": {
          "type": "boolean",
          "description": "False when this decision_id already had an outcome (the first write wins)"
        }
      }
    },
    {
      "type": "object",
      "description": "The write outlasted ROUTE_POLICY_DECISION_WRITE_BUDGET_S and goes on in the background",
      "additionalProperties": false,
      "required": ["decision_id", "stored", "deferred"],
      "properties": {
        "decision_id": {"type": "string", "format": "uuid"},
        "stored": {"type": "null"},
        "deferred": {"const": true}
      }
    }
  ]
}
```

## `POST /v1/route-policy/evaluate`

Request body (`application/json`):

```json
{
  "$comment": "generated from route_policy.api.schemas.EvaluateRequest — do not edit",
  "description": "POST /evaluate: a stored ruleset (``ruleset``) or an unsaved one\n(``document``, refs allowed), run on ``messages`` / ``tools`` /\n``backends``. Writes nothing.",
  "properties": {
    "messages": {
      "items": {},
      "type": "array"
    },
    "tools": {
      "anyOf": [
        {
          "items": {},
          "type": "array"
        },
        {"type": "null"}
      ],
      "default": null
    },
    "backends": {
      "anyOf": [
        {
          "additionalProperties": {
            "description": "One backend of the overflow layer's snapshot (key: its api_base).",
            "properties": {
              "verdict": {"default": "unknown", "maxLength": 32, "type": "string"},
              "waiting": {
                "anyOf": [
                  {"type": "number"},
                  {"type": "null"}
                ],
                "default": null
              },
              "running": {
                "anyOf": [
                  {"type": "number"},
                  {"type": "null"}
                ],
                "default": null
              }
            },
            "type": "object",
            "additionalProperties": true
          },
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null
    },
    "background_task": {"default": false, "type": "boolean"},
    "ruleset": {
      "anyOf": [
        {
          "additionalProperties": false,
          "properties": {
            "id": {"maxLength": 64, "minLength": 1, "type": "string"},
            "version": {
              "anyOf": [
                {"minimum": 1, "type": "integer"},
                {"type": "null"}
              ],
              "default": null
            }
          },
          "required": ["id"],
          "type": "object"
        },
        {"type": "null"}
      ],
      "default": null
    },
    "document": {
      "anyOf": [
        {"additionalProperties": true, "type": "object"},
        {"type": "null"}
      ],
      "default": null
    },
    "requested_model": {"default": "auto", "maxLength": 256, "minLength": 1, "type": "string"}
  },
  "type": "object",
  "additionalProperties": true
}
```

Response `200` (`application/json`) — the /decide shape with decision_id null; each vote also carries its evaluator's `detail` and `error`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["decision_id", "route", "mode", "apply", "ruleset", "votes", "scores", "latency_ms"],
  "properties": {
    "decision_id": {"type": "null"},
    "route": {"type": "string"},
    "mode": {"enum": ["shadow", "enforce"]},
    "apply": {"type": "boolean"},
    "ruleset": {
      "type": "object",
      "description": "A ruleset version.",
      "additionalProperties": false,
      "required": ["id", "version"],
      "properties": {
        "id": {"type": "string"},
        "version": {"type": "integer", "minimum": 1}
      }
    },
    "votes": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One vote of a dry run: the /decide vote plus the evaluator's `detail` and `error`.",
        "allOf": [
          {
            "$comment": "The keys every vote carries: a fragment (no type, no additionalProperties) that `vote` and `evaluated_vote` close with unevaluatedProperties: false.",
            "description": "One evaluator's part in a decision. An abstain has `route` and `confidence` null and a reason.",
            "required": [
              "evaluator",
              "type",
              "route",
              "weight",
              "confidence",
              "role",
              "abstained",
              "reason",
              "latency_ms"
            ],
            "properties": {
              "evaluator": {"type": "string", "description": "The evaluator's id in the ruleset"},
              "type": {"type": "string", "description": "Its evaluator type (GET /evaluator-types)"},
              "route": {
                "type": ["string", "null"],
                "description": "The route it voted for; null when it abstained"
              },
              "weight": {"type": "number"},
              "confidence": {"type": ["number", "null"], "description": "Null when it abstained"},
              "role": {"enum": ["vote", "force", "veto", "observe"]},
              "abstained": {"type": "boolean"},
              "reason": {"type": "string"},
              "latency_ms": {"type": "number"}
            },
            "if": {
              "properties": {
                "abstained": {"const": true}
              },
              "required": ["abstained"]
            },
            "then": {
              "properties": {
                "route": {"type": "null"},
                "confidence": {"type": "null"}
              }
            },
            "else": {
              "properties": {
                "route": {"type": "string"},
                "confidence": {"type": "number"}
              }
            }
          },
          {
            "$comment": "generated from route_policy.evaluators.base.registry() — do not edit",
            "description": "A vote's `detail` is its evaluator type's declared output: `detail_<type>`.",
            "properties": {
              "type": {
                "enum": [
                  "backend_load",
                  "context_size",
                  "cooldown",
                  "language",
                  "llm_classifier",
                  "nearest_examples",
                  "pii",
                  "prompt_injection",
                  "regex_text",
                  "static",
                  "text_shape",
                  "tool_calls"
                ]
              }
            },
            "allOf": [
              {
                "if": {
                  "properties": {
                    "type": {"const": "backend_load"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.backend_load.BackendLoad.output — do not edit",
                      "type": "object",
                      "description": "Votes from the overflow layer's backend snapshot (busy / down / queue depth), so a ruleset can be load-aware without picking deployments.",
                      "additionalProperties": false,
                      "required": ["require", "watched", "qualifying"],
                      "properties": {
                        "require": {"type": "string", "description": "any | all"},
                        "watched": {
                          "type": "object",
                          "description": "{api_base: verdict} for every snapshot key a `backends` pattern matched",
                          "additionalProperties": true
                        },
                        "qualifying": {
                          "type": "array",
                          "description": "The watched api_bases that met the condition"
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "context_size"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.context_size.ContextSize.output — do not edit",
                      "type": "object",
                      "description": "The request's estimated size in tokens: JSON characters of messages + tools / chars_per_token (the LiteLLM overflow hook's estimate). Pure, no sidecar.",
                      "additionalProperties": false,
                      "required": ["estimated_tokens", "chars", "max_tokens"],
                      "properties": {
                        "estimated_tokens": {
                          "type": "integer",
                          "description": "JSON characters / chars_per_token, plus one"
                        },
                        "chars": {
                          "type": "integer",
                          "description": "JSON characters of messages + tools"
                        },
                        "max_tokens": {"type": "integer", "description": "The configured threshold"}
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "cooldown"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.cooldown.Cooldown.output — do not edit",
                      "type": "object",
                      "description": "Keeps voting for vote.route while the conversation's recent exchanges are relevant, for `exchanges` irrelevant exchanges after the last relevant one.",
                      "additionalProperties": false,
                      "required": [
                        "exchanges",
                        "relevant_if",
                        "relevant_now",
                        "last_relevant_ago",
                        "irrelevant_since",
                        "why",
                        "expired"
                      ],
                      "properties": {
                        "exchanges": {
                          "type": "integer",
                          "description": "The cooldown: irrelevant exchanges allowed after the last relevant one"
                        },
                        "relevant_if": {
                          "type": "array",
                          "description": "The evaluators asked, in order"
                        },
                        "relevant_now": {
                          "type": "boolean",
                          "description": "Whether the current exchange is relevant"
                        },
                        "last_relevant_ago": {
                          "type": ["integer", "null"],
                          "description": "Exchanges back from the current one to the last relevant one (0 = the current one); null when none was"
                        },
                        "irrelevant_since": {
                          "type": ["integer", "null"],
                          "description": "Irrelevant exchanges between that one and the current one; null when none was relevant"
                        },
                        "why": {
                          "type": ["string", "null"],
                          "description": "Why the last relevant exchange counted; null when none was"
                        },
                        "expired": {
                          "type": "boolean",
                          "description": "True when a relevant exchange exists but the cooldown has run out"
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "language"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.language.Language.output — do not edit",
                      "type": "object",
                      "description": "The latest user message's language (the signals sidecar's `language` task: fastText lid.176).",
                      "additionalProperties": false,
                      "required": ["language", "confidence", "status", "signals_ms"],
                      "properties": {
                        "language": {
                          "type": ["string", "null"],
                          "description": "The detected language code; null when not detected"
                        },
                        "confidence": {
                          "type": ["number", "null"],
                          "description": "The detector's confidence, 0-1; null when not detected"
                        },
                        "status": {
                          "type": "string",
                          "description": "The language task's status (evaluators/_signals.py)"
                        },
                        "signals_ms": {
                          "type": ["number", "null"],
                          "description": "The signals call's time; null when there was none"
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "llm_classifier"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.llm_classifier.LlmClassifier.output — do not edit",
                      "type": "object",
                      "description": "Votes the route a local model chose for the conversation -- classified in the background through the document classifier's POST /assess (one presence criterion per route), and applied from the next turn on.",
                      "additionalProperties": false,
                      "required": [
                        "model",
                        "user_messages",
                        "looked_up",
                        "lookup_error",
                        "input_key",
                        "exchange_index",
                        "age_exchanges",
                        "status",
                        "scores"
                      ],
                      "properties": {
                        "model": {
                          "type": "string",
                          "description": "The classifier registry id the verdicts come from"
                        },
                        "user_messages": {
                          "type": "integer",
                          "description": "User messages in the conversation (n)"
                        },
                        "looked_up": {
                          "type": "integer",
                          "description": "Verdict keys looked up: the prefixes ending at user messages n, n-1, ... n - max_age_exchanges"
                        },
                        "lookup_error": {
                          "type": ["string", "null"],
                          "description": "Why the verdicts could not be read (timed out | failed | unavailable (no database)); null when they were"
                        },
                        "input_key": {
                          "type": ["string", "null"],
                          "description": "The key of the verdict reported (used, or the failed / routeless one that made it abstain); null when none was found"
                        },
                        "exchange_index": {
                          "type": ["integer", "null"],
                          "description": "Which user message that verdict's prefix ends at (1-based)"
                        },
                        "age_exchanges": {
                          "type": ["integer", "null"],
                          "description": "How many user messages old that verdict is (0 = this one)"
                        },
                        "status": {
                          "type": ["string", "null"],
                          "description": "That verdict's status: ok | incomplete | unavailable | refused | failed | timeout | not_local"
                        },
                        "scores": {
                          "type": ["object", "null"],
                          "description": "That verdict's {route: 1-10 score} (null for an errored criterion)",
                          "additionalProperties": true
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "nearest_examples"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.nearest_examples.NearestExamples.output — do not edit",
                      "type": "object",
                      "description": "The route of the curated example prompts nearest to the latest user message: embedding kNN in the signals sidecar (`nearest` task), over an example set versioned with the ruleset.",
                      "additionalProperties": false,
                      "required": [
                        "model",
                        "status",
                        "fingerprint",
                        "neighbours",
                        "route_shares",
                        "best_similarity",
                        "signals_ms"
                      ],
                      "properties": {
                        "model": {
                          "type": ["string", "null"],
                          "description": "The embedding model the sidecar answered with (null when it did not)"
                        },
                        "status": {
                          "type": "string",
                          "description": "The nearest task's status (evaluators/_signals.py)"
                        },
                        "fingerprint": {
                          "type": "string",
                          "description": "SHA-256 of the example set + model pin"
                        },
                        "neighbours": {
                          "type": "array",
                          "description": "The k nearest examples [{id, route, similarity}], nearest first"
                        },
                        "route_shares": {
                          "type": "object",
                          "description": "{route: share of the top-k similarity mass}",
                          "additionalProperties": true
                        },
                        "best_similarity": {
                          "type": ["number", "null"],
                          "description": "The nearest example's similarity; null when unscored"
                        },
                        "signals_ms": {
                          "type": ["number", "null"],
                          "description": "The signals call's time; null when there was none"
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "pii"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.pii.Pii.output — do not edit",
                      "type": "object",
                      "description": "Sensitive data (email, phone, street address, government id, card / bank / account number) in the conversation's user messages or data-tool results: common.pii's patterns in-process, plus the signals sidecar's NER.",
                      "additionalProperties": false,
                      "required": [
                        "types",
                        "by_source",
                        "forced_by",
                        "forced_in",
                        "texts_in_scope",
                        "texts_scanned",
                        "chars_scanned",
                        "ner_status",
                        "ner_unscanned_texts",
                        "ner_model",
                        "signals_ms"
                      ],
                      "properties": {
                        "types": {
                          "type": "object",
                          "description": "{type: count} found in scope, both layers (per text the larger count)",
                          "additionalProperties": true
                        },
                        "by_source": {
                          "type": "object",
                          "description": "{patterns: {type: count}, ner: {type: count}}",
                          "additionalProperties": true
                        },
                        "forced_by": {
                          "type": "array",
                          "description": "The types that voted, sorted (`person+<type>` for a with_person pair)"
                        },
                        "forced_in": {
                          "type": "array",
                          "description": "Where they were found: latest_user | earlier_user | tool:<tool name>"
                        },
                        "texts_in_scope": {
                          "type": "integer",
                          "description": "Non-empty texts in scope"
                        },
                        "texts_scanned": {
                          "type": "integer",
                          "description": "Texts the patterns scanned (all of them unless past 200000 characters)"
                        },
                        "chars_scanned": {
                          "type": "integer",
                          "description": "Characters the patterns scanned"
                        },
                        "ner_status": {
                          "type": "string",
                          "description": "The NER task's status (evaluators/_signals.py): ok | partial | deadline | skipped | warming | error | unavailable | not_sent | missing | timed out | failed | not local | not fetched | conflict | model mismatch; off with `ner: false`"
                        },
                        "ner_unscanned_texts": {
                          "type": ["integer", "null"],
                          "description": "Texts in scope the NER did not scan completely; null with `ner: false`"
                        },
                        "ner_model": {
                          "type": ["string", "null"],
                          "description": "The NER model id the sidecar answered with"
                        },
                        "signals_ms": {
                          "type": ["number", "null"],
                          "description": "The signals call's time; null when there was none"
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "prompt_injection"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.prompt_injection.PromptInjection.output — do not edit",
                      "type": "object",
                      "description": "A jailbreak / prompt-injection score (the signals sidecar's `injection` task, a head and a tail window per text) on the latest user message, or on it and this exchange's tool results.",
                      "additionalProperties": false,
                      "required": [
                        "model",
                        "status",
                        "score",
                        "scored_texts",
                        "windows",
                        "signals_ms"
                      ],
                      "properties": {
                        "model": {
                          "type": ["string", "null"],
                          "description": "The model the sidecar answered with (null when it did not)"
                        },
                        "status": {
                          "type": "string",
                          "description": "The injection task's status (evaluators/_signals.py)"
                        },
                        "score": {
                          "type": ["number", "null"],
                          "description": "The highest score, 0-1; null when no text was scored"
                        },
                        "scored_texts": {"type": "integer", "description": "Texts that got a score"},
                        "windows": {
                          "type": "integer",
                          "description": "Windows scored across the texts (head + tail of max_tokens each; 1 for a short text)"
                        },
                        "signals_ms": {
                          "type": ["number", "null"],
                          "description": "The signals call's time; null when there was none"
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "regex_text"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.regex_text.RegexText.output — do not edit",
                      "type": "object",
                      "description": "A keyword rule ({name, operator, case_sensitive, method, keywords}; Go-RE2 semantics via common.keywordmatch) on the latest user message.",
                      "additionalProperties": false,
                      "required": [
                        "rule",
                        "scope",
                        "method",
                        "operator",
                        "matched",
                        "matched_patterns",
                        "searched_chars"
                      ],
                      "properties": {
                        "rule": {"type": "string", "description": "The rule's name (e.g. code_kw)"},
                        "scope": {
                          "type": "string",
                          "description": "Which text was searched: current_message"
                        },
                        "method": {
                          "type": "string",
                          "description": "regex (keywords are patterns) | literal (whole-word literals)"
                        },
                        "operator": {"type": "string", "description": "OR | AND | NOR"},
                        "matched": {
                          "type": "boolean",
                          "description": "Whether the rule matched (the vote)"
                        },
                        "matched_patterns": {
                          "type": "array",
                          "description": "Indices into the rule's keywords that were found"
                        },
                        "searched_chars": {
                          "type": "integer",
                          "description": "Length of the text searched"
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "static"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.static.Static.output — do not edit",
                      "type": "object",
                      "description": "A fixed vote (or a fixed abstain).",
                      "additionalProperties": false,
                      "required": [],
                      "properties": {}
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "text_shape"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.text_shape.TextShape.output — do not edit",
                      "type": "object",
                      "description": "The shape of the latest user message: lines, code fences, code-looking lines, URLs, attachments. Pure, no sidecar.",
                      "additionalProperties": false,
                      "required": [
                        "chars",
                        "lines",
                        "fenced_blocks",
                        "code_lines",
                        "code_ratio",
                        "urls",
                        "attachments"
                      ],
                      "properties": {
                        "chars": {"type": "integer", "description": "Characters of the user text"},
                        "lines": {"type": "integer", "description": "Non-empty lines"},
                        "fenced_blocks": {
                          "type": "integer",
                          "description": "``` / ~~~ fenced blocks"
                        },
                        "code_lines": {
                          "type": "integer",
                          "description": "Lines that look like code (inside a fence, or CODE_LINE)"
                        },
                        "code_ratio": {
                          "type": "number",
                          "description": "code_lines / non-empty lines outside the fence markers"
                        },
                        "urls": {"type": "integer", "description": "http(s) URLs"},
                        "attachments": {
                          "type": "object",
                          "description": "{image, file, audio}: content parts of the latest user message by type",
                          "additionalProperties": true
                        }
                      }
                    }
                  }
                }
              },
              {
                "if": {
                  "properties": {
                    "type": {"const": "tool_calls"}
                  },
                  "required": ["type"]
                },
                "then": {
                  "properties": {
                    "detail": {
                      "$comment": "generated from route_policy.evaluators.tool_calls.ToolCalls.output — do not edit",
                      "type": "object",
                      "description": "A tool the conversation CALLED (not merely offered), matched by a full-name regex.",
                      "additionalProperties": false,
                      "required": ["scope", "pattern", "called", "matched"],
                      "properties": {
                        "scope": {
                          "type": "string",
                          "description": "current_exchange | conversation"
                        },
                        "pattern": {"type": "string", "description": "The full-name pattern"},
                        "called": {
                          "type": "array",
                          "description": "Tools called in scope, most recent first (at most 20)"
                        },
                        "matched": {
                          "type": ["string", "null"],
                          "description": "The most recent called tool that matched, or null"
                        }
                      }
                    }
                  }
                }
              }
            ]
          }
        ],
        "required": ["detail", "error"],
        "properties": {
          "detail": {
            "type": "object",
            "additionalProperties": true,
            "description": "The evaluator's declared output (GET /evaluator-types), checked per `type` by `vote_detail`"
          },
          "error": {
            "type": ["string", "null"],
            "description": "Why the evaluator failed (its exception's type name), or null"
          }
        },
        "unevaluatedProperties": false
      }
    },
    "scores": {
      "type": "object",
      "description": "Σ weight × confidence per route, over the non-abstaining `vote` and `force` votes.",
      "additionalProperties": {"type": "number"}
    },
    "latency_ms": {"type": "number"}
  }
}
```

Response `422` (`application/json`) — the document is not a valid ruleset or its refs cannot be resolved; a body FastAPI rejects (not exactly one of `ruleset` / `document`, wrong types) gets FastAPI's list:

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
          "additionalProperties": false,
          "required": ["message", "errors"],
          "properties": {
            "message": {"type": "string"},
            "errors": {
              "type": "array",
              "items": {"type": "string"},
              "description": "Every problem of the document"
            }
          }
        },
        {
          "type": "array",
          "items": {
            "type": "object",
            "description": "One problem of FastAPI's 422 list.",
            "additionalProperties": false,
            "required": ["type", "loc", "msg"],
            "properties": {
              "type": {"type": "string"},
              "loc": {
                "type": "array",
                "items": {"type": ["string", "integer"]}
              },
              "msg": {"type": "string"},
              "input": {"description": "What was sent at `loc`"},
              "ctx": {"type": "object", "additionalProperties": true}
            }
          }
        }
      ]
    }
  }
}
```

## `GET /v1/route-policy/rulesets`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["rulesets"],
  "properties": {
    "rulesets": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One stored version, without its document.",
        "allOf": [
          {
            "$comment": "The keys every stored ruleset version carries: a fragment closed by the schemas that use it (unevaluatedProperties: false).",
            "required": [
              "id",
              "version",
              "sha256",
              "source",
              "created_at",
              "created_by",
              "active_from",
              "active_to",
              "mode",
              "pin",
              "description"
            ],
            "properties": {
              "id": {"type": "string"},
              "version": {"type": "integer", "minimum": 1},
              "sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "The content hash of the resolved document (refs inlined)"
              },
              "source": {
                "type": ["string", "null"],
                "description": "Where it came from, e.g. rulesets/default.yaml"
              },
              "created_at": {"type": "string", "format": "date-time"},
              "created_by": {"type": "string"},
              "active_from": {
                "type": ["string", "null"],
                "format": "date-time",
                "description": "The first time a binding or a shadow slot pointed at it"
              },
              "active_to": {
                "type": ["string", "null"],
                "format": "date-time",
                "description": "When no binding or shadow slot points at it any more"
              },
              "mode": {"enum": ["shadow", "enforce"]},
              "pin": {"enum": ["turn", null]},
              "description": {"type": ["string", "null"]}
            }
          }
        ],
        "unevaluatedProperties": false
      },
      "description": "Every stored version, ordered by id and version"
    }
  }
}
```

## `GET /v1/route-policy/rulesets/:rulesetId`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "description": "The highest version with its document",
  "allOf": [
    {
      "$comment": "The keys every stored ruleset version carries: a fragment closed by the schemas that use it (unevaluatedProperties: false).",
      "required": [
        "id",
        "version",
        "sha256",
        "source",
        "created_at",
        "created_by",
        "active_from",
        "active_to",
        "mode",
        "pin",
        "description"
      ],
      "properties": {
        "id": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "sha256": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$",
          "description": "The content hash of the resolved document (refs inlined)"
        },
        "source": {
          "type": ["string", "null"],
          "description": "Where it came from, e.g. rulesets/default.yaml"
        },
        "created_at": {"type": "string", "format": "date-time"},
        "created_by": {"type": "string"},
        "active_from": {
          "type": ["string", "null"],
          "format": "date-time",
          "description": "The first time a binding or a shadow slot pointed at it"
        },
        "active_to": {
          "type": ["string", "null"],
          "format": "date-time",
          "description": "When no binding or shadow slot points at it any more"
        },
        "mode": {"enum": ["shadow", "enforce"]},
        "pin": {"enum": ["turn", null]},
        "description": {"type": ["string", "null"]}
      }
    }
  ],
  "required": ["document", "versions"],
  "properties": {
    "document": {
      "$comment": "generated from route_policy.rulesets.model.RulesetDocument — do not edit",
      "additionalProperties": false,
      "properties": {
        "id": {"pattern": "^[a-z0-9][a-z0-9_-]{0,63}$", "type": "string"},
        "version": {"minimum": 1, "type": "integer"},
        "description": {"default": "", "maxLength": 4000, "type": "string"},
        "mode": {"enum": ["shadow", "enforce"], "type": "string"},
        "pin": {
          "anyOf": [
            {"const": "turn", "type": "string"},
            {"type": "null"}
          ],
          "default": null
        },
        "routes": {
          "items": {"type": "string"},
          "minItems": 1,
          "type": "array"
        },
        "default_route": {"type": "string"},
        "evaluators": {
          "items": {
            "additionalProperties": false,
            "properties": {
              "id": {"pattern": "^[a-z0-9][a-z0-9_-]{0,63}$", "type": "string"},
              "type": {"maxLength": 64, "minLength": 1, "type": "string"},
              "role": {
                "default": "vote",
                "enum": ["vote", "force", "veto", "observe"],
                "type": "string"
              },
              "description": {
                "anyOf": [
                  {"maxLength": 2000, "type": "string"},
                  {"type": "null"}
                ],
                "default": null
              },
              "config": {"additionalProperties": true, "type": "object"},
              "vote": {
                "additionalProperties": false,
                "properties": {
                  "route": {
                    "anyOf": [
                      {"pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", "type": "string"},
                      {"type": "null"}
                    ],
                    "default": null
                  },
                  "weight": {"default": 1.0, "maximum": 1000.0, "minimum": 0.0, "type": "number"}
                },
                "type": "object"
              }
            },
            "required": ["id", "type"],
            "type": "object"
          },
          "type": "array"
        },
        "combiner": {
          "additionalProperties": true,
          "description": "``{type: <name>, ...options}`` -- the options are validated by the\ncombiner type's own Config model.",
          "properties": {
            "type": {"default": "priority", "type": "string"}
          },
          "type": "object"
        }
      },
      "required": ["id", "version", "mode", "routes", "default_route"],
      "type": "object"
    },
    "versions": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "One stored version, without its document.",
        "allOf": [
          {
            "$comment": "The keys every stored ruleset version carries: a fragment closed by the schemas that use it (unevaluatedProperties: false).",
            "required": [
              "id",
              "version",
              "sha256",
              "source",
              "created_at",
              "created_by",
              "active_from",
              "active_to",
              "mode",
              "pin",
              "description"
            ],
            "properties": {
              "id": {"type": "string"},
              "version": {"type": "integer", "minimum": 1},
              "sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "The content hash of the resolved document (refs inlined)"
              },
              "source": {
                "type": ["string", "null"],
                "description": "Where it came from, e.g. rulesets/default.yaml"
              },
              "created_at": {"type": "string", "format": "date-time"},
              "created_by": {"type": "string"},
              "active_from": {
                "type": ["string", "null"],
                "format": "date-time",
                "description": "The first time a binding or a shadow slot pointed at it"
              },
              "active_to": {
                "type": ["string", "null"],
                "format": "date-time",
                "description": "When no binding or shadow slot points at it any more"
              },
              "mode": {"enum": ["shadow", "enforce"]},
              "pin": {"enum": ["turn", null]},
              "description": {"type": ["string", "null"]}
            }
          }
        ],
        "unevaluatedProperties": false
      },
      "description": "Every stored version's summary, oldest first"
    }
  },
  "unevaluatedProperties": false
}
```

## `GET /v1/route-policy/rulesets/:rulesetId/:rulesetVersion`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "description": "One stored version with its document (refs inlined).",
  "allOf": [
    {
      "$comment": "The keys every stored ruleset version carries: a fragment closed by the schemas that use it (unevaluatedProperties: false).",
      "required": [
        "id",
        "version",
        "sha256",
        "source",
        "created_at",
        "created_by",
        "active_from",
        "active_to",
        "mode",
        "pin",
        "description"
      ],
      "properties": {
        "id": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "sha256": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$",
          "description": "The content hash of the resolved document (refs inlined)"
        },
        "source": {
          "type": ["string", "null"],
          "description": "Where it came from, e.g. rulesets/default.yaml"
        },
        "created_at": {"type": "string", "format": "date-time"},
        "created_by": {"type": "string"},
        "active_from": {
          "type": ["string", "null"],
          "format": "date-time",
          "description": "The first time a binding or a shadow slot pointed at it"
        },
        "active_to": {
          "type": ["string", "null"],
          "format": "date-time",
          "description": "When no binding or shadow slot points at it any more"
        },
        "mode": {"enum": ["shadow", "enforce"]},
        "pin": {"enum": ["turn", null]},
        "description": {"type": ["string", "null"]}
      }
    }
  ],
  "required": ["document"],
  "properties": {
    "document": {
      "$comment": "generated from route_policy.rulesets.model.RulesetDocument — do not edit",
      "additionalProperties": false,
      "properties": {
        "id": {"pattern": "^[a-z0-9][a-z0-9_-]{0,63}$", "type": "string"},
        "version": {"minimum": 1, "type": "integer"},
        "description": {"default": "", "maxLength": 4000, "type": "string"},
        "mode": {"enum": ["shadow", "enforce"], "type": "string"},
        "pin": {
          "anyOf": [
            {"const": "turn", "type": "string"},
            {"type": "null"}
          ],
          "default": null
        },
        "routes": {
          "items": {"type": "string"},
          "minItems": 1,
          "type": "array"
        },
        "default_route": {"type": "string"},
        "evaluators": {
          "items": {
            "additionalProperties": false,
            "properties": {
              "id": {"pattern": "^[a-z0-9][a-z0-9_-]{0,63}$", "type": "string"},
              "type": {"maxLength": 64, "minLength": 1, "type": "string"},
              "role": {
                "default": "vote",
                "enum": ["vote", "force", "veto", "observe"],
                "type": "string"
              },
              "description": {
                "anyOf": [
                  {"maxLength": 2000, "type": "string"},
                  {"type": "null"}
                ],
                "default": null
              },
              "config": {"additionalProperties": true, "type": "object"},
              "vote": {
                "additionalProperties": false,
                "properties": {
                  "route": {
                    "anyOf": [
                      {"pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", "type": "string"},
                      {"type": "null"}
                    ],
                    "default": null
                  },
                  "weight": {"default": 1.0, "maximum": 1000.0, "minimum": 0.0, "type": "number"}
                },
                "type": "object"
              }
            },
            "required": ["id", "type"],
            "type": "object"
          },
          "type": "array"
        },
        "combiner": {
          "additionalProperties": true,
          "description": "``{type: <name>, ...options}`` -- the options are validated by the\ncombiner type's own Config model.",
          "properties": {
            "type": {"default": "priority", "type": "string"}
          },
          "type": "object"
        }
      },
      "required": ["id", "version", "mode", "routes", "default_route"],
      "type": "object"
    }
  },
  "unevaluatedProperties": false
}
```

## `GET /v1/route-policy/bindings`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["bindings"],
  "properties": {
    "bindings": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "Requested model (+ key alias) -> ruleset version, with its shadow slots.",
        "additionalProperties": false,
        "required": [
          "scope_model",
          "scope_key_alias",
          "ruleset_id",
          "version",
          "updated_at",
          "updated_by",
          "shadows"
        ],
        "properties": {
          "scope_model": {"type": "string", "description": "The requested model it binds (auto)"},
          "scope_key_alias": {"type": ["string", "null"]},
          "ruleset_id": {"type": "string"},
          "version": {"type": "integer", "minimum": 1},
          "updated_at": {"type": "string", "format": "date-time"},
          "updated_by": {"type": "string"},
          "shadows": {
            "type": "array",
            "description": "The shadow slots, in order ([] when it has none)",
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": ["ruleset_id", "version"],
              "properties": {
                "ruleset_id": {"type": "string"},
                "version": {"type": "integer", "minimum": 1}
              }
            }
          }
        }
      }
    }
  }
}
```

## `GET /v1/route-policy/chats/:chatId/decisions`

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["chat_id", "decisions"],
  "properties": {
    "chat_id": {"type": "string"},
    "decisions": {
      "type": "array",
      "items": {
        "type": "object",
        "description": "A primary decision with its votes, its outcome and its shadows.",
        "allOf": [
          {
            "$comment": "The keys every decision of GET /chats/:chatId/decisions carries: a fragment closed by the schemas that use it (unevaluatedProperties: false).",
            "required": [
              "decision_id",
              "created_at",
              "message_id",
              "litellm_call_id",
              "requested_model",
              "shadow_of",
              "pinned_from",
              "background_task",
              "route",
              "applied",
              "mode",
              "ruleset",
              "reason",
              "scores",
              "votes",
              "latency_ms",
              "error",
              "outcome"
            ],
            "properties": {
              "decision_id": {"type": "string", "format": "uuid"},
              "created_at": {"type": "string", "format": "date-time"},
              "message_id": {"type": ["string", "null"]},
              "litellm_call_id": {"type": ["string", "null"]},
              "requested_model": {"type": "string"},
              "shadow_of": {
                "type": ["string", "null"],
                "format": "uuid",
                "description": "The primary's decision_id on a shadow decision; null on a primary"
              },
              "pinned_from": {
                "type": ["string", "null"],
                "format": "uuid",
                "description": "The turn's first decision, on a later call of a `pin: turn` turn; null otherwise"
              },
              "background_task": {"type": "boolean"},
              "route": {"type": "string"},
              "applied": {"type": "boolean"},
              "mode": {"enum": ["shadow", "enforce"]},
              "ruleset": {
                "type": "object",
                "description": "A ruleset version.",
                "additionalProperties": false,
                "required": ["id", "version"],
                "properties": {
                  "id": {"type": "string"},
                  "version": {"type": "integer", "minimum": 1}
                }
              },
              "reason": {"type": ["string", "null"]},
              "scores": {
                "type": "object",
                "description": "Σ weight × confidence per route, over the non-abstaining `vote` and `force` votes.",
                "additionalProperties": {"type": "number"}
              },
              "votes": {
                "type": "array",
                "items": {
                  "type": "object",
                  "description": "One vote, in ruleset order, abstains included.",
                  "allOf": [
                    {
                      "$comment": "The keys every vote carries: a fragment (no type, no additionalProperties) that `vote` and `evaluated_vote` close with unevaluatedProperties: false.",
                      "description": "One evaluator's part in a decision. An abstain has `route` and `confidence` null and a reason.",
                      "required": [
                        "evaluator",
                        "type",
                        "route",
                        "weight",
                        "confidence",
                        "role",
                        "abstained",
                        "reason",
                        "latency_ms"
                      ],
                      "properties": {
                        "evaluator": {
                          "type": "string",
                          "description": "The evaluator's id in the ruleset"
                        },
                        "type": {
                          "type": "string",
                          "description": "Its evaluator type (GET /evaluator-types)"
                        },
                        "route": {
                          "type": ["string", "null"],
                          "description": "The route it voted for; null when it abstained"
                        },
                        "weight": {"type": "number"},
                        "confidence": {
                          "type": ["number", "null"],
                          "description": "Null when it abstained"
                        },
                        "role": {"enum": ["vote", "force", "veto", "observe"]},
                        "abstained": {"type": "boolean"},
                        "reason": {"type": "string"},
                        "latency_ms": {"type": "number"}
                      },
                      "if": {
                        "properties": {
                          "abstained": {"const": true}
                        },
                        "required": ["abstained"]
                      },
                      "then": {
                        "properties": {
                          "route": {"type": "null"},
                          "confidence": {"type": "null"}
                        }
                      },
                      "else": {
                        "properties": {
                          "route": {"type": "string"},
                          "confidence": {"type": "number"}
                        }
                      }
                    }
                  ],
                  "unevaluatedProperties": false
                }
              },
              "latency_ms": {"type": "number"},
              "error": {"type": ["string", "null"]},
              "outcome": {
                "anyOf": [
                  {
                    "type": "object",
                    "description": "What really answered (POST /observe).",
                    "additionalProperties": false,
                    "required": [
                      "observed_at",
                      "response_id",
                      "served_model_group",
                      "served_deployment",
                      "overflow_reason",
                      "finish_reason",
                      "tool_calls",
                      "status",
                      "error",
                      "agree"
                    ],
                    "properties": {
                      "observed_at": {"type": "string", "format": "date-time"},
                      "response_id": {"type": ["string", "null"]},
                      "served_model_group": {
                        "type": ["string", "null"],
                        "description": "The chain that answered"
                      },
                      "served_deployment": {"type": ["string", "null"]},
                      "overflow_reason": {"type": ["string", "null"]},
                      "finish_reason": {"type": ["string", "null"]},
                      "tool_calls": {
                        "type": "array",
                        "items": {"type": "string"}
                      },
                      "status": {"type": "string"},
                      "error": {"type": ["string", "null"]},
                      "agree": {
                        "type": "boolean",
                        "description": "The chosen route == the served chain"
                      }
                    }
                  },
                  {"type": "null"}
                ],
                "description": "Null until /observe arrives; always null on a shadow decision"
              }
            }
          }
        ],
        "required": ["shadows"],
        "properties": {
          "shadows": {
            "type": "array",
            "items": {
              "type": "object",
              "description": "A shadow decision: the primary's keys except `shadows`.",
              "allOf": [
                {
                  "$comment": "The keys every decision of GET /chats/:chatId/decisions carries: a fragment closed by the schemas that use it (unevaluatedProperties: false).",
                  "required": [
                    "decision_id",
                    "created_at",
                    "message_id",
                    "litellm_call_id",
                    "requested_model",
                    "shadow_of",
                    "pinned_from",
                    "background_task",
                    "route",
                    "applied",
                    "mode",
                    "ruleset",
                    "reason",
                    "scores",
                    "votes",
                    "latency_ms",
                    "error",
                    "outcome"
                  ],
                  "properties": {
                    "decision_id": {"type": "string", "format": "uuid"},
                    "created_at": {"type": "string", "format": "date-time"},
                    "message_id": {"type": ["string", "null"]},
                    "litellm_call_id": {"type": ["string", "null"]},
                    "requested_model": {"type": "string"},
                    "shadow_of": {
                      "type": ["string", "null"],
                      "format": "uuid",
                      "description": "The primary's decision_id on a shadow decision; null on a primary"
                    },
                    "pinned_from": {
                      "type": ["string", "null"],
                      "format": "uuid",
                      "description": "The turn's first decision, on a later call of a `pin: turn` turn; null otherwise"
                    },
                    "background_task": {"type": "boolean"},
                    "route": {"type": "string"},
                    "applied": {"type": "boolean"},
                    "mode": {"enum": ["shadow", "enforce"]},
                    "ruleset": {
                      "type": "object",
                      "description": "A ruleset version.",
                      "additionalProperties": false,
                      "required": ["id", "version"],
                      "properties": {
                        "id": {"type": "string"},
                        "version": {"type": "integer", "minimum": 1}
                      }
                    },
                    "reason": {"type": ["string", "null"]},
                    "scores": {
                      "type": "object",
                      "description": "Σ weight × confidence per route, over the non-abstaining `vote` and `force` votes.",
                      "additionalProperties": {"type": "number"}
                    },
                    "votes": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "description": "One vote, in ruleset order, abstains included.",
                        "allOf": [
                          {
                            "$comment": "The keys every vote carries: a fragment (no type, no additionalProperties) that `vote` and `evaluated_vote` close with unevaluatedProperties: false.",
                            "description": "One evaluator's part in a decision. An abstain has `route` and `confidence` null and a reason.",
                            "required": [
                              "evaluator",
                              "type",
                              "route",
                              "weight",
                              "confidence",
                              "role",
                              "abstained",
                              "reason",
                              "latency_ms"
                            ],
                            "properties": {
                              "evaluator": {
                                "type": "string",
                                "description": "The evaluator's id in the ruleset"
                              },
                              "type": {
                                "type": "string",
                                "description": "Its evaluator type (GET /evaluator-types)"
                              },
                              "route": {
                                "type": ["string", "null"],
                                "description": "The route it voted for; null when it abstained"
                              },
                              "weight": {"type": "number"},
                              "confidence": {
                                "type": ["number", "null"],
                                "description": "Null when it abstained"
                              },
                              "role": {"enum": ["vote", "force", "veto", "observe"]},
                              "abstained": {"type": "boolean"},
                              "reason": {"type": "string"},
                              "latency_ms": {"type": "number"}
                            },
                            "if": {
                              "properties": {
                                "abstained": {"const": true}
                              },
                              "required": ["abstained"]
                            },
                            "then": {
                              "properties": {
                                "route": {"type": "null"},
                                "confidence": {"type": "null"}
                              }
                            },
                            "else": {
                              "properties": {
                                "route": {"type": "string"},
                                "confidence": {"type": "number"}
                              }
                            }
                          }
                        ],
                        "unevaluatedProperties": false
                      }
                    },
                    "latency_ms": {"type": "number"},
                    "error": {"type": ["string", "null"]},
                    "outcome": {
                      "anyOf": [
                        {
                          "type": "object",
                          "description": "What really answered (POST /observe).",
                          "additionalProperties": false,
                          "required": [
                            "observed_at",
                            "response_id",
                            "served_model_group",
                            "served_deployment",
                            "overflow_reason",
                            "finish_reason",
                            "tool_calls",
                            "status",
                            "error",
                            "agree"
                          ],
                          "properties": {
                            "observed_at": {"type": "string", "format": "date-time"},
                            "response_id": {"type": ["string", "null"]},
                            "served_model_group": {
                              "type": ["string", "null"],
                              "description": "The chain that answered"
                            },
                            "served_deployment": {"type": ["string", "null"]},
                            "overflow_reason": {"type": ["string", "null"]},
                            "finish_reason": {"type": ["string", "null"]},
                            "tool_calls": {
                              "type": "array",
                              "items": {"type": "string"}
                            },
                            "status": {"type": "string"},
                            "error": {"type": ["string", "null"]},
                            "agree": {
                              "type": "boolean",
                              "description": "The chosen route == the served chain"
                            }
                          }
                        },
                        {"type": "null"}
                      ],
                      "description": "Null until /observe arrives; always null on a shadow decision"
                    }
                  }
                }
              ],
              "unevaluatedProperties": false
            },
            "description": "The decisions the binding's shadow rulesets made beside it"
          }
        },
        "unevaluatedProperties": false
      },
      "description": "The latest `limit` primary decisions, oldest first ([] for an unknown chat)"
    }
  }
}
```
