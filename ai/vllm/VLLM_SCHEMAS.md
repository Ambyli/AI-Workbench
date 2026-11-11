# vllm request and response schemas

Generated from [`vllm.schemas.json`](vllm.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`VLLM.md`](VLLM.md).

## `GET /health`

The HTTP server starts only after the model has loaded, so a starting container refuses connections rather than answering 503.

Response `200`: no body — the engine is up.

Response `503`: no body — the engine has died (EngineDeadError).

## `GET /v1/models`

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

## `POST /v1/chat/completions`

The OpenAI Chat Completions subset shared by vLLM, llama-server and LiteLLM: one 200 schema with two branches, the JSON answer and, with `"stream": true`, the payload of every SSE `data:` line (`text/event-stream`). Server-specific fields say whose they are. Errors are the OpenAI error object (the collection's description).

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

Response `200` (`application/json`) — with `"stream": true` the body is `text/event-stream` instead, one `chat.completion.chunk` per `data:` line, then `data: [DONE]`:

```json
{
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": true,
      "$comment": "vLLM ChatCompletionResponse (open subset); the same shape from llama-server and LiteLLM",
      "description": "`stream` false or absent: the whole answer, `application/json`",
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
    },
    {
      "type": "object",
      "additionalProperties": true,
      "$comment": "vLLM ChatCompletionStreamResponse (open subset); the same shape from llama-server and LiteLLM",
      "description": "`stream: true`: the body is `text/event-stream`, and each `data:` line carries one of these; the stream ends with `data: [DONE]`",
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
  ]
}
```

## `POST /v1/completions`

The OpenAI (legacy) Completions subset shared by vLLM and llama-server: no chat template, the prompt is fed verbatim. With `"stream": true` each SSE `data:` line is a `text_completion` object holding the next `text` fragment, then `data: [DONE]`.

Request body (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "OpenAI (legacy) Completions request: the fields this stack sends (vLLM CompletionRequest)",
  "required": ["model", "prompt"],
  "properties": {
    "model": {"type": "string", "description": "As for chat completions"},
    "prompt": {
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
      ],
      "description": "Fed to the model verbatim: no chat template, so no reasoning or tool parsing"
    },
    "max_tokens": {"type": "integer", "description": "vLLM's default is 16"},
    "temperature": {"type": "number"},
    "top_p": {"type": "number"},
    "stop": {
      "anyOf": [
        {"type": "string"},
        {
          "type": "array",
          "items": {"type": "string"}
        }
      ]
    },
    "stream": {"type": "boolean", "default": false},
    "logprobs": {"type": "integer"},
    "echo": {"type": "boolean"},
    "seed": {"type": "integer"}
  }
}
```

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "$comment": "vLLM CompletionResponse (open subset); llama-server sends the same shape",
  "required": ["id", "object", "created", "model", "choices"],
  "properties": {
    "id": {"type": "string"},
    "object": {"const": "text_completion"},
    "created": {"type": "integer"},
    "model": {"type": "string"},
    "choices": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": true,
        "required": ["index", "text", "finish_reason"],
        "properties": {
          "index": {"type": "integer"},
          "text": {"type": "string"},
          "finish_reason": {"type": ["string", "null"]},
          "logprobs": {"type": ["object", "null"], "additionalProperties": true}
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
    "timings": {"type": "object", "additionalProperties": true, "description": "llama-server only"}
  }
}
```
