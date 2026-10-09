# LiteLLM + MCP (Tool Calling)

An MCP server exposes tools via LiteLLM. The model receives tool definitions but LiteLLM does **not** execute the tool calls automatically — you must orchestrate the tool call loop.

## Step 1: Send the user message

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6",
    "messages": [{"role": "user", "content": "List all tables in the database"}]
  }'
```

The response will have `"finish_reason": "tool_calls"` with a tool call object containing a `tool_call_id`.

## Step 2: Send the tool result back

Call the MCP tool directly (via your MCP server), then send the result back to LiteLLM:

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6",
    "messages": [
      {"role": "user", "content": "List all tables in the database"},
      {"role": "assistant", "tool_calls": [{"function": {"arguments": "{}", "name": "list_tables"}, "id": "CALL_ID_FROM_STEP_1", "type": "function"}]},
      {"role": "tool", "tool_call_id": "CALL_ID_FROM_STEP_1", "content": "[\"projects\", \"users\"]"}
    ]
  }'
```

Replace the `content` with the actual result from calling the tool on your MCP server.

## Add MCP Servers to Claude Code

Use `claude mcp add` to connect MCP servers to Claude Code. The server name in the URL path must match the key defined under `mcp_servers:` in your LiteLLM config.

```bash
# Kokoro TTS MCP
claude mcp add --transport http -s user kokoro http://192.168.5.233:4001/mcp/kokoro_tts \
  --header "Authorization: Bearer sk-your-master-key"

# Open-vocabulary detector MCP (tool: detect_objects)
claude mcp add --transport http -s user detector http://192.168.5.233:4001/mcp/detector \
  --header "Authorization: Bearer sk-your-master-key"

# Trino federated SQL MCP (tools: list_catalogs, list_schemas, list_tables,
# describe_table, run_query)
claude mcp add --transport http -s user trino http://192.168.5.233:4001/mcp/trino \
  --header "Authorization: Bearer sk-your-master-key"

# Phoenix MCP
claude mcp add --transport http -s user phoenix http://192.168.5.233:4001/mcp/phoenix \
  --header "Authorization: Bearer sk-your-phoenix-auth-value"
```

Run `/mcp` inside Claude Code afterwards to confirm each server shows as connected with its tools listed.

**Trino reads everything Trino can see.** `trino-mcp` has no host port, so LiteLLM's `/mcp/trino` is the only way to reach it from outside Docker. Once it is added, Claude Code can query every catalog: `postgres_phoenix` (Phoenix production), `postgres_litellm`, `postgres_roofix`, `postgres_sandbox`, `postgres_classifier`, `postgres_supabase` (its `trino_reader` role has `BYPASSRLS`, so that includes `auth.users`), and the hosted Supabase projects. Queries are SELECT-only and capped at `TRINO_MCP_MAX_ROWS` rows and `TRINO_MCP_MAX_RUNTIME_S` seconds, but there is no per-catalog access control (see `ai/trino/TRINO.md § MCP tools`). With a virtual key instead of the master key, the key must be allowed the `trino` MCP server or LiteLLM refuses the connection.

## MCP API Token

Some MCP servers issue long-lived API tokens via a browser-based OAuth flow. Check your MCP server's documentation for token acquisition.
