# litellm request and response schemas

Generated from [`litellm.schemas.json`](litellm.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`LITELLM.md`](LITELLM.md).

## `GET /health`

Needs a key. Pings every deployment the key may use (or answers the background check's last result).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["healthy_endpoints", "unhealthy_endpoints", "healthy_count", "unhealthy_count"],
  "properties": {
    "healthy_endpoints": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "description": "One deployment's health check, its `litellm_params` minus credentials (`api_base` only for an admin)",
        "properties": {
          "model": {"type": "string", "description": "The deployment's `litellm_params.model`"},
          "model_id": {"type": ["string", "null"]},
          "error": {"type": "string", "description": "On an unhealthy endpoint: why"}
        }
      }
    },
    "unhealthy_endpoints": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "description": "One deployment's health check, its `litellm_params` minus credentials (`api_base` only for an admin)",
        "properties": {
          "model": {"type": "string", "description": "The deployment's `litellm_params.model`"},
          "model_id": {"type": ["string", "null"]},
          "error": {"type": "string", "description": "On an unhealthy endpoint: why"}
        }
      }
    },
    "healthy_count": {"type": "integer"},
    "unhealthy_count": {"type": "integer"}
  }
}
```

## `GET /health/liveliness`

No key. The container healthcheck's target.

Response `200` (`application/json`):

```json
{"const": "I'm alive!", "description": "a JSON string"}
```

Response `503` (`application/json`) — this copy is shutting down (SIGTERM: a swap deploy retiring it, or a stop):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["status"],
  "properties": {
    "status": {"const": "shutting_down"}
  }
}
```

## `GET /health/readiness`

No key.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["status", "db"],
  "properties": {
    "status": {"const": "healthy"},
    "db": {
      "enum": ["connected", "disconnected", "Not connected"],
      "description": "`disconnected` answers 503 with this same body; `Not connected`: no database configured"
    }
  }
}
```

Response `503` (`application/json`) — this copy is shutting down (SIGTERM: a swap deploy retiring it, or a stop):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["status"],
  "properties": {
    "status": {"const": "shutting_down"}
  }
}
```

## `POST /v1/chat/completions`

Same as `vllm: POST /v1/chat/completions`.

The OpenAI Chat Completions subset shared by vLLM, llama-server and LiteLLM. Server-specific fields say whose they are. Errors are the OpenAI error object (the collection's description).

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "OpenAI Chat Completions request: the fields this stack sends (vLLM ChatCompletionRequest has ~70; llama-server and LiteLLM accept the same names)",
  "required": ["model", "messages"],
  "properties": {
    "model": {
      "type": "string",
      "description": "vLLM: the `--served-model-name` (any other is a 404, \"The model … does not exist.\"); llama-server: its `-a` alias (not enforced); LiteLLM: a `model_name`, chain alias or `auto`, which it routes on"
    },
    "messages": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "additionalProperties": true,
        "$comment": "OpenAI chat message param",
        "required": ["role"],
        "properties": {
          "role": {
            "type": "string",
            "description": "`system`, `user`, `assistant` or `tool` in this stack"
          },
          "content": {
            "anyOf": [
              {"type": "string"},
              {
                "type": "array",
                "items": {
                  "type": "object",
                  "additionalProperties": true,
                  "$comment": "OpenAI chat content part; the stack sends `text` and `image_url` parts",
                  "required": ["type"],
                  "properties": {
                    "type": {"type": "string", "description": "`text` or `image_url` in this stack"},
                    "text": {"type": "string"},
                    "image_url": {
                      "type": "object",
                      "additionalProperties": true,
                      "required": ["url"],
                      "properties": {
                        "url": {
                          "type": "string",
                          "description": "An http(s) URL the server fetches, or a `data:image/...;base64,` URI"
                        },
                        "detail": {"type": "string"}
                      }
                    }
                  }
                }
              },
              {"type": "null"}
            ],
            "description": "Text, or a list of content parts (multimodal); null on an assistant message that only calls tools"
          },
          "name": {"type": "string"},
          "tool_calls": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": true,
              "$comment": "OpenAI tool call (vLLM ToolCall)",
              "required": ["id", "type", "function"],
              "properties": {
                "id": {"type": "string"},
                "type": {"const": "function"},
                "function": {
                  "type": "object",
                  "additionalProperties": true,
                  "required": ["name", "arguments"],
                  "properties": {
                    "name": {"type": "string"},
                    "arguments": {
                      "type": "string",
                      "description": "The arguments as a JSON-encoded string"
                    }
                  }
                }
              }
            },
            "description": "On an assistant message: the calls it made"
          },
          "tool_call_id": {
            "type": "string",
            "description": "On a `tool` message: the call it answers"
          }
        }
      }
    },
    "stream": {
      "type": "boolean",
      "default": false,
      "description": "true: a `text/event-stream` of `chat.completion.chunk` payloads"
    },
    "stream_options": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "include_usage": {"type": "boolean", "description": "A last chunk carries `usage`"}
      }
    },
    "max_tokens": {"type": "integer"},
    "max_completion_tokens": {"type": "integer"},
    "temperature": {"type": "number"},
    "top_p": {"type": "number"},
    "top_k": {"type": "integer", "description": "Not OpenAI; vLLM and llama-server"},
    "min_p": {"type": "number", "description": "Not OpenAI; vLLM and llama-server"},
    "presence_penalty": {"type": "number"},
    "frequency_penalty": {"type": "number"},
    "seed": {"type": "integer"},
    "n": {
      "type": "integer",
      "description": "Choices to sample (the classifier's sampling confidence)"
    },
    "stop": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "array",
          "items": {"type": "string"}
        }
      ]
    },
    "logprobs": {"type": "boolean"},
    "top_logprobs": {"type": "integer"},
    "response_format": {
      "type": "object",
      "additionalProperties": true,
      "required": ["type"],
      "properties": {
        "type": {
          "enum": ["text", "json_object", "json_schema"],
          "description": "`json_object`: the answer is constrained to JSON (llama-server turns it into a grammar)"
        },
        "json_schema": {"type": "object", "additionalProperties": true}
      }
    },
    "tools": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "$comment": "OpenAI tool definition (vLLM ChatCompletionToolsParam)",
        "required": ["type", "function"],
        "properties": {
          "type": {"const": "function"},
          "function": {
            "type": "object",
            "additionalProperties": true,
            "required": ["name"],
            "properties": {
              "name": {"type": "string"},
              "description": {"type": "string"},
              "parameters": {
                "type": "object",
                "additionalProperties": true,
                "description": "A JSON Schema for the arguments"
              }
            }
          }
        }
      }
    },
    "tool_choice": {
      "anyOf": [
        {"type": "string"},
        {"type": "object", "additionalProperties": true}
      ]
    },
    "parallel_tool_calls": {"type": "boolean"},
    "reasoning_effort": {"type": "string"},
    "chat_template_kwargs": {
      "type": "object",
      "additionalProperties": true,
      "description": "Not OpenAI (vLLM, llama-server): variables for the chat template. This stack sends `enable_thinking` (Qwen), `reasoning_effort` (the fixed Qwen template; glm5.3-flash takes `low` | `high`) and `reasoning_strength` (muse-glimmer). Through LiteLLM a request's value REPLACES the deployment's, it is not merged"
    },
    "user": {"type": "string"}
  }
}
```

Response `200` (`application/json`) — `stream` false or absent: the whole answer:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "vLLM ChatCompletionResponse (open subset); the same shape from llama-server and LiteLLM",
  "required": ["id", "object", "created", "model", "choices", "usage"],
  "properties": {
    "id": {"type": "string"},
    "object": {"const": "chat.completion"},
    "created": {"type": "integer", "description": "Unix seconds"},
    "model": {
      "type": "string",
      "description": "What the request named (LiteLLM: `auto` stays `auto`)"
    },
    "choices": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["index", "message", "finish_reason"],
        "properties": {
          "index": {"type": "integer"},
          "message": {
            "type": "object",
            "additionalProperties": true,
            "required": ["role"],
            "properties": {
              "role": {"const": "assistant"},
              "content": {
                "type": ["string", "null"],
                "description": "The answer (null when the turn only calls tools)"
              },
              "reasoning": {
                "type": ["string", "null"],
                "description": "vLLM: the reasoning parser's output (`--reasoning-parser`), kept out of `content`"
              },
              "reasoning_content": {
                "type": ["string", "null"],
                "description": "llama-server and LiteLLM (which maps vLLM's `reasoning` onto it): the same reasoning text"
              },
              "tool_calls": {
                "type": ["array", "null"],
                "items": {
                  "type": "object",
                  "additionalProperties": true,
                  "$comment": "OpenAI tool call (vLLM ToolCall)",
                  "required": ["id", "type", "function"],
                  "properties": {
                    "id": {"type": "string"},
                    "type": {"const": "function"},
                    "function": {
                      "type": "object",
                      "additionalProperties": true,
                      "required": ["name", "arguments"],
                      "properties": {
                        "name": {"type": "string"},
                        "arguments": {
                          "type": "string",
                          "description": "The arguments as a JSON-encoded string"
                        }
                      }
                    }
                  }
                },
                "description": "The parsed tool calls (`--tool-call-parser` on vLLM, `--jinja` on llama-server); absent when none"
              }
            }
          },
          "finish_reason": {
            "type": ["string", "null"],
            "description": "`stop`, `length`, `tool_calls`, …"
          },
          "logprobs": {
            "type": ["object", "null"],
            "additionalProperties": true,
            "$comment": "vLLM ChatCompletionLogProbs; present when the request set `logprobs: true`",
            "properties": {
              "content": {
                "type": ["array", "null"],
                "description": "One entry per generated token. The classifier reads an integer field's distribution from these (`common.logprobs`)",
                "items": {
                  "type": "object",
                  "additionalProperties": true,
                  "required": ["token", "logprob"],
                  "properties": {
                    "token": {"type": "string"},
                    "logprob": {"type": "number"},
                    "bytes": {
                      "type": ["array", "null"],
                      "items": {"type": "integer"}
                    },
                    "top_logprobs": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "additionalProperties": true,
                        "required": ["token", "logprob"],
                        "properties": {
                          "token": {"type": "string"},
                          "logprob": {"type": "number"},
                          "bytes": {
                            "type": ["array", "null"],
                            "items": {"type": "integer"}
                          }
                        }
                      },
                      "description": "The `top_logprobs` most likely tokens at this position"
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "usage": {
      "type": "object",
      "additionalProperties": true,
      "$comment": "vLLM UsageInfo; llama-server and LiteLLM send the same keys",
      "required": ["prompt_tokens", "total_tokens"],
      "properties": {
        "prompt_tokens": {"type": "integer"},
        "completion_tokens": {"type": ["integer", "null"]},
        "total_tokens": {"type": "integer"},
        "prompt_tokens_details": {
          "type": ["object", "null"],
          "additionalProperties": true,
          "properties": {
            "cached_tokens": {
              "type": ["integer", "null"],
              "description": "Prompt tokens served from the prefix cache. vLLM fills it only with `--enable-prompt-tokens-details` (muse-glimmer); the classifier records it per call kind"
            }
          }
        },
        "completion_tokens_details": {
          "type": ["object", "null"],
          "additionalProperties": true,
          "properties": {
            "reasoning_tokens": {
              "type": ["integer", "null"],
              "description": "Completion tokens spent reasoning, where the server counts them"
            }
          }
        }
      }
    },
    "system_fingerprint": {"type": ["string", "null"]},
    "timings": {
      "type": "object",
      "additionalProperties": true,
      "description": "llama-server only: prompt and generation speed (`cache_n`, `prompt_n`, `prompt_ms`, `predicted_n`, `predicted_per_second`, …)"
    }
  }
}
```

Response `200` (`text/event-stream`) — `"stream": true`: one `chat.completion.chunk` per `data:` line, then `data: [DONE]`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "vLLM ChatCompletionStreamResponse (open subset); the same shape from llama-server and LiteLLM",
  "required": ["id", "object", "created", "model", "choices"],
  "properties": {
    "id": {"type": "string"},
    "object": {"const": "chat.completion.chunk"},
    "created": {"type": "integer"},
    "model": {"type": "string"},
    "choices": {
      "type": "array",
      "description": "Empty on a final usage-only chunk",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["index", "delta"],
        "properties": {
          "index": {"type": "integer"},
          "delta": {
            "type": "object",
            "additionalProperties": true,
            "description": "What this chunk adds; concatenate `content` (and the reasoning field) across chunks",
            "properties": {
              "role": {"type": ["string", "null"]},
              "content": {"type": ["string", "null"]},
              "reasoning": {
                "type": ["string", "null"],
                "description": "vLLM: the reasoning parser's output (`--reasoning-parser`), kept out of `content`"
              },
              "reasoning_content": {
                "type": ["string", "null"],
                "description": "llama-server and LiteLLM (which maps vLLM's `reasoning` onto it): the same reasoning text"
              },
              "tool_calls": {
                "type": "array",
                "items": {
                  "type": "object",
                  "additionalProperties": true,
                  "required": ["index"],
                  "properties": {
                    "index": {"type": "integer"},
                    "id": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"]},
                    "function": {
                      "type": ["object", "null"],
                      "additionalProperties": true,
                      "properties": {
                        "name": {"type": ["string", "null"]},
                        "arguments": {
                          "type": ["string", "null"],
                          "description": "A fragment of the JSON arguments"
                        }
                      }
                    }
                  }
                }
              }
            }
          },
          "finish_reason": {
            "type": ["string", "null"],
            "description": "Set on the choice's last chunk"
          },
          "logprobs": {
            "type": ["object", "null"],
            "additionalProperties": true,
            "$comment": "vLLM ChatCompletionLogProbs; present when the request set `logprobs: true`",
            "properties": {
              "content": {
                "type": ["array", "null"],
                "description": "One entry per generated token. The classifier reads an integer field's distribution from these (`common.logprobs`)",
                "items": {
                  "type": "object",
                  "additionalProperties": true,
                  "required": ["token", "logprob"],
                  "properties": {
                    "token": {"type": "string"},
                    "logprob": {"type": "number"},
                    "bytes": {
                      "type": ["array", "null"],
                      "items": {"type": "integer"}
                    },
                    "top_logprobs": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "additionalProperties": true,
                        "required": ["token", "logprob"],
                        "properties": {
                          "token": {"type": "string"},
                          "logprob": {"type": "number"},
                          "bytes": {
                            "type": ["array", "null"],
                            "items": {"type": "integer"}
                          }
                        }
                      },
                      "description": "The `top_logprobs` most likely tokens at this position"
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "usage": {
      "anyOf": [
        {
          "type": "object",
          "additionalProperties": true,
          "$comment": "vLLM UsageInfo; llama-server and LiteLLM send the same keys",
          "required": ["prompt_tokens", "total_tokens"],
          "properties": {
            "prompt_tokens": {"type": "integer"},
            "completion_tokens": {"type": ["integer", "null"]},
            "total_tokens": {"type": "integer"},
            "prompt_tokens_details": {
              "type": ["object", "null"],
              "additionalProperties": true,
              "properties": {
                "cached_tokens": {
                  "type": ["integer", "null"],
                  "description": "Prompt tokens served from the prefix cache. vLLM fills it only with `--enable-prompt-tokens-details` (muse-glimmer); the classifier records it per call kind"
                }
              }
            },
            "completion_tokens_details": {
              "type": ["object", "null"],
              "additionalProperties": true,
              "properties": {
                "reasoning_tokens": {
                  "type": ["integer", "null"],
                  "description": "Completion tokens spent reasoning, where the server counts them"
                }
              }
            }
          }
        },
        {"type": "null"}
      ],
      "description": "On the last chunk with `stream_options.include_usage` (LiteLLM and llama-server send it unasked)"
    }
  }
}
```

## `POST /v1/embeddings`

No embedding model is configured in this deployment, so this answers an error today: every `model_list` backend is a chat (or TTS) server. The collection's `qwen3.6` reaches its vLLM server, which registers no `/v1/embeddings` for a chat model (FastAPI's 404 `{"detail": "Not Found"}`); LiteLLM (no retry for a 404) tries its `fallbacks` entry `claude-sonnet-5` (which it cannot embed with: `Unmapped LLM provider for this endpoint`) and answers the first error, a 404. With qwen3.6 down it is a 500 (connection error) instead. The 200 is what a model with an embedding backend would answer.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["model", "input"],
  "properties": {
    "model": {"type": "string"},
    "input": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "array",
          "items": {"type": "string"}
        },
        {
          "type": "array",
          "items": {"type": "integer"}
        },
        {
          "type": "array",
          "items": {
            "type": "array",
            "items": {"type": "integer"}
          }
        }
      ]
    },
    "encoding_format": {"enum": ["float", "base64"]},
    "dimensions": {"type": "integer"},
    "user": {"type": "string"}
  }
}
```

Response `200` (`application/json`) — only from a model whose backend embeds (none configured):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["object", "data", "model"],
  "properties": {
    "object": {"const": "list"},
    "data": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["object", "index", "embedding"],
        "properties": {
          "object": {"const": "embedding"},
          "index": {"type": "integer"},
          "embedding": {
            "anyOf": [
              {
                "type": "array",
                "items": {"type": "number"}
              },
              {"type": "string"}
            ],
            "description": "Floats, or a base64 string with `encoding_format: base64`"
          }
        }
      }
    },
    "model": {"type": "string"},
    "usage": {
      "type": "object",
      "additionalProperties": true,
      "properties": {
        "prompt_tokens": {"type": "integer"},
        "total_tokens": {"type": "integer"}
      }
    }
  }
}
```

Response `404` (`application/json`) — today, for the collection's `qwen3.6`: its vLLM server has no embeddings route:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "LiteLLM's ProxyException body",
  "required": ["error"],
  "properties": {
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["message", "type", "param", "code"],
      "properties": {
        "message": {
          "type": "string",
          "description": "`litellm.NotFoundError: NotFoundError: OpenAIException - Error code: 404 - {'detail': 'Not Found'}. Received Model Group=qwen3.6`, then the fallbacks tried and each one's error"
        },
        "type": {"type": ["string", "null"], "description": "null here"},
        "param": {"type": ["string", "null"], "description": "null here"},
        "code": {"const": "404", "description": "The HTTP status, as a string"}
      }
    }
  }
}
```

## `GET /v1/models`

Same as `vllm: GET /v1/models`.

The OpenAI model list, shared by vLLM, llama-server and LiteLLM. A vLLM server lists exactly one model; LiteLLM lists what the calling key may use.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "vLLM ModelList (open subset); llama-server and LiteLLM send the same envelope",
  "required": ["object", "data"],
  "properties": {
    "object": {"const": "list"},
    "data": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["id", "object"],
        "properties": {
          "id": {
            "type": "string",
            "description": "What a request's `model` must be: vLLM's `--served-model-name` (else the HF repo id, as on vllm-qwen-vl), llama-server's `-a` alias, a LiteLLM `model_name`"
          },
          "object": {"const": "model"},
          "created": {"type": "integer"},
          "owned_by": {"type": "string", "description": "`vllm`, `llamacpp`, `openai` (LiteLLM)"},
          "root": {"type": ["string", "null"], "description": "vLLM: the model path or repo"},
          "max_model_len": {"type": ["integer", "null"], "description": "vLLM: `--max-model-len`"},
          "meta": {
            "type": ["object", "null"],
            "additionalProperties": true,
            "description": "llama-server: `n_ctx_train`, `n_params`, `size`, … (null while the model loads)"
          }
        }
      }
    }
  }
}
```

## `POST /v1/audio/speech`

Routed by `model` (`kokoro` -> kokoro-api's `POST /v1/audio/speech`, kokoro.schemas.json).

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["model", "input"],
  "properties": {
    "model": {"type": "string", "description": "`kokoro`"},
    "input": {"type": "string", "description": "The text to speak"},
    "voice": {"type": "string", "description": "An OpenAI alias or a Kokoro voice name"},
    "response_format": {"type": "string", "description": "Ignored by Kokoro, which only makes WAV"},
    "speed": {"type": "number"}
  }
}
```

Response `200` (`audio/mpeg`) — the backend's audio bytes, labelled `audio/mpeg` by LiteLLM whatever they are (only a Gemini TTS model gets `audio/wav`): from kokoro they are WAV (`RIFF…WAVE`).

## `POST /key/generate`

Master key (or an admin). The response is the only time the secret `key` is shown.

Request body (`application/json`):

```json
{
  "allOf": [
    {
      "type": "object",
      "additionalProperties": true,
      "$comment": "the GenerateKeyRequest / UpdateKeyRequest fields this stack uses (~50 in all)",
      "properties": {
        "key_alias": {"type": ["string", "null"]},
        "models": {
          "type": "array",
          "items": {"type": "string"},
          "description": "The `model_name`s / chain aliases the key may call; empty: all"
        },
        "max_budget": {"type": ["number", "null"]},
        "spend": {"type": ["number", "null"]},
        "user_id": {"type": ["string", "null"]},
        "team_id": {"type": ["string", "null"]},
        "metadata": {
          "type": "object",
          "additionalProperties": true,
          "description": "Free-form; stored as sent",
          "properties": {
            "allowed_passthrough_routes": {
              "type": "array",
              "items": {"type": "string"},
              "description": "The pass-through prefixes this key may call (`/v1/classifier`, …). Set it INSIDE `metadata`, never as the top-level field of the same name (Enterprise-only, 403 unlicensed); only a proxy admin may set it. LITELLM.md § Pass-through auth"
            }
          }
        },
        "tpm_limit": {"type": ["integer", "null"]},
        "rpm_limit": {"type": ["integer", "null"]},
        "max_parallel_requests": {"type": ["integer", "null"]},
        "budget_duration": {"type": ["string", "null"]}
      }
    }
  ],
  "type": "object",
  "additionalProperties": true,
  "properties": {
    "duration": {"type": ["string", "null"], "description": "`30d`, `1h`, …; null: never expires"},
    "key": {"type": ["string", "null"], "description": "Set the secret yourself (else generated)"}
  }
}
```

Response `200` (`application/json`):

```json
{
  "allOf": [
    {
      "type": "object",
      "additionalProperties": true,
      "$comment": "the GenerateKeyRequest / UpdateKeyRequest fields this stack uses (~50 in all)",
      "properties": {
        "key_alias": {"type": ["string", "null"]},
        "models": {
          "type": "array",
          "items": {"type": "string"},
          "description": "The `model_name`s / chain aliases the key may call; empty: all"
        },
        "max_budget": {"type": ["number", "null"]},
        "spend": {"type": ["number", "null"]},
        "user_id": {"type": ["string", "null"]},
        "team_id": {"type": ["string", "null"]},
        "metadata": {
          "type": "object",
          "additionalProperties": true,
          "description": "Free-form; stored as sent",
          "properties": {
            "allowed_passthrough_routes": {
              "type": "array",
              "items": {"type": "string"},
              "description": "The pass-through prefixes this key may call (`/v1/classifier`, …). Set it INSIDE `metadata`, never as the top-level field of the same name (Enterprise-only, 403 unlicensed); only a proxy admin may set it. LITELLM.md § Pass-through auth"
            }
          }
        },
        "tpm_limit": {"type": ["integer", "null"]},
        "rpm_limit": {"type": ["integer", "null"]},
        "max_parallel_requests": {"type": ["integer", "null"]},
        "budget_duration": {"type": ["string", "null"]}
      }
    }
  ],
  "type": "object",
  "additionalProperties": true,
  "required": ["key"],
  "properties": {
    "key": {"type": "string", "description": "The new virtual key (`sk-…`): store it now"},
    "key_name": {"type": ["string", "null"], "description": "A masked form of the key"},
    "expires": {"type": ["string", "null"], "description": "ISO 8601"},
    "token": {
      "type": ["string", "null"],
      "description": "The key's hash, its id in /key/* calls' results"
    },
    "created_at": {"type": ["string", "null"]}
  }
}
```

## `GET /key/info`

`?key=` names the key (without it: the calling key). 404 for an unknown key.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["key", "info"],
  "properties": {
    "key": {"type": "string", "description": "As asked"},
    "info": {
      "allOf": [
        {
          "type": "object",
          "additionalProperties": true,
          "$comment": "the GenerateKeyRequest / UpdateKeyRequest fields this stack uses (~50 in all)",
          "properties": {
            "key_alias": {"type": ["string", "null"]},
            "models": {
              "type": "array",
              "items": {"type": "string"},
              "description": "The `model_name`s / chain aliases the key may call; empty: all"
            },
            "max_budget": {"type": ["number", "null"]},
            "spend": {"type": ["number", "null"]},
            "user_id": {"type": ["string", "null"]},
            "team_id": {"type": ["string", "null"]},
            "metadata": {
              "type": "object",
              "additionalProperties": true,
              "description": "Free-form; stored as sent",
              "properties": {
                "allowed_passthrough_routes": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "The pass-through prefixes this key may call (`/v1/classifier`, …). Set it INSIDE `metadata`, never as the top-level field of the same name (Enterprise-only, 403 unlicensed); only a proxy admin may set it. LITELLM.md § Pass-through auth"
                }
              }
            },
            "tpm_limit": {"type": ["integer", "null"]},
            "rpm_limit": {"type": ["integer", "null"]},
            "max_parallel_requests": {"type": ["integer", "null"]},
            "budget_duration": {"type": ["string", "null"]}
          }
        }
      ],
      "type": "object",
      "additionalProperties": true,
      "description": "The key's row without its hash (`token`)",
      "properties": {
        "key_name": {"type": ["string", "null"]},
        "expires": {"type": ["string", "null"]},
        "created_at": {"type": ["string", "null"]},
        "updated_at": {"type": ["string", "null"]}
      }
    }
  }
}
```

## `POST /key/update`

Every field given replaces the stored one (`metadata` as a whole: read it with /key/info first). 404 for an unknown key.

Request body (`application/json`):

```json
{
  "allOf": [
    {
      "type": "object",
      "additionalProperties": true,
      "$comment": "the GenerateKeyRequest / UpdateKeyRequest fields this stack uses (~50 in all)",
      "properties": {
        "key_alias": {"type": ["string", "null"]},
        "models": {
          "type": "array",
          "items": {"type": "string"},
          "description": "The `model_name`s / chain aliases the key may call; empty: all"
        },
        "max_budget": {"type": ["number", "null"]},
        "spend": {"type": ["number", "null"]},
        "user_id": {"type": ["string", "null"]},
        "team_id": {"type": ["string", "null"]},
        "metadata": {
          "type": "object",
          "additionalProperties": true,
          "description": "Free-form; stored as sent",
          "properties": {
            "allowed_passthrough_routes": {
              "type": "array",
              "items": {"type": "string"},
              "description": "The pass-through prefixes this key may call (`/v1/classifier`, …). Set it INSIDE `metadata`, never as the top-level field of the same name (Enterprise-only, 403 unlicensed); only a proxy admin may set it. LITELLM.md § Pass-through auth"
            }
          }
        },
        "tpm_limit": {"type": ["integer", "null"]},
        "rpm_limit": {"type": ["integer", "null"]},
        "max_parallel_requests": {"type": ["integer", "null"]},
        "budget_duration": {"type": ["string", "null"]}
      }
    }
  ],
  "type": "object",
  "additionalProperties": true,
  "required": ["key"],
  "properties": {
    "key": {"type": "string", "description": "The key to change"}
  }
}
```

Response `200` (`application/json`):

```json
{
  "allOf": [
    {
      "type": "object",
      "additionalProperties": true,
      "$comment": "the GenerateKeyRequest / UpdateKeyRequest fields this stack uses (~50 in all)",
      "properties": {
        "key_alias": {"type": ["string", "null"]},
        "models": {
          "type": "array",
          "items": {"type": "string"},
          "description": "The `model_name`s / chain aliases the key may call; empty: all"
        },
        "max_budget": {"type": ["number", "null"]},
        "spend": {"type": ["number", "null"]},
        "user_id": {"type": ["string", "null"]},
        "team_id": {"type": ["string", "null"]},
        "metadata": {
          "type": "object",
          "additionalProperties": true,
          "description": "Free-form; stored as sent",
          "properties": {
            "allowed_passthrough_routes": {
              "type": "array",
              "items": {"type": "string"},
              "description": "The pass-through prefixes this key may call (`/v1/classifier`, …). Set it INSIDE `metadata`, never as the top-level field of the same name (Enterprise-only, 403 unlicensed); only a proxy admin may set it. LITELLM.md § Pass-through auth"
            }
          }
        },
        "tpm_limit": {"type": ["integer", "null"]},
        "rpm_limit": {"type": ["integer", "null"]},
        "max_parallel_requests": {"type": ["integer", "null"]},
        "budget_duration": {"type": ["string", "null"]}
      }
    }
  ],
  "type": "object",
  "additionalProperties": true,
  "required": ["key"],
  "description": "`key` plus the updated fields",
  "properties": {
    "key": {"type": "string"}
  }
}
```

## `POST /key/delete`

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "properties": {
    "keys": {
      "type": ["array", "null"],
      "items": {"type": "string"},
      "description": "Keys (`sk-…`) or their hashes"
    },
    "key_aliases": {
      "type": ["array", "null"],
      "items": {"type": "string"}
    }
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["deleted_keys"],
  "properties": {
    "deleted_keys": {
      "type": "array",
      "items": {"type": "string"}
    }
  }
}
```

## `GET /user/info`

`?user_id=` names the user (without it: the calling key's).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["user_id", "user_info", "keys", "teams"],
  "properties": {
    "user_id": {"type": ["string", "null"]},
    "user_info": {
      "type": ["object", "null"],
      "additionalProperties": true,
      "description": "The user's row: spend, max_budget, models, …"
    },
    "keys": {
      "type": "array",
      "items": {"type": "object", "additionalProperties": true},
      "description": "The user's keys (hashes, not secrets)"
    },
    "teams": {
      "type": "array",
      "items": {"type": "object", "additionalProperties": true}
    }
  }
}
```

## `GET /model/info`

Every deployment: the config file's `model_list` and the database's (`store_model_in_db`).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["data"],
  "properties": {
    "data": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["model_name", "litellm_params", "model_info"],
        "properties": {
          "model_name": {
            "type": "string",
            "description": "What a request's `model` names (a chain alias groups several deployments)"
          },
          "litellm_params": {
            "type": "object",
            "additionalProperties": true,
            "description": "`model`, `api_base`, `order`, … (credentials removed)"
          },
          "model_info": {
            "type": "object",
            "additionalProperties": true,
            "description": "`id` (the `x-litellm-model-id` header), `mode`, `max_input_tokens`, …"
          }
        }
      }
    }
  }
}
```

## `GET /v1/classifier/health`

Same as `classifier: GET /v1/classifier/health`.

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

## `GET /v1/madlad/health`

Same as `madlad: GET /v1/madlad/health`.

Response `200` (`application/json`) — madlad-api is up; madlad-app is not checked:

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

## `GET /v1/interceptor/health`

Same as `interceptor: GET /v1/interceptor/health`.

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

## `GET /v1/roofix/health`

Same as `roofix: GET /health`.

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

## `GET /v1/sandbox/health`

Same as `sandbox-runner: GET /v1/sandbox/health`.

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

## `GET /mcp/kokoro_tts`

The URL to register (`claude mcp add --transport http … {{litellm}}/mcp/<server>`), not an API: MCP clients POST JSON-RPC here. A plain GET is the MCP stream a client opens for server-sent messages, so it needs `Accept: text/event-stream`; Postman's default `*/*` gets the 406. Every `/mcp/<server>` item answers alike, after LiteLLM's own checks: a bad key or one not allowed the server is its 401 / 403, and `gmail` (OAuth delegated to Google) answers a request with no bearer 401 with a `WWW-Authenticate` challenge.

Response `200` (`text/event-stream`) — with `Accept: text/event-stream`: an open stream for server-to-client JSON-RPC messages, which on LiteLLM's stateless path (no `mcp-session-id`) carries nothing and never ends.

Response `406` (`application/json`) — without `text/event-stream` in `Accept`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.JSONRPCError",
  "required": ["jsonrpc", "id", "error"],
  "properties": {
    "jsonrpc": {"const": "2.0"},
    "id": {"const": "server-error"},
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["code", "message"],
      "properties": {
        "code": {"const": -32600},
        "message": {"const": "Not Acceptable: Client must accept text/event-stream"}
      }
    }
  }
}
```

## `GET /mcp/madlad_translate`

Same as `litellm: GET /mcp/kokoro_tts`.

The URL to register (`claude mcp add --transport http … {{litellm}}/mcp/<server>`), not an API: MCP clients POST JSON-RPC here. A plain GET is the MCP stream a client opens for server-sent messages, so it needs `Accept: text/event-stream`; Postman's default `*/*` gets the 406. Every `/mcp/<server>` item answers alike, after LiteLLM's own checks: a bad key or one not allowed the server is its 401 / 403, and `gmail` (OAuth delegated to Google) answers a request with no bearer 401 with a `WWW-Authenticate` challenge.

Response `200` (`text/event-stream`) — with `Accept: text/event-stream`: an open stream for server-to-client JSON-RPC messages, which on LiteLLM's stateless path (no `mcp-session-id`) carries nothing and never ends.

Response `406` (`application/json`) — without `text/event-stream` in `Accept`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.JSONRPCError",
  "required": ["jsonrpc", "id", "error"],
  "properties": {
    "jsonrpc": {"const": "2.0"},
    "id": {"const": "server-error"},
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["code", "message"],
      "properties": {
        "code": {"const": -32600},
        "message": {"const": "Not Acceptable: Client must accept text/event-stream"}
      }
    }
  }
}
```

## `GET /mcp/detector`

Same as `litellm: GET /mcp/kokoro_tts`.

The URL to register (`claude mcp add --transport http … {{litellm}}/mcp/<server>`), not an API: MCP clients POST JSON-RPC here. A plain GET is the MCP stream a client opens for server-sent messages, so it needs `Accept: text/event-stream`; Postman's default `*/*` gets the 406. Every `/mcp/<server>` item answers alike, after LiteLLM's own checks: a bad key or one not allowed the server is its 401 / 403, and `gmail` (OAuth delegated to Google) answers a request with no bearer 401 with a `WWW-Authenticate` challenge.

Response `200` (`text/event-stream`) — with `Accept: text/event-stream`: an open stream for server-to-client JSON-RPC messages, which on LiteLLM's stateless path (no `mcp-session-id`) carries nothing and never ends.

Response `406` (`application/json`) — without `text/event-stream` in `Accept`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.JSONRPCError",
  "required": ["jsonrpc", "id", "error"],
  "properties": {
    "jsonrpc": {"const": "2.0"},
    "id": {"const": "server-error"},
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["code", "message"],
      "properties": {
        "code": {"const": -32600},
        "message": {"const": "Not Acceptable: Client must accept text/event-stream"}
      }
    }
  }
}
```

## `GET /mcp/interceptor`

Same as `litellm: GET /mcp/kokoro_tts`.

The URL to register (`claude mcp add --transport http … {{litellm}}/mcp/<server>`), not an API: MCP clients POST JSON-RPC here. A plain GET is the MCP stream a client opens for server-sent messages, so it needs `Accept: text/event-stream`; Postman's default `*/*` gets the 406. Every `/mcp/<server>` item answers alike, after LiteLLM's own checks: a bad key or one not allowed the server is its 401 / 403, and `gmail` (OAuth delegated to Google) answers a request with no bearer 401 with a `WWW-Authenticate` challenge.

Response `200` (`text/event-stream`) — with `Accept: text/event-stream`: an open stream for server-to-client JSON-RPC messages, which on LiteLLM's stateless path (no `mcp-session-id`) carries nothing and never ends.

Response `406` (`application/json`) — without `text/event-stream` in `Accept`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.JSONRPCError",
  "required": ["jsonrpc", "id", "error"],
  "properties": {
    "jsonrpc": {"const": "2.0"},
    "id": {"const": "server-error"},
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["code", "message"],
      "properties": {
        "code": {"const": -32600},
        "message": {"const": "Not Acceptable: Client must accept text/event-stream"}
      }
    }
  }
}
```

## `GET /mcp/sandbox`

Same as `litellm: GET /mcp/kokoro_tts`.

The URL to register (`claude mcp add --transport http … {{litellm}}/mcp/<server>`), not an API: MCP clients POST JSON-RPC here. A plain GET is the MCP stream a client opens for server-sent messages, so it needs `Accept: text/event-stream`; Postman's default `*/*` gets the 406. Every `/mcp/<server>` item answers alike, after LiteLLM's own checks: a bad key or one not allowed the server is its 401 / 403, and `gmail` (OAuth delegated to Google) answers a request with no bearer 401 with a `WWW-Authenticate` challenge.

Response `200` (`text/event-stream`) — with `Accept: text/event-stream`: an open stream for server-to-client JSON-RPC messages, which on LiteLLM's stateless path (no `mcp-session-id`) carries nothing and never ends.

Response `406` (`application/json`) — without `text/event-stream` in `Accept`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.JSONRPCError",
  "required": ["jsonrpc", "id", "error"],
  "properties": {
    "jsonrpc": {"const": "2.0"},
    "id": {"const": "server-error"},
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["code", "message"],
      "properties": {
        "code": {"const": -32600},
        "message": {"const": "Not Acceptable: Client must accept text/event-stream"}
      }
    }
  }
}
```

## `GET /mcp/trino`

Same as `litellm: GET /mcp/kokoro_tts`.

The URL to register (`claude mcp add --transport http … {{litellm}}/mcp/<server>`), not an API: MCP clients POST JSON-RPC here. A plain GET is the MCP stream a client opens for server-sent messages, so it needs `Accept: text/event-stream`; Postman's default `*/*` gets the 406. Every `/mcp/<server>` item answers alike, after LiteLLM's own checks: a bad key or one not allowed the server is its 401 / 403, and `gmail` (OAuth delegated to Google) answers a request with no bearer 401 with a `WWW-Authenticate` challenge.

Response `200` (`text/event-stream`) — with `Accept: text/event-stream`: an open stream for server-to-client JSON-RPC messages, which on LiteLLM's stateless path (no `mcp-session-id`) carries nothing and never ends.

Response `406` (`application/json`) — without `text/event-stream` in `Accept`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.JSONRPCError",
  "required": ["jsonrpc", "id", "error"],
  "properties": {
    "jsonrpc": {"const": "2.0"},
    "id": {"const": "server-error"},
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["code", "message"],
      "properties": {
        "code": {"const": -32600},
        "message": {"const": "Not Acceptable: Client must accept text/event-stream"}
      }
    }
  }
}
```

## `GET /mcp/phoenix`

Same as `litellm: GET /mcp/kokoro_tts`.

The URL to register (`claude mcp add --transport http … {{litellm}}/mcp/<server>`), not an API: MCP clients POST JSON-RPC here. A plain GET is the MCP stream a client opens for server-sent messages, so it needs `Accept: text/event-stream`; Postman's default `*/*` gets the 406. Every `/mcp/<server>` item answers alike, after LiteLLM's own checks: a bad key or one not allowed the server is its 401 / 403, and `gmail` (OAuth delegated to Google) answers a request with no bearer 401 with a `WWW-Authenticate` challenge.

Response `200` (`text/event-stream`) — with `Accept: text/event-stream`: an open stream for server-to-client JSON-RPC messages, which on LiteLLM's stateless path (no `mcp-session-id`) carries nothing and never ends.

Response `406` (`application/json`) — without `text/event-stream` in `Accept`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.JSONRPCError",
  "required": ["jsonrpc", "id", "error"],
  "properties": {
    "jsonrpc": {"const": "2.0"},
    "id": {"const": "server-error"},
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["code", "message"],
      "properties": {
        "code": {"const": -32600},
        "message": {"const": "Not Acceptable: Client must accept text/event-stream"}
      }
    }
  }
}
```

## `GET /mcp/gmail`

Same as `litellm: GET /mcp/kokoro_tts`.

The URL to register (`claude mcp add --transport http … {{litellm}}/mcp/<server>`), not an API: MCP clients POST JSON-RPC here. A plain GET is the MCP stream a client opens for server-sent messages, so it needs `Accept: text/event-stream`; Postman's default `*/*` gets the 406. Every `/mcp/<server>` item answers alike, after LiteLLM's own checks: a bad key or one not allowed the server is its 401 / 403, and `gmail` (OAuth delegated to Google) answers a request with no bearer 401 with a `WWW-Authenticate` challenge.

Response `200` (`text/event-stream`) — with `Accept: text/event-stream`: an open stream for server-to-client JSON-RPC messages, which on LiteLLM's stateless path (no `mcp-session-id`) carries nothing and never ends.

Response `406` (`application/json`) — without `text/event-stream` in `Accept`:

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "mcp.types.JSONRPCError",
  "required": ["jsonrpc", "id", "error"],
  "properties": {
    "jsonrpc": {"const": "2.0"},
    "id": {"const": "server-error"},
    "error": {
      "type": "object",
      "additionalProperties": true,
      "required": ["code", "message"],
      "properties": {
        "code": {"const": -32600},
        "message": {"const": "Not Acceptable: Client must accept text/event-stream"}
      }
    }
  }
}
```
