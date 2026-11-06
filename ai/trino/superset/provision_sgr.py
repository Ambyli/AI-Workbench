"""Provision the LiteLLM SGR dataset, charts and dashboard (sgr_spec.py).

bootstrap.sh runs this on every container start, after the Trino connection
is registered. Idempotent, and it never fails the boot: each step commits on
its own or rolls back and logs, and the script always exits 0.

* The ``litellm_sgr_monthly`` virtual dataset on the ``trino`` database:
  created if missing, then its SQL is re-read from
  assets/litellm_sgr_monthly.sql and its columns re-synced from
  sgr_spec.COLUMNS on every run, so `make build trino superset && make up
  trino superset` ships an SQL change. The SQL itself is not run here.
* The two charts and the dashboard are created only when no object carries
  their fixed UUID, so edits made in the UI survive restarts; delete one to
  get the shipped version back on the next start.

Built against apache/superset:4.0.2's ORM (SqlaTable / TableColumn, Slice,
Dashboard). ai/trino/TRINO.md § Counting LiteLLM SGRs.
"""

from __future__ import annotations

import copy
import json
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import sgr_spec as spec

HERE = Path(__file__).resolve().parent


def log(message: str) -> None:
    print(f"[provision_sgr] {message}", flush=True)


def main() -> int:
    try:
        sql = (HERE / spec.SQL_FILE).read_text(encoding="utf-8").strip()
        from superset.app import create_app

        app = create_app()
    except Exception:  # noqa: BLE001 -- never fail the boot
        log("setup failed, nothing provisioned:\n" + traceback.format_exc())
        return 0

    with app.app_context():
        from superset import db

        def step(name: str, fn: Callable[[], Any]) -> Any:
            try:
                result = fn()
                db.session.commit()
                return result
            except Exception:  # noqa: BLE001 -- log, roll back, carry on
                db.session.rollback()
                log(f"{name} failed:\n" + traceback.format_exc())
                return None

        dataset = step("dataset", lambda: sync_dataset(db, sql))
        if dataset is None:
            return 0
        charts = {}
        for chart in spec.CHARTS:
            slc = step(f"chart {chart['slice_name']!r}", lambda c=chart: ensure_chart(db, dataset, c))
            if slc is not None:
                charts[chart["uuid"]] = slc
        step("dashboard", lambda: ensure_dashboard(db, charts))
    return 0


def _set(obj: Any, attr: str, value: Any) -> bool:
    """Assign only on change, so an unchanged boot leaves the rows (and
    ``changed_on``, which keys Superset's chart cache) alone."""
    if getattr(obj, attr) == value:
        return False
    setattr(obj, attr, value)
    return True


def sync_dataset(db: Any, sql: str) -> Optional[Any]:
    from superset.connectors.sqla.models import SqlaTable, TableColumn
    from superset.models.core import Database

    database = db.session.query(Database).filter_by(database_name=spec.DATABASE_NAME).one_or_none()
    if database is None:
        log(f"no database named {spec.DATABASE_NAME!r}; skipping the SGR dashboard")
        return None

    dataset_uuid = uuid.UUID(spec.DATASET_UUID)
    dataset = db.session.query(SqlaTable).filter_by(uuid=dataset_uuid).one_or_none() or (
        db.session.query(SqlaTable)
        .filter_by(database_id=database.id, table_name=spec.DATASET_NAME)
        .first()
    )
    changes = []
    if dataset is None:
        dataset = SqlaTable(table_name=spec.DATASET_NAME, database=database, uuid=dataset_uuid)
        db.session.add(dataset)
        changes.append("created")
    for attr, value in (
        ("sql", sql),
        ("main_dttm_col", spec.MAIN_DTTM_COL),
        ("description", spec.DATASET_DESCRIPTION),
    ):
        if _set(dataset, attr, value):
            changes.append(attr)

    # Columns come from the spec, not from running the SQL. Calculated
    # columns added in the UI (an expression) are kept; a physical column
    # the SQL no longer selects is dropped.
    existing = {col.column_name: col for col in dataset.columns}
    for name, type_, is_dttm in spec.COLUMNS:
        col = existing.pop(name, None)
        if col is None:
            dataset.columns.append(
                TableColumn(column_name=name, type=type_, is_dttm=is_dttm, groupby=True, filterable=True)
            )
            changes.append(f"+{name}")
            continue
        touched = [
            _set(col, "type", type_),
            _set(col, "is_dttm", is_dttm),
            _set(col, "groupby", True),
            _set(col, "filterable", True),
            bool(col.expression) and _set(col, "expression", None),
        ]
        if any(touched):
            changes.append(f"~{name}")
    for name, col in existing.items():
        if not col.expression:
            dataset.columns.remove(col)
            changes.append(f"-{name}")

    log(f"dataset {spec.DATASET_NAME!r}: " + (", ".join(changes) if changes else "up to date"))
    return dataset


def ensure_chart(db: Any, dataset: Any, chart: dict) -> Any:
    from superset.models.slice import Slice

    chart_uuid = uuid.UUID(chart["uuid"])
    slc = db.session.query(Slice).filter_by(uuid=chart_uuid).one_or_none()
    if slc is not None:
        log(f"chart {chart['slice_name']!r} exists (id {slc.id}); left as is")
        return slc
    db.session.flush()  # a new dataset needs its id first
    params = dict(chart["params"], datasource=f"{dataset.id}__table")
    slc = Slice(
        uuid=chart_uuid,
        slice_name=chart["slice_name"],
        viz_type=chart["viz_type"],
        description=chart["description"],
        datasource_type="table",
        datasource_id=dataset.id,
        params=json.dumps(params, indent=2, sort_keys=True),
        owners=[],
    )
    db.session.add(slc)
    db.session.flush()
    log(f"chart {chart['slice_name']!r} created (id {slc.id})")
    return slc


def ensure_dashboard(db: Any, charts: dict) -> None:
    from superset.models.dashboard import Dashboard

    dash_spec = spec.DASHBOARD
    dash_uuid = uuid.UUID(dash_spec["uuid"])
    existing = db.session.query(Dashboard).filter_by(uuid=dash_uuid).one_or_none()
    if existing is not None:
        log(f"dashboard {dash_spec['dashboard_title']!r} exists (id {existing.id}); left as is")
        return
    missing = [c["slice_name"] for c in spec.CHARTS if c["uuid"] not in charts]
    if missing:
        log(f"dashboard {dash_spec['dashboard_title']!r} not created; missing charts: {missing}")
        return

    position = copy.deepcopy(dash_spec["position"])
    for component in position.values():
        if isinstance(component, dict) and component.get("type") == "CHART":
            slc = charts[component["meta"]["uuid"]]
            component["meta"]["chartId"] = slc.id
            component["meta"]["sliceName"] = slc.slice_name
    slug = dash_spec["slug"]
    if db.session.query(Dashboard).filter_by(slug=slug).first() is not None:
        log(f"slug {slug!r} is taken; creating the dashboard without one")
        slug = None
    dash = Dashboard(
        uuid=dash_uuid,
        dashboard_title=dash_spec["dashboard_title"],
        slug=slug,
        position_json=json.dumps(position, indent=2),
        json_metadata=json.dumps(dash_spec["metadata"], indent=2),
        css="",
        published=True,
        slices=[charts[c["uuid"]] for c in spec.CHARTS],
        owners=[],
    )
    db.session.add(dash)
    db.session.flush()
    log(f"dashboard {dash_spec['dashboard_title']!r} created (id {dash.id})")


if __name__ == "__main__":
    sys.exit(main())
