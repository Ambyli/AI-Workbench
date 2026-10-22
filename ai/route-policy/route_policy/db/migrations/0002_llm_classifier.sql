-- 0002_llm_classifier -- phase 4 (ROUTE_POLICY_PLAN § 4, § 5): shadow
-- bindings, shadow decisions, and the llm_classifier's analyses.
--
-- Applied once by route_policy/db/migrations (recorded in schema_migrations
-- with this file's SHA-256: never edit it after it has run on the box -- add
-- 0003_*.sql instead). Idempotent anyway, so a hand-made database converges.
-- Only adds: tables, a nullable column, indexes, views -- rolling the service
-- back leaves them unused, not broken.
--
-- Same Trino-friendly types as 0001 (jsonb instead of arrays, uuid), read as
-- postgres_route_policy.public.* through trino_reader, whose
-- pg_read_all_data covers the new tables and views with no grant.
--
-- No conversation text is stored here either. `analyses` keeps a hash of
-- the transcript that was classified (input_key), never the transcript, and
-- the model's short reason for its verdict (<= 200 characters, enforced
-- below as well as in the code).

-- A binding's SHADOW slots: rulesets decided beside the primary after
-- /decide has answered, logged, never applied. Seeded from bindings.yaml
-- (`shadow: [...]`) at every startup, in order.
CREATE TABLE IF NOT EXISTS binding_shadows (
    binding_id  bigint   NOT NULL REFERENCES bindings (id) ON DELETE CASCADE,
    position    smallint NOT NULL,
    ruleset_id  text     NOT NULL,
    version     integer  NOT NULL,
    PRIMARY KEY (binding_id, position),
    UNIQUE (binding_id, ruleset_id, version),
    FOREIGN KEY (ruleset_id, version) REFERENCES rulesets (id, version)
);

-- A shadow decision is a decisions row whose decision_id is
-- uuid5(primary decision_id, '<ruleset>@<version>') and whose shadow_of is
-- the primary's decision_id (NULL for a primary). No foreign key, on
-- purpose: the primary's own write may still be in flight -- or never land
-- -- when the shadow's is made, the same reasoning as outcomes.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS shadow_of uuid;
CREATE INDEX IF NOT EXISTS decisions_shadow_of_idx ON decisions (shadow_of);

-- One row per classified conversation PREFIX (the transcript up to one
-- user message, as an llm_classifier renders it). The first write wins.
CREATE TABLE IF NOT EXISTS analyses (
    -- sha256(evaluator fingerprint, transcript): the verdict's identity.
    input_key          text        PRIMARY KEY,
    created_at         timestamptz NOT NULL DEFAULT now(),
    -- sha256 over the evaluator's model, criteria, otherwise, min_score,
    -- history: which configuration asked.
    fingerprint        text        NOT NULL,
    -- The document classifier's registry id that answered.
    model_id           text        NOT NULL,
    -- Who asked first: the ruleset, evaluator and decision (the decision may
    -- be a shadow one: shadow_of points at its primary) and the turn's ids.
    ruleset_id         text,
    ruleset_version    integer,
    evaluator_id       text,
    chat_id            text,
    message_id         text,
    decision_id        uuid,
    -- The prefix: ends with the exchange_index-th user message (1-based);
    -- input_chars is the length of the transcript sent (never the text).
    exchange_index     integer     NOT NULL,
    input_chars        integer     NOT NULL,
    -- The verdict: the route (NULL when the classification failed, or no
    -- criterion reached min_score and the evaluator has no `otherwise`),
    -- its confidence, {route: 1-10} per criterion (null where one errored),
    -- and the reason of the best-scoring criterion.
    route              text,
    confidence         double precision,
    scores             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    reason             text        CHECK (reason IS NULL OR char_length(reason) <= 200),
    -- ok | incomplete | unavailable | refused | failed | timeout | not_local
    status             text        NOT NULL,
    error              text,
    -- What it cost, copied from the classifier's job before route-policy
    -- deleted it (DELETE /jobs/{id} takes the job's llm_calls rows with it).
    classifier_job_id  text,
    calls              integer,
    prompt_tokens      integer,
    completion_tokens  integer,
    model_seconds      double precision,
    -- Submit -> the job seen running (± ROUTE_POLICY_CLASSIFY_POLL_S): the
    -- wait in the classifier's queue, behind document jobs.
    queue_ms           double precision,
    -- The whole cycle, from the locality check to the result.
    latency_ms         double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS analyses_created_at_idx ON analyses (created_at);
CREATE INDEX IF NOT EXISTS analyses_chat_id_idx ON analyses (chat_id, created_at);
CREATE INDEX IF NOT EXISTS analyses_decision_id_idx ON analyses (decision_id);

-- decision_outcomes, over PRIMARY decisions only (shadow rows never have an
-- outcome of their own). Same columns as 0001's; a view's column list can
-- only be appended to by CREATE OR REPLACE, so it is dropped and recreated.
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
    (d.chosen_route = o.served_model_group)        AS agree
FROM (SELECT * FROM decisions WHERE shadow_of IS NULL) d
FULL OUTER JOIN outcomes o USING (decision_id);

-- Every shadow decision beside the primary it shadowed and what served that
-- turn: the promotion measure. agree_with_primary = the shadow chose what
-- the live ruleset chose; agree_with_served = it chose the chain that really
-- answered (null until /observe arrives).
CREATE OR REPLACE VIEW shadow_comparisons AS
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
    (s.chosen_route = o.served_model_group)        AS agree_with_served
FROM decisions s
LEFT JOIN decisions p ON p.decision_id = s.shadow_of
LEFT JOIN outcomes o ON o.decision_id = s.shadow_of
WHERE s.shadow_of IS NOT NULL;
