-- LiteLLM Successful Gateway Requests (SGRs) per month, by bucket.
--
-- LiteLLM Enterprise licensing meters SGRs: every call LiteLLM logs in
-- LiteLLM_SpendLogs with a success status, counted once per request. Applied
-- here as one row per request_id (the primary key of the table, so
-- count(*)), kept unless status is "failure". A missing or empty status
-- counts as a success, as the LiteLLM guide says.
--
-- Months are UTC calendar months: starttime is stored in UTC (timestamp
-- without time zone), so date_trunc cuts at 00:00 UTC on the 1st, not at
-- local midnight.
--
-- bucket
--   model                     every other non-empty call_type (acompletion,
--                             aresponses, anthropic_messages, aspeech, ...)
--   mcp                       call_mcp_tool, list_mcp_tools
--   a2a                       asend_message, send_message (LiteLLM 1.95.0)
--   pass-through: job status  pass_through_endpoint to .../jobs/<id> or
--                             .../jobs/<id>/progress, any query string. The
--                             method is not logged, so besides the GET
--                             status polls this holds the one
--                             DELETE /jobs/<id> per job.
--   pass-through: other       every other pass_through_endpoint call, the
--                             /jobs/<id>/artifacts/... downloads included
--   other                     empty call_type
-- target is the host and port in the api_base of a pass-through, else
-- model_group (empty when LiteLLM logged none, as for MCP calls).
--
-- No JSON functions (metadata etc.): Trino timed out on them at this volume
-- (about 1M rows a month). Superset uses this file as the virtual dataset
-- litellm_sgr_monthly, re-synced on every boot by provision_sgr.py: keep it
-- one SELECT with no trailing semicolon, no percent sign and no Jinja
-- braces, its columns in step with sgr_spec.COLUMNS and its buckets with
-- sgr_spec.BUCKETS (unit-tests/trino checks all of it).
-- ai/trino/TRINO.md, section "Counting LiteLLM SGRs".
SELECT
    date_trunc('month', starttime) AS month,
    CASE
        WHEN call_type IN ('call_mcp_tool', 'list_mcp_tools') THEN 'mcp'
        WHEN call_type IN ('asend_message', 'send_message') THEN 'a2a'
        WHEN call_type = 'pass_through_endpoint'
            AND regexp_like(api_base, '/jobs/[0-9A-Fa-f-]+(/progress)?([?].*)?$')
            THEN 'pass-through: job status'
        WHEN call_type = 'pass_through_endpoint' THEN 'pass-through: other'
        WHEN coalesce(call_type, '') = '' THEN 'other'
        ELSE 'model'
    END AS bucket,
    coalesce(call_type, '') AS call_type,
    CASE
        WHEN call_type = 'pass_through_endpoint'
            THEN coalesce(regexp_extract(api_base, '^[a-z]+://([^/]+)', 1), '')
        ELSE coalesce(model_group, '')
    END AS target,
    count(*) AS sgr
FROM postgres_litellm.public.litellm_spendlogs
WHERE coalesce(nullif(status, ''), 'success') <> 'failure'
GROUP BY 1, 2, 3, 4
