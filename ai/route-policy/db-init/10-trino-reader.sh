#!/bin/sh
# Read-only federation role for Trino (ai/trino/catalogs/postgres_route_policy.properties).
#
# The classifier's recipe (ai/classifier/db-init/10-trino-reader.sh), for
# route-policy-db. Mounted into /docker-entrypoint-initdb.d/, so the postgres
# image runs it ONCE, on the first start of an empty route_policy_db volume,
# as POSTGRES_USER over the init-time unix socket. A .sh rather than a .sql so
# it can read TRINO_READER_PASSWORD (= ROUTE_POLICY_TRINO_READER_PASSWORD in
# .env) from the container environment.
#
# Safe to SOURCE as well as execute -- the entrypoint sources a .sh that has
# no exec bit, and a Windows checkout has none. So: no `exit`, no `set`, and
# every variable is local to the function. psql's ON_ERROR_STOP still fails
# the init (and so the container) on a real SQL error.
#
# Idempotent. Re-run by hand to retrofit an existing volume, or to rotate the
# password after changing ROUTE_POLICY_TRINO_READER_PASSWORD in .env (then
# `make up route-policy route-policy-db` first, so the container env carries
# the new value):
#
#   docker exec route-policy-db sh /docker-entrypoint-initdb.d/10-trino-reader.sh
#
# Posture: pg_read_all_data (every table and view, including the ones the
# service's migrations create later) + default_transaction_read_only + a
# statement timeout. No BYPASSRLS: nothing in this database uses row-level
# security. INHERIT is explicit: on Postgres 16+ a NOINHERIT role does not get
# pg_read_all_data's privileges without a SET ROLE, which Trino never issues.
#
# What it can read: rulesets, bindings, decisions (chat / message / user ids,
# routes, votes and their reasons -- never message text), evaluations,
# outcomes. Treat the password as a real secret all the same.
#
# An EMPTY password skips the role with a message instead of failing: the
# service itself must not depend on Trino's credentials, and the only thing
# that breaks is the postgres_route_policy catalog.

route_policy_trino_reader() {
    local reader_user reader_pass db
    reader_user="${TRINO_READER_USER:-trino_reader}"
    reader_pass="${TRINO_READER_PASSWORD:-}"
    db="${POSTGRES_DB:-route_policy}"

    if [ -z "$reader_pass" ]; then
        echo "trino-reader: ROUTE_POLICY_TRINO_READER_PASSWORD is empty — not creating role" \
             "'$reader_user'; the postgres_route_policy Trino catalog cannot connect until it" \
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

route_policy_trino_reader
