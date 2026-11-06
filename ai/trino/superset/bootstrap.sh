#!/bin/sh
# Superset bootstrap.
#
# The Dockerfile's CMD runs this before gunicorn each time the container
# starts (apache/superset's own CMD only starts gunicorn), and starts
# Superset whether or not it succeeds. Idempotent:
#   * `superset db upgrade` is a no-op after the first apply.
#   * `fab create-admin` errors if the admin already exists; we swallow.
#   * database-registration Python skips the insert if the connection
#     name already exists.
#   * provision_sgr.py re-syncs the LiteLLM SGR dataset and creates its
#     charts / dashboard only when missing (TRINO.md § Counting LiteLLM SGRs).
# Superset's models need an app context, so the Python steps build the app
# first. A failed registration or provisioning step is logged and skipped.
set -eu

echo "[bootstrap] applying metadata migrations…"
superset db upgrade

echo "[bootstrap] ensuring bootstrap admin exists…"
superset fab create-admin \
    --username admin \
    --firstname Admin \
    --lastname Bootstrap \
    --email admin@zeoenergy.com \
    --password "${SUPERSET_ADMIN_PASSWORD:-admin}" \
    || true

echo "[bootstrap] initializing roles + permissions…"
superset init

echo "[bootstrap] registering Trino connection…"
python <<'PY' || echo "[bootstrap] Trino registration failed (see above); continuing"
import os
from superset.app import create_app

TRINO_URL = os.environ["TRINO_URL"]
name = "trino"

with create_app().app_context():
    from superset import db
    from superset.models.core import Database

    session = db.session
    existing = session.query(Database).filter_by(database_name=name).first()
    if existing:
        print(f"[bootstrap] '{name}' connection already registered — skipping")
    else:
        session.add(Database(
            database_name=name,
            sqlalchemy_uri=TRINO_URL,
            expose_in_sqllab=True,
            allow_ctas=False,
            allow_cvas=False,
            allow_dml=False,
        ))
        session.commit()
        print(f"[bootstrap] registered '{name}' → {TRINO_URL}")
PY

echo "[bootstrap] provisioning the LiteLLM SGR dashboard…"
python /app/provision/provision_sgr.py \
    || echo "[bootstrap] SGR provisioning failed (see above); continuing"
