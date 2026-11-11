# supabase request and response schemas

Generated from [`supabase.schemas.json`](supabase.schemas.json) by `uv run --no-sync python scripts/api_schemas.py render` (root `CLAUDE.md` § Postman collections) — edit that file, never this one; an entry whose `$comment` says `generated from …` comes from the code and is rewritten by the service's `test_api_schemas.py`. An error status not listed under an endpoint answers the generic error body the Postman collection's description names. Each endpoint's prose: [`SUPABASE.md`](SUPABASE.md).

## `GET /auth/v1/health`

No key.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["version", "name", "description"],
  "properties": {
    "version": {"type": "string", "description": "GoTrue's version"},
    "name": {"const": "GoTrue"},
    "description": {"const": "GoTrue is a user registration and authentication API"}
  }
}
```

## `GET /rest/v1/`

The tables and views of SUPABASE_PGRST_DB_SCHEMAS, as the `apikey`'s role sees them (nearly empty here: this repo creates no application tables).

Response `200` (`application/openapi+json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["swagger", "info", "paths"],
  "properties": {
    "swagger": {"const": "2.0"},
    "info": {"type": "object", "additionalProperties": true},
    "paths": {
      "type": "object",
      "additionalProperties": true,
      "description": "One per table / view / function"
    },
    "definitions": {
      "type": "object",
      "additionalProperties": true,
      "description": "One per table / view: its columns"
    }
  }
}
```

## `GET /rest/v1/:table`

One object per row, the columns `select` names (`*`: all); at most SUPABASE_PGRST_DB_MAX_ROWS (1000) whatever `limit` says. An empty array where rows exist is row-level security filtering the role.

Response `200` (`application/json`):

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": true,
    "description": "A row: column name -> value"
  }
}
```

## `GET /storage/v1/status`

Response `200`: no body — the API is up.

## `GET /functions/v1/hello`

Unauthenticated while SUPABASE_FUNCTIONS_VERIFY_JWT is false (this deployment's default).

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": false,
  "$comment": "ours: the whole body",
  "required": ["message", "method", "timestamp"],
  "properties": {
    "message": {"const": "hello from supabase edge functions"},
    "method": {"type": "string", "description": "The request's method"},
    "timestamp": {"type": "string", "description": "UTC ISO 8601 (`toISOString()`)"}
  }
}
```

Response `401` (`application/json`) — only with SUPABASE_FUNCTIONS_VERIFY_JWT=true: no, or a bad, Bearer token (the same code in the `sb-error-code` header):

```json
{
  "type": "object",
  "additionalProperties": false,
  "$comment": "main/index.ts getAuthErrorResponse",
  "required": ["code", "message", "msg"],
  "properties": {
    "code": {
      "enum": [
        "UNAUTHORIZED_LEGACY_JWT",
        "UNAUTHORIZED_ASYMMETRIC_JWT",
        "UNAUTHORIZED_INVALID_JWT_FORMAT",
        "UNAUTHORIZED_UNSUPPORTED_TOKEN_ALGORITHM",
        "UNAUTHORIZED_NO_AUTH_HEADER"
      ]
    },
    "message": {"type": "string"},
    "msg": {"type": "string", "description": "Deprecated copy of `message`"}
  }
}
```

## `GET /realtime/v1/api/tenants/realtime-dev/health`

`healthy: false, db_connected: false` after a SUPABASE_JWT_SECRET rotation is the one-shot-secret trap (SUPABASE.md § Secrets that are one-shot). 404 for an unknown tenant.

Response `200` (`application/json`):

```json
{
  "type": "object",
  "additionalProperties": true,
  "required": ["data"],
  "properties": {
    "data": {
      "type": "object",
      "additionalProperties": true,
      "required": ["healthy", "db_connected", "connected_cluster"],
      "properties": {
        "healthy": {"type": "boolean"},
        "db_connected": {"type": "boolean"},
        "replication_connected": {"type": "boolean"},
        "connected_cluster": {"type": "integer", "description": "Clients connected to the tenant"},
        "region": {"type": ["string", "null"]},
        "node": {"type": "string"}
      }
    }
  }
}
```

## `GET /`

Basic auth: SUPABASE_DASHBOARD_USERNAME / SUPABASE_DASHBOARD_PASSWORD (401 without).

Response `200` (`text/html`) — Studio's page.
