#!/bin/sh
# Superset first-boot bootstrap.
#
# Runs before gunicorn each time the container starts. Idempotent:
#   * `superset db upgrade` is a no-op after the first apply.
#   * `fab create-admin` errors if the admin already exists; we swallow.
#   * database-registration Python skips the insert if the connection
#     name already exists.
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
python <<'PY'
import os
from superset import db, security_manager
from superset.models.core import Database

TRINO_URL = os.environ["TRINO_URL"]
name = "trino"

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
