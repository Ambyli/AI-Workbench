#!/bin/sh
# Read-only federation role for Trino (ai/trino/catalogs/postgres_classifier.properties).
#
# Mounted into classifier-db's /docker-entrypoint-initdb.d/, so the postgres
# image runs it ONCE, on the first start of an empty classifier_db volume, as
# POSTGRES_USER over the init-time unix socket. It is a .sh rather than a .sql
# so it can read TRINO_READER_PASSWORD (= CLASSIFIER_TRINO_READER_PASSWORD in
# .env) from the container environment.
#
# Safe to SOURCE as well as execute — the entrypoint sources a .sh that has no
# exec bit, and a Windows checkout has none. So: no `exit`, no `set`, and every
# variable is local to the function. psql's ON_ERROR_STOP still fails the init
# (and so the container) on a real SQL error.
#
# Idempotent. Re-run by hand to retrofit an existing volume, or to rotate the
# password after changing CLASSIFIER_TRINO_READER_PASSWORD in .env (then
# `make up classifier` first, so the container env carries the new value):
#
#   docker exec classifier-db sh /docker-entrypoint-initdb.d/10-trino-reader.sh
#
# Posture: pg_read_all_data (every table, including ones created later) +
# default_transaction_read_only + a statement timeout — the same as
# ai/supabase/volumes/db/trino-reader.sql, minus BYPASSRLS: nothing in this
# database uses row-level security. INHERIT is explicit: on Postgres 16+ a
# NOINHERIT role does not get pg_read_all_data's privileges without a SET ROLE,
# which Trino never issues.
#
# An EMPTY password skips the role with a message instead of failing: the
# classifier itself must not depend on Trino's credentials, and the only thing
# that breaks is the postgres_classifier catalog.

classifier_trino_reader() {
    local reader_user reader_pass db
    reader_user="${TRINO_READER_USER:-trino_reader}"
    reader_pass="${TRINO_READER_PASSWORD:-}"
    db="${POSTGRES_DB:-classifier}"

    if [ -z "$reader_pass" ]; then
        echo "trino-reader: CLASSIFIER_TRINO_READER_PASSWORD is empty — not creating role" \
             "'$reader_user'; the postgres_classifier Trino catalog cannot connect until it" \
             "is set and this script is re-run." >&2
        return 0
    fi

    psql -v ON_ERROR_STOP=1 --no-psqlrc --username "${POSTGRES_USER:-postgres}" --dbname "$db" \
        -v trino_user="$reader_user" -v trino_pass="$reader_pass" -v db="$db" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'trino_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'trino_user')
\gexec

ALTER ROLE :"trino_user" WITH LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'trino_pass';
ALTER ROLE :"trino_user" SET default_transaction_read_only = on;
ALTER ROLE :"trino_user" SET statement_timeout = '300s';

GRANT pg_read_all_data TO :"trino_user";
GRANT CONNECT ON DATABASE :"db" TO :"trino_user";
SQL
    echo "trino-reader: role '$reader_user' ready on database '$db'"
}

classifier_trino_reader
