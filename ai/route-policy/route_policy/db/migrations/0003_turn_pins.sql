-- 0003_turn_pins -- one route per turn, and Open WebUI's background tasks
-- told apart (ROUTE_POLICY.md § Turn pinning, § Background tasks).
--
-- Applied once by route_policy/db/migrations (recorded in schema_migrations
-- with this file's SHA-256: never edit it after it has run on the box -- add
-- 0004_*.sql instead). Idempotent anyway, so a hand-made database converges.
-- Only adds: two columns with defaults, an index, appended view columns --
-- rolling the service back leaves them unused, not broken.

-- A ruleset with `pin: turn` holds every call of one turn (the same chat id
-- and message id: the tool rounds of one answer, Open WebUI's hidden
-- auto-continue nudge) to the route of the turn's FIRST decision of that
-- ruleset id, unless the fresh decision is local-private. Every later call
-- of the turn records that first decision here -- whether the pin changed
-- its route or not (the reason says which); NULL for a turn's first
-- decision, for a call without both ids, and for every decision of a
-- ruleset that does not pin. No foreign key, like shadow_of: rows are
-- written in the background, in no guaranteed order.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS pinned_from uuid;

-- The LiteLLM hook's verdict that the request was an Open WebUI background
-- task (a chat title, tags, follow-ups -- `### Task:` prompts sent without
-- streaming), not a chat answer. An llm_classifier never classifies one,
-- and a background task never pins a turn nor is pinned to one. Rows written
-- before this migration (and by a hook that does not send the flag) are
-- false.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS background_task boolean NOT NULL DEFAULT false;

-- The pin lookup: the earliest decision of a ruleset id for one
-- (chat id, message id), on the /decide path within DB_READ_BUDGET_S.
CREATE INDEX IF NOT EXISTS decisions_turn_idx ON decisions (chat_id, message_id, ruleset_id, created_at);

-- decision_outcomes: 0002's columns, then pinned_from and background_task
-- appended (dropped and recreated, as 0002 did, so a re-run converges).
DROP VIEW IF EXISTS decision_outcomes;
CREATE VIEW decision_outcomes AS
SELECT
    decision_id,
    d.created_at                                   AS decided_at,
    o.created_at                                   AS observed_at,
    d.chat_id,
    d.message_id,
    d.user_id,
    d.key_alias,
    COALESCE(d.litellm_call_id, o.litellm_call_id) AS litellm_call_id,
    o.response_id,
    d.requested_model,
    d.chosen_route,
    d.applied,
    d.mode,
    d.ruleset_id,
    d.ruleset_version,
    d.reason,
    d.scores,
    d.latency_ms,
    d.error                                        AS decision_error,
    o.served_model_group,
    o.served_deployment,
    o.overflow_reason,
    o.finish_reason,
    o.tool_calls,
    o.status,
    o.error                                        AS outcome_error,
    (d.chosen_route = o.served_model_group)        AS agree,
    d.pinned_from,
    d.background_task
FROM (SELECT * FROM decisions WHERE shadow_of IS NULL) d
FULL OUTER JOIN outcomes o USING (decision_id);

-- shadow_comparisons: 0002's columns, then the turn and task columns of both
-- sides appended.
DROP VIEW IF EXISTS shadow_comparisons;
CREATE VIEW shadow_comparisons AS
SELECT
    s.decision_id                                  AS shadow_decision_id,
    s.shadow_of                                    AS primary_decision_id,
    s.created_at                                   AS shadow_decided_at,
    p.created_at                                   AS primary_decided_at,
    o.created_at                                   AS observed_at,
    COALESCE(p.chat_id, s.chat_id)                 AS chat_id,
    COALESCE(p.message_id, s.message_id)           AS message_id,
    COALESCE(p.user_id, s.user_id)                 AS user_id,
    s.requested_model,
    s.ruleset_id                                   AS shadow_ruleset_id,
    s.ruleset_version                              AS shadow_ruleset_version,
    p.ruleset_id                                   AS primary_ruleset_id,
    p.ruleset_version                              AS primary_ruleset_version,
    s.chosen_route                                 AS shadow_route,
    p.chosen_route                                 AS primary_route,
    p.applied                                      AS primary_applied,
    o.served_model_group,
    o.served_deployment,
    s.reason                                       AS shadow_reason,
    p.reason                                       AS primary_reason,
    s.error                                        AS shadow_error,
    (s.chosen_route = p.chosen_route)              AS agree_with_primary,
    (s.chosen_route = o.served_model_group)        AS agree_with_served,
    s.pinned_from                                  AS shadow_pinned_from,
    p.pinned_from                                  AS primary_pinned_from,
    s.background_task
FROM decisions s
LEFT JOIN decisions p ON p.decision_id = s.shadow_of
LEFT JOIN outcomes o ON o.decision_id = s.shadow_of
WHERE s.shadow_of IS NOT NULL;
