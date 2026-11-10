# roofix request and response schemas

Generated from [`roofix.schemas.json`](roofix.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`ROOFIX.md`](ROOFIX.md).

## `GET /health`

Response `200` (`application/json`) — the process is up; Gmail, Phoenix and roofix-db are not checked:

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

## `GET /status`

Response `200` (`application/json`) — the in-memory tick counters (they reset on a restart):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "started_at",
    "last_tick_at",
    "last_tick_ok",
    "last_tick_error",
    "tick_count",
    "decisions_total",
    "decisions_by_action",
    "decisions_by_source",
    "escalations_total",
    "phoenix_write_failures",
    "dry_run",
    "agent_phase",
    "tick_interval_seconds"
  ],
  "properties": {
    "started_at": {"type": "string", "format": "date-time"},
    "last_tick_at": {"type": ["string", "null"], "format": "date-time"},
    "last_tick_ok": {"type": ["boolean", "null"]},
    "last_tick_error": {
      "type": ["string", "null"],
      "description": "repr() of the exception the last tick raised"
    },
    "tick_count": {"type": "integer", "minimum": 0},
    "decisions_total": {"type": "integer", "minimum": 0},
    "decisions_by_action": {
      "type": "object",
      "additionalProperties": {"type": "integer", "minimum": 1}
    },
    "decisions_by_source": {
      "type": "object",
      "propertyNames": {"enum": ["rule", "ai"]},
      "additionalProperties": {"type": "integer", "minimum": 1}
    },
    "escalations_total": {
      "type": "integer",
      "minimum": 0,
      "description": "Decisions with needs_human or action escalate"
    },
    "phoenix_write_failures": {"const": 0, "description": "Never incremented by the code: always 0"},
    "dry_run": {
      "type": "boolean",
      "description": "The effective DRY_RUN (default true): check it before /tick"
    },
    "agent_phase": {
      "type": "string",
      "description": "AGENT_PHASE as set: `0` chatter + milestones only; `1` adds create / notify"
    },
    "tick_interval_seconds": {"type": "integer"}
  }
}
```

## `POST /tick`

Serialized with the scheduled tick (never overlaps). Optional body: none or `{}` fetches unread Roofix mail from Gmail (LISTENER_QUERY); `raw_emails` processes those instead, without touching Gmail. A `raw_emails` that is not a list is FastAPI's 422.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "properties": {
    "raw_emails": {
      "type": ["array", "null"],
      "items": {
        "type": "object",
        "additionalProperties": true,
        "description": "Contract A, the shape GmailClient.fetch produces (every key optional to the parser; others are ignored)",
        "properties": {
          "sender": {
            "type": ["string", "null"],
            "description": "e.g. `RFX | New Comment <no-reply@roofix.io>`: the event type is read from the `RFX | <type>` display name"
          },
          "subject": {"type": ["string", "null"]},
          "body_text": {"type": ["string", "null"]},
          "body_html": {"type": ["string", "null"]},
          "timestamp": {"type": ["string", "null"], "description": "ISO"},
          "to": {
            "type": "array",
            "items": {"type": "string"}
          },
          "message_id": {
            "type": ["string", "null"],
            "description": "Gmail message id (for mark-as-read)"
          },
          "attachments": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": true}
          }
        }
      }
    }
  }
}
```

Response `200` (`application/json`) — `{records, count}`, or `{error, records: [], count: 0}` when the pipeline raised:

```json
{
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["records", "count"],
      "properties": {
        "records": {
          "type": "array",
          "description": "One per event processed (an event whose scrape hit a transient failure is deferred to the next tick and left out)",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["event", "decision"],
            "properties": {
              "event": {
                "type": "object",
                "additionalProperties": false,
                "description": "The parsed event (`parser.ParsedEvent.as_dict()`) as the pipeline left it, `_raw_email` stripped",
                "required": [
                  "event_type",
                  "roofix_id",
                  "tracking_url",
                  "customer_name",
                  "address",
                  "address_suffix",
                  "comment_text",
                  "mentioned_users",
                  "parse_complete",
                  "email_timestamp",
                  "raw_subject",
                  "notes",
                  "message_id",
                  "project_id"
                ],
                "properties": {
                  "event_type": {
                    "type": "string",
                    "description": "From the `RFX | <type>` sender display name, else the subject's leading segment, aliases normalised (New Comment, New Task, Estimate Complete, Estimate, Deposit Invoice Sent, Install Date, HIC Executed, ...); `Unknown` when neither has one"
                  },
                  "roofix_id": {
                    "type": ["string", "null"],
                    "description": "Set only by a successful proposal scrape (the parser leaves it null)"
                  },
                  "tracking_url": {
                    "type": ["string", "null"],
                    "description": "The tokenized `urlNNNN.roofix.io/ls/click?…` link, else a `roofix.io/project/<id>` link"
                  },
                  "customer_name": {
                    "type": ["string", "null"],
                    "description": "From the \"<Name> - <Address>\" pattern (subject, then body); a scrape may replace it"
                  },
                  "address": {
                    "type": ["string", "null"],
                    "description": "The address half of that pattern, suffix stripped; a scrape may replace it"
                  },
                  "address_suffix": {"type": ["string", "null"], "description": "e.g. `Reorder`"},
                  "comment_text": {
                    "type": ["string", "null"],
                    "description": "The quoted comment, for New Comment events"
                  },
                  "mentioned_users": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "@Name tokens found in the comment"
                  },
                  "parse_complete": {
                    "type": "boolean",
                    "description": "False when the email is too thin to act on without scraping"
                  },
                  "email_timestamp": {
                    "type": ["string", "null"],
                    "description": "The raw email's `timestamp`"
                  },
                  "raw_subject": {
                    "type": "string",
                    "description": "The subject, HTML-unescaped (\"\" when there was none)"
                  },
                  "notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Parser, Phoenix-resolve and scrape observations, in order"
                  },
                  "message_id": {"type": ["string", "null"], "description": "The Gmail message id"},
                  "project_id": {
                    "type": ["integer", "null"],
                    "description": "The Phoenix project id when the lookup matched exactly one; null on a miss, an ambiguous match, or an ignore-eligible event (not looked up)"
                  },
                  "is_accepted": {
                    "type": "boolean",
                    "description": "Present only after a successful proposal scrape"
                  },
                  "_extracted_payload": {
                    "type": "object",
                    "additionalProperties": false,
                    "description": "Present only after a successful proposal scrape",
                    "required": [
                      "roofix_id",
                      "is_accepted",
                      "display_text",
                      "customer_name",
                      "full_name",
                      "first_name",
                      "last_name",
                      "email",
                      "phone",
                      "street_address",
                      "city",
                      "state_text",
                      "state_abbr",
                      "zip_code",
                      "contract_price",
                      "actual_contract_price",
                      "funding_type",
                      "trade",
                      "job_status",
                      "hic_status",
                      "hic_signature_present",
                      "acceptance_signals",
                      "error"
                    ],
                    "properties": {
                      "roofix_id": {"type": ["string", "null"]},
                      "is_accepted": {"type": "boolean"},
                      "display_text": {
                        "type": ["string", "null"],
                        "description": "\"Name - Address\" combined"
                      },
                      "customer_name": {
                        "type": "null",
                        "description": "Always null: ExtractedProposal has no such field (the name is `full_name`)"
                      },
                      "full_name": {"type": ["string", "null"]},
                      "first_name": {"type": ["string", "null"]},
                      "last_name": {"type": ["string", "null"]},
                      "email": {"type": ["string", "null"]},
                      "phone": {"type": ["string", "null"]},
                      "street_address": {"type": ["string", "null"]},
                      "city": {"type": ["string", "null"]},
                      "state_text": {
                        "type": "null",
                        "description": "Always null: ExtractedProposal's field is `state`"
                      },
                      "state_abbr": {"type": ["string", "null"]},
                      "zip_code": {"type": ["string", "null"]},
                      "contract_price": {
                        "type": "null",
                        "description": "Always null: ExtractedProposal has `estimated_price` and `actual_contract_price`"
                      },
                      "actual_contract_price": {"type": ["number", "null"]},
                      "funding_type": {"type": ["string", "null"]},
                      "trade": {"type": ["string", "null"]},
                      "job_status": {"type": ["string", "null"]},
                      "hic_status": {
                        "type": "null",
                        "description": "Always null: ExtractedProposal has no such field"
                      },
                      "hic_signature_present": {
                        "type": "null",
                        "description": "Always null: the signal lives in `acceptance_signals`"
                      },
                      "acceptance_signals": {
                        "type": "object",
                        "additionalProperties": false,
                        "required": [
                          "hic_present",
                          "hic_executed",
                          "hic_signature_present",
                          "job_present",
                          "job_status",
                          "homeowner_stage",
                          "warranty_present"
                        ],
                        "description": "The sub-signals behind `is_accepted`",
                        "properties": {
                          "hic_present": {"type": "boolean"},
                          "hic_executed": {
                            "type": "boolean",
                            "description": "hic exists AND status == \"executed\""
                          },
                          "hic_signature_present": {"type": "boolean"},
                          "job_present": {"type": "boolean"},
                          "job_status": {"type": ["string", "null"]},
                          "homeowner_stage": {"type": ["string", "null"]},
                          "warranty_present": {"type": "boolean"}
                        }
                      },
                      "error": {"type": ["string", "null"]}
                    }
                  }
                }
              },
              "decision": {
                "type": "object",
                "additionalProperties": false,
                "description": "The brain's Decision (`brain.Decision.as_dict()`, `message_id` stamped by the orchestrator). With `source: rule` the types are the rules'; the AI fallback's `action`, `target`, `payload` and `reasoning` are whatever JSON the model returned",
                "required": [
                  "action",
                  "event_type",
                  "target",
                  "payload",
                  "reasoning",
                  "needs_human",
                  "source",
                  "message_id"
                ],
                "properties": {
                  "action": {
                    "description": "update_chatter | update_milestone | create_project | noop_project_exists | escalate | ignore from the rules; the AI fallback may name another (then it is logged as not enabled and retried next tick)"
                  },
                  "event_type": {"type": "string"},
                  "target": {
                    "description": "What it acts on: the Phoenix project id as a string, or the tracking URL for create_project; null when there is none"
                  },
                  "payload": {
                    "description": "Values to write: `{\"note_text\"}` for update_chatter, `{\"tracking_url\"}` for create_project, `{}` otherwise"
                  },
                  "reasoning": {"description": "Why: read during the watch period"},
                  "needs_human": {
                    "type": "boolean",
                    "description": "True: surfaced for a human (escalation)"
                  },
                  "source": {"enum": ["rule", "ai"], "description": "Who decided"},
                  "message_id": {
                    "type": ["string", "null"],
                    "description": "The source email's Gmail message id"
                  },
                  "_forwarded": {
                    "type": "boolean",
                    "description": "Present when the escalation branch ran (needs_human): whether the email was forwarded to ESCALATION_RECIPIENTS (true: marked read; false: left unread)"
                  }
                },
                "if": {
                  "properties": {
                    "source": {"const": "rule"}
                  }
                },
                "then": {
                  "properties": {
                    "action": {
                      "enum": [
                        "update_chatter",
                        "update_milestone",
                        "create_project",
                        "noop_project_exists",
                        "escalate",
                        "ignore"
                      ]
                    },
                    "target": {"type": ["string", "null"]},
                    "payload": {
                      "type": "object",
                      "additionalProperties": false,
                      "properties": {
                        "note_text": {"type": "string"},
                        "tracking_url": {"type": "string"}
                      }
                    },
                    "reasoning": {"type": "string"}
                  }
                }
              }
            }
          }
        },
        "count": {"type": "integer", "minimum": 0, "description": "len(records)"}
      }
    },
    {
      "type": "object",
      "additionalProperties": false,
      "description": "The pipeline raised (still a 200)",
      "required": ["error", "records", "count"],
      "properties": {
        "error": {"type": "string", "description": "repr() of the exception"},
        "records": {"type": "array", "maxItems": 0},
        "count": {"const": 0}
      }
    }
  ]
}
```

## `POST /execute/:roofixMessageId`

Re-runs one Gmail message (fetched by id, read or unread; the processed_store dedup skipped). 404 `{"detail"}` when Gmail cannot find the id; a Gmail OAuth failure before the pipeline starts is an unhandled 500 (plain-text `Internal Server Error`).

Response `200` (`application/json`) — `{records, count}`, or `{error, records: [], count: 0}` when the pipeline raised:

```json
{
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["records", "count"],
      "properties": {
        "records": {
          "type": "array",
          "description": "One per event processed (an event whose scrape hit a transient failure is deferred to the next tick and left out)",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["event", "decision"],
            "properties": {
              "event": {
                "type": "object",
                "additionalProperties": false,
                "description": "The parsed event (`parser.ParsedEvent.as_dict()`) as the pipeline left it, `_raw_email` stripped",
                "required": [
                  "event_type",
                  "roofix_id",
                  "tracking_url",
                  "customer_name",
                  "address",
                  "address_suffix",
                  "comment_text",
                  "mentioned_users",
                  "parse_complete",
                  "email_timestamp",
                  "raw_subject",
                  "notes",
                  "message_id",
                  "project_id"
                ],
                "properties": {
                  "event_type": {
                    "type": "string",
                    "description": "From the `RFX | <type>` sender display name, else the subject's leading segment, aliases normalised (New Comment, New Task, Estimate Complete, Estimate, Deposit Invoice Sent, Install Date, HIC Executed, ...); `Unknown` when neither has one"
                  },
                  "roofix_id": {
                    "type": ["string", "null"],
                    "description": "Set only by a successful proposal scrape (the parser leaves it null)"
                  },
                  "tracking_url": {
                    "type": ["string", "null"],
                    "description": "The tokenized `urlNNNN.roofix.io/ls/click?…` link, else a `roofix.io/project/<id>` link"
                  },
                  "customer_name": {
                    "type": ["string", "null"],
                    "description": "From the \"<Name> - <Address>\" pattern (subject, then body); a scrape may replace it"
                  },
                  "address": {
                    "type": ["string", "null"],
                    "description": "The address half of that pattern, suffix stripped; a scrape may replace it"
                  },
                  "address_suffix": {"type": ["string", "null"], "description": "e.g. `Reorder`"},
                  "comment_text": {
                    "type": ["string", "null"],
                    "description": "The quoted comment, for New Comment events"
                  },
                  "mentioned_users": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "@Name tokens found in the comment"
                  },
                  "parse_complete": {
                    "type": "boolean",
                    "description": "False when the email is too thin to act on without scraping"
                  },
                  "email_timestamp": {
                    "type": ["string", "null"],
                    "description": "The raw email's `timestamp`"
                  },
                  "raw_subject": {
                    "type": "string",
                    "description": "The subject, HTML-unescaped (\"\" when there was none)"
                  },
                  "notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Parser, Phoenix-resolve and scrape observations, in order"
                  },
                  "message_id": {"type": ["string", "null"], "description": "The Gmail message id"},
                  "project_id": {
                    "type": ["integer", "null"],
                    "description": "The Phoenix project id when the lookup matched exactly one; null on a miss, an ambiguous match, or an ignore-eligible event (not looked up)"
                  },
                  "is_accepted": {
                    "type": "boolean",
                    "description": "Present only after a successful proposal scrape"
                  },
                  "_extracted_payload": {
                    "type": "object",
                    "additionalProperties": false,
                    "description": "Present only after a successful proposal scrape",
                    "required": [
                      "roofix_id",
                      "is_accepted",
                      "display_text",
                      "customer_name",
                      "full_name",
                      "first_name",
                      "last_name",
                      "email",
                      "phone",
                      "street_address",
                      "city",
                      "state_text",
                      "state_abbr",
                      "zip_code",
                      "contract_price",
                      "actual_contract_price",
                      "funding_type",
                      "trade",
                      "job_status",
                      "hic_status",
                      "hic_signature_present",
                      "acceptance_signals",
                      "error"
                    ],
                    "properties": {
                      "roofix_id": {"type": ["string", "null"]},
                      "is_accepted": {"type": "boolean"},
                      "display_text": {
                        "type": ["string", "null"],
                        "description": "\"Name - Address\" combined"
                      },
                      "customer_name": {
                        "type": "null",
                        "description": "Always null: ExtractedProposal has no such field (the name is `full_name`)"
                      },
                      "full_name": {"type": ["string", "null"]},
                      "first_name": {"type": ["string", "null"]},
                      "last_name": {"type": ["string", "null"]},
                      "email": {"type": ["string", "null"]},
                      "phone": {"type": ["string", "null"]},
                      "street_address": {"type": ["string", "null"]},
                      "city": {"type": ["string", "null"]},
                      "state_text": {
                        "type": "null",
                        "description": "Always null: ExtractedProposal's field is `state`"
                      },
                      "state_abbr": {"type": ["string", "null"]},
                      "zip_code": {"type": ["string", "null"]},
                      "contract_price": {
                        "type": "null",
                        "description": "Always null: ExtractedProposal has `estimated_price` and `actual_contract_price`"
                      },
                      "actual_contract_price": {"type": ["number", "null"]},
                      "funding_type": {"type": ["string", "null"]},
                      "trade": {"type": ["string", "null"]},
                      "job_status": {"type": ["string", "null"]},
                      "hic_status": {
                        "type": "null",
                        "description": "Always null: ExtractedProposal has no such field"
                      },
                      "hic_signature_present": {
                        "type": "null",
                        "description": "Always null: the signal lives in `acceptance_signals`"
                      },
                      "acceptance_signals": {
                        "type": "object",
                        "additionalProperties": false,
                        "required": [
                          "hic_present",
                          "hic_executed",
                          "hic_signature_present",
                          "job_present",
                          "job_status",
                          "homeowner_stage",
                          "warranty_present"
                        ],
                        "description": "The sub-signals behind `is_accepted`",
                        "properties": {
                          "hic_present": {"type": "boolean"},
                          "hic_executed": {
                            "type": "boolean",
                            "description": "hic exists AND status == \"executed\""
                          },
                          "hic_signature_present": {"type": "boolean"},
                          "job_present": {"type": "boolean"},
                          "job_status": {"type": ["string", "null"]},
                          "homeowner_stage": {"type": ["string", "null"]},
                          "warranty_present": {"type": "boolean"}
                        }
                      },
                      "error": {"type": ["string", "null"]}
                    }
                  }
                }
              },
              "decision": {
                "type": "object",
                "additionalProperties": false,
                "description": "The brain's Decision (`brain.Decision.as_dict()`, `message_id` stamped by the orchestrator). With `source: rule` the types are the rules'; the AI fallback's `action`, `target`, `payload` and `reasoning` are whatever JSON the model returned",
                "required": [
                  "action",
                  "event_type",
                  "target",
                  "payload",
                  "reasoning",
                  "needs_human",
                  "source",
                  "message_id"
                ],
                "properties": {
                  "action": {
                    "description": "update_chatter | update_milestone | create_project | noop_project_exists | escalate | ignore from the rules; the AI fallback may name another (then it is logged as not enabled and retried next tick)"
                  },
                  "event_type": {"type": "string"},
                  "target": {
                    "description": "What it acts on: the Phoenix project id as a string, or the tracking URL for create_project; null when there is none"
                  },
                  "payload": {
                    "description": "Values to write: `{\"note_text\"}` for update_chatter, `{\"tracking_url\"}` for create_project, `{}` otherwise"
                  },
                  "reasoning": {"description": "Why: read during the watch period"},
                  "needs_human": {
                    "type": "boolean",
                    "description": "True: surfaced for a human (escalation)"
                  },
                  "source": {"enum": ["rule", "ai"], "description": "Who decided"},
                  "message_id": {
                    "type": ["string", "null"],
                    "description": "The source email's Gmail message id"
                  },
                  "_forwarded": {
                    "type": "boolean",
                    "description": "Present when the escalation branch ran (needs_human): whether the email was forwarded to ESCALATION_RECIPIENTS (true: marked read; false: left unread)"
                  }
                },
                "if": {
                  "properties": {
                    "source": {"const": "rule"}
                  }
                },
                "then": {
                  "properties": {
                    "action": {
                      "enum": [
                        "update_chatter",
                        "update_milestone",
                        "create_project",
                        "noop_project_exists",
                        "escalate",
                        "ignore"
                      ]
                    },
                    "target": {"type": ["string", "null"]},
                    "payload": {
                      "type": "object",
                      "additionalProperties": false,
                      "properties": {
                        "note_text": {"type": "string"},
                        "tracking_url": {"type": "string"}
                      }
                    },
                    "reasoning": {"type": "string"}
                  }
                }
              }
            }
          }
        },
        "count": {"type": "integer", "minimum": 0, "description": "len(records)"}
      }
    },
    {
      "type": "object",
      "additionalProperties": false,
      "description": "The pipeline raised (still a 200)",
      "required": ["error", "records", "count"],
      "properties": {
        "error": {"type": "string", "description": "repr() of the exception"},
        "records": {"type": "array", "maxItems": 0},
        "count": {"const": 0}
      }
    }
  ]
}
```

## `POST /reset/:roofixMessageId`

404 `{"detail"}` when Gmail cannot find the id; any other Gmail or roofix-db failure is an unhandled 500 (plain-text `Internal Server Error`).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["message_id", "store", "gmail", "label"],
  "properties": {
    "message_id": {"type": "string", "description": "The path's id"},
    "store": {
      "enum": ["unchanged", "reset"],
      "description": "`reset`: its processed_store row is now `pending`; `unchanged`: it had no row"
    },
    "gmail": {
      "type": "object",
      "additionalProperties": false,
      "required": ["unread", "label_removed"],
      "properties": {
        "unread": {
          "const": true,
          "description": "The UNREAD label was re-added (a failure there is a 404 or 500 instead)"
        },
        "label_removed": {
          "type": "boolean",
          "description": "ROOFIX_PROCESSED_LABEL was removed (a failure is audit-logged, not raised)"
        }
      }
    },
    "label": {"type": "string", "description": "ROOFIX_PROCESSED_LABEL"}
  }
}
```

## `POST /labels/backfill`

500 `{"detail"}` when the label cannot be resolved; a Gmail OAuth or roofix-db failure is an unhandled 500 (plain-text `Internal Server Error`).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["label", "counts"],
  "properties": {
    "label": {"type": "string", "description": "ROOFIX_PROCESSED_LABEL"},
    "counts": {
      "type": "object",
      "additionalProperties": false,
      "required": ["labeled", "skipped", "failed"],
      "description": "Over every `ok` / `error` processed_store row",
      "properties": {
        "labeled": {"type": "integer", "minimum": 0, "description": "Gmail accepted the modify"},
        "skipped": {"type": "integer", "minimum": 0, "description": "The row had no message_id"},
        "failed": {
          "type": "integer",
          "minimum": 0,
          "description": "Gmail returned an error; each is audit-logged as `gmail / apply_label_failed`"
        }
      }
    }
  }
}
```
