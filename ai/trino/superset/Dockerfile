# Superset with the SQLAlchemy drivers we need pre-installed:
#   * sqlalchemy-trino  → the "trino://" URL scheme used by TRINO_URL
#   * psycopg2-binary   → for the Superset metadata DB on superset-db
#
# apache/superset:4.0.2 already ships psycopg2, but bake it explicitly
# so an image bump doesn't drop it.

FROM apache/superset:4.0.2

USER root

RUN pip install --no-cache-dir \
      sqlalchemy-trino==0.5.0 \
      trino==0.328.0 \
      psycopg2-binary==2.9.9

COPY superset_config.py /app/pythonpath/superset_config.py
COPY bootstrap.sh       /app/docker-entrypoint-init.d/bootstrap.sh
RUN chmod +x /app/docker-entrypoint-init.d/bootstrap.sh
# The LiteLLM SGR dashboard (TRINO.md § Counting LiteLLM SGRs): bootstrap.sh
# runs provision_sgr.py, which reads sgr_spec.py and assets/ beside it.
COPY provision_sgr.py sgr_spec.py /app/provision/
COPY assets/                      /app/provision/assets/

USER superset

ENV PYTHONPATH=/app/pythonpath:$PYTHONPATH

# apache/superset:4.0.2 has no ENTRYPOINT and its CMD, /usr/bin/run-server.sh,
# only starts gunicorn, so nothing runs bootstrap.sh by itself. Run it first
# (`superset db upgrade`, admin, `superset init`, the Trino connection, the
# SGR dashboard), then start Superset whether or not it succeeded.
CMD ["/bin/sh", "-c", "/app/docker-entrypoint-init.d/bootstrap.sh || echo '[bootstrap] failed (see above); starting Superset anyway' >&2; exec /usr/bin/run-server.sh"]
