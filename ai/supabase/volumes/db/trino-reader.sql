-- Read-only federation role for Trino (ai/trino/catalogs/postgres_supabase.properties).
--
-- Runs once at first init, in the init-scripts phase, as the superuser `postgres`.
-- Only `.sql` files run inside init-scripts/, and environment reaches SQL via the
-- psql backtick \set trick — the same mechanism upstream's own roles.sql uses.
--
-- Idempotent. Re-run by hand to retrofit an existing data volume, or to rotate the
-- password after changing SUPABASE_TRINO_READER_PASSWORD in .env:
--
--   docker compose -f ai/supabase/docker-compose.supabase.yml --env-file .env -p ai-supabase \
--     exec supabase-db psql -v ON_ERROR_STOP=1 -U supabase_admin -d postgres \
--     -f /docker-entrypoint-initdb.d/init-scripts/99-trino-reader.sql
--
-- BYPASSRLS mirrors upstream's own supabase_read_only_user. Without it every
-- RLS-enabled table (storage.objects today, any future app table) reads as ZERO
-- ROWS in Trino with no error at all — the worst possible failure mode for a
-- federated catalog.
--
-- Runbook note: pg_read_all_data covers auth.users and vault.secrets. Treat
-- SUPABASE_TRINO_READER_PASSWORD as a production-grade secret.

\set trino_user `echo "$TRINO_READER_USER"`
\set trino_pass `echo "$TRINO_READER_PASSWORD"`

SELECT format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS', :'trino_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'trino_user')
\gexec

ALTER ROLE :"trino_user" WITH LOGIN PASSWORD :'trino_pass';
ALTER ROLE :"trino_user" SET default_transaction_read_only = on;
ALTER ROLE :"trino_user" SET statement_timeout = '300s';

GRANT pg_read_all_data TO :"trino_user";
GRANT CONNECT ON DATABASE postgres TO :"trino_user";
