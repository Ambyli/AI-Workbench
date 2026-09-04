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

USER superset

ENV PYTHONPATH=/app/pythonpath:$PYTHONPATH

# The default entrypoint runs docker-entrypoint-init.d/*.sh before
# gunicorn — that's where bootstrap.sh runs `superset db upgrade` and
# registers the Trino connection on first boot.
