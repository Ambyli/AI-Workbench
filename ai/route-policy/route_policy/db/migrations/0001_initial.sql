-- 0001_initial -- the phase-2 schema (ROUTE_POLICY_PLAN § 5).
--
-- Applied once by route_policy/db/migrations (recorded in schema_migrations,
-- with this file's SHA-256: never edit it after it has run on the box -- add
-- 0002_*.sql instead). Idempotent anyway, so a hand-made database converges.
--
-- Read by Trino as postgres_route_policy.public.* through the read-only
-- trino_reader role (db-init/10-trino-reader.sh). Types are chosen for that:
-- jsonb (Trino: json) instead of arrays, which the PostgreSQL connector hides
-- by default (postgresql.array-mapping = DISABLED); uuid (Trino: uuid).
--
-- No conversation text is stored anywhere: decisions keep ids, the votes and
-- their reasons (pattern NAMES and indices, tool NAMES), never the messages.
--
-- Phase 4 adds `analyses` (llm_classifier runs) as its own migration.

-- One row per ruleset VERSION. Versions are immutable: startup refuses a
-- seed whose content_sha256 differs from the stored one.
CREATE TABLE IF NOT EXISTS rulesets (
    id              text        NOT NULL,
    version         integer     NOT NULL CHECK (version >= 1),
    -- The resolved document (refs inlined), exactly as hashed.
    document        jsonb       NOT NULL,
    content_sha256  text        NOT NULL,
    -- Where it came from: "rulesets/default.yaml" for a seeded file.
    source          text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL DEFAULT 'seed',
    -- First time a binding pointed at it / when the last one stopped (NULL
    -- while bound). Maintained by the seeding transaction.
    active_from     timestamptz,
    active_to       timestamptz,
    PRIMARY KEY (id, version)
);

-- Requested model (+ optional LiteLLM key alias) -> ruleset version. A NULL
-- key alias is the binding for every key; NULLS NOT DISTINCT makes it unique.
CREATE TABLE IF NOT EXISTS bindings (
    id              bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope_model     text        NOT NULL,
    scope_key_alias text,
    ruleset_id      text        NOT NULL,
    version         integer     NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text        NOT NULL DEFAULT 'seed',
    CONSTRAINT bindings_scope_key UNIQUE NULLS NOT DISTINCT (scope_model, scope_key_alias),
    FOREIGN KEY (ruleset_id, version) REFERENCES rulesets (id, version)
);

-- One row per POST /decide (the first write of a decision_id wins).
CREATE TABLE IF NOT EXISTS decisions (
    decision_id      uuid        PRIMARY KEY,
    created_at       timestamptz NOT NULL DEFAULT now(),
    chat_id          text,
    -- Null until Open WebUI forwards X-OpenWebUI-Message-Id (patch 0005).
    message_id       text,
    litellm_call_id  text,
    user_id          text,
    key_alias        text,
    requested_model  text        NOT NULL,
    chosen_route     text        NOT NULL,
    -- Whether the route may be applied: an enforce ruleset whose invariants held.
    applied          boolean     NOT NULL,
    mode             text        NOT NULL,
    ruleset_id       text        NOT NULL,
    ruleset_version  integer     NOT NULL,
    -- The combiner's explanation, plus any invariant override.
    reason           text,
    -- {route: Σ weight × confidence} over the votes.
    scores           jsonb       NOT NULL,
    -- The overflow layer's {api_base: {verdict, waiting, running}} as sent.
    backend_snapshot jsonb,
    -- Shape of the request, never its content.
    message_count    integer,
    tool_count       integer,
    latency_ms       double precision NOT NULL,
    -- Evaluator errors / an invariant violation; null when clean.
    error            text,
    -- The /decide response body exactly as returned (replayed on a duplicate).
    response         jsonb       NOT NULL,
    FOREIGN KEY (ruleset_id, ruleset_version) REFERENCES rulesets (id, version)
);
CREATE INDEX IF NOT EXISTS decisions_chat_id_idx ON decisions (chat_id, created_at);
CREATE INDEX IF NOT EXISTS decisions_created_at_idx ON decisions (created_at);
CREATE INDEX IF NOT EXISTS decisions_litellm_call_id_idx ON decisions (litellm_call_id);

-- One row per evaluator per decision, in ruleset order.
CREATE TABLE IF NOT EXISTS evaluations (
    decision_id   uuid        NOT NULL REFERENCES decisions (decision_id) ON DELETE CASCADE,
    position      smallint    NOT NULL,
    evaluator_id  text        NOT NULL,
    type          text        NOT NULL,
    role          text        NOT NULL,
    -- The route it voted for; null when it abstained.
    vote_route    text,
    weight        double precision NOT NULL,
    confidence    double precision,
    reason        text,
    abstained     boolean     NOT NULL,
    latency_ms    double precision NOT NULL,
    error         text,
    -- The type's declared detail (GET /evaluator-types).
    detail        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (decision_id, position)
);
CREATE INDEX IF NOT EXISTS evaluations_evaluator_idx ON evaluations (evaluator_id);

-- One row per POST /observe: what really answered. NO foreign key to
-- decisions on purpose: the hook fires /decide and /observe without waiting,
-- so an outcome can arrive first (or alone, if its /decide was lost).
CREATE TABLE IF NOT EXISTS outcomes (
    decision_id         uuid        PRIMARY KEY,
    created_at          timestamptz NOT NULL DEFAULT now(),
    litellm_call_id     text,
    -- The provider's response id (chatcmpl-...): LiteLLM_SpendLogs.request_id.
    response_id         text,
    served_model_group  text,
    served_deployment   text,
    overflow_reason     text,
    finish_reason       text,
    -- Tool names the answer called (jsonb array of strings).
    tool_calls          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    status              text        NOT NULL,
    error               text
);
CREATE INDEX IF NOT EXISTS outcomes_created_at_idx ON outcomes (created_at);
CREATE INDEX IF NOT EXISTS outcomes_response_id_idx ON outcomes (response_id);
CREATE INDEX IF NOT EXISTS outcomes_litellm_call_id_idx ON outcomes (litellm_call_id);

-- Every decision with its outcome, either side possibly missing (FULL JOIN:
-- an outcome whose decision never arrived still shows). `agree` is the
-- phase-2 success measure: the shadow route vs the chain that served.
-- CREATE OR REPLACE VIEW may only APPEND columns: a later migration that
-- changes one must DROP VIEW first.
CREATE OR REPLACE VIEW decision_outcomes AS
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
FROM decisions d
FULL OUTER JOIN outcomes o USING (decision_id);
