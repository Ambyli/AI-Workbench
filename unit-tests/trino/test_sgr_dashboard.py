"""Checks for the LiteLLM SGR dashboard Superset provisions at boot
(ai/trino/superset/: assets/litellm_sgr_monthly.sql, sgr_spec.py,
provision_sgr.py, bootstrap.sh, Dockerfile). TRINO.md § Counting LiteLLM SGRs.

No Superset install is needed: the spec is plain data, the SQL is read as
text, and the provisioner is only run to prove it gives up quietly (exit 0)
where Superset is missing. The SQL itself was validated live against Trino
when written; these tests keep the spec, the SQL and the image wiring in
step.

    UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/trino -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import uuid

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
SUPERSET_DIR = _ROOT / "ai" / "trino" / "superset"
BOOTSTRAP = SUPERSET_DIR / "bootstrap.sh"
DOCKERFILE = SUPERSET_DIR / "Dockerfile"

# provision_sgr.py imports sgr_spec as a sibling module, as it does in the image.
sys.path.insert(0, str(SUPERSET_DIR))
_spec = importlib.util.spec_from_file_location("sgr_spec", SUPERSET_DIR / "sgr_spec.py")
spec = importlib.util.module_from_spec(_spec)
sys.modules["sgr_spec"] = spec
_spec.loader.exec_module(spec)

SQL_TEXT = (SUPERSET_DIR / spec.SQL_FILE).read_text(encoding="utf-8")
SQL_CODE = "\n".join(line.split("--", 1)[0] for line in SQL_TEXT.splitlines()).strip()
COLUMN_NAMES = [name for name, _, _ in spec.COLUMNS]


def select_aliases(sql: str) -> list[str]:
    """The output column aliases of the (single) SELECT, in order."""
    select_list = sql[sql.index("SELECT") + len("SELECT"):sql.index("\nFROM")]
    return re.findall(r"\bAS\s+(\w+)\s*(?:,|$)", select_list, re.MULTILINE)


# ── The SQL ─────────────────────────────────────────────────────────────


def test_sql_selects_the_spec_columns_in_order():
    assert select_aliases(SQL_CODE) == COLUMN_NAMES


def test_sql_buckets_match_the_spec():
    bucket_case = SQL_CODE[SQL_CODE.index("CASE"):SQL_CODE.index("AS bucket")]
    labels = re.findall(r"(?:THEN|ELSE)\s+'([^']+)'", bucket_case)
    assert len(labels) == len(set(labels))
    assert set(labels) == set(spec.BUCKETS)
    assert set(spec.BUCKET_COLORS) == set(spec.BUCKETS)


def test_sql_counts_successes_per_utc_month_without_json():
    # A missing or empty status counts as a success; only 'failure' is dropped.
    assert "WHERE coalesce(nullif(status, ''), 'success') <> 'failure'" in SQL_CODE
    assert "date_trunc('month', starttime) AS month" in SQL_CODE
    assert "count(*) AS sgr" in SQL_CODE
    assert "FROM postgres_litellm.public.litellm_spendlogs" in SQL_CODE
    # Trino timed out on JSON functions at this volume.
    assert not re.search(r"json_|\bmetadata\b|proxy_server_request|\bmessages\b|\bresponse\b", SQL_CODE, re.I)


@pytest.mark.parametrize(
    "path, bucket",
    [
        ("http://classifier:8000/jobs/0a1b2c3d4e5f", "pass-through: job status"),
        ("http://classifier:8000/jobs/0a1b2c3d4e5f?wait=30", "pass-through: job status"),
        ("http://classifier:8000/jobs/0a1b2c3d4e5f/progress?since=4", "pass-through: job status"),
        ("http://quality-checker:8000/jobs/6f1c2a7e-0b9d-4c2e-9a51-3d2f8e1b7c40", "pass-through: job status"),
        ("http://classifier:8000/jobs/0a1b2c3d4e5f/artifacts/p0.base.jpg", "pass-through: other"),
        ("http://classifier:8000/jobs/0a1b2c3d4e5f/artifacts", "pass-through: other"),
        ("http://sandbox-runner:8000/jobs/0a1b2c3d4e5f/logs?tail=5", "pass-through: other"),
        ("http://classifier:8000/jobs", "pass-through: other"),
        ("http://classifier:8000/assess", "pass-through: other"),
    ],
)
def test_job_status_pattern(path, bucket):
    # Trino's regexp_like is a search, like re.search, for this pattern.
    pattern = re.search(r"regexp_like\(api_base, '([^']+)'\)", SQL_CODE).group(1)
    got = "pass-through: job status" if re.search(pattern, path) else "pass-through: other"
    assert got == bucket


def test_sql_is_safe_as_a_superset_virtual_dataset():
    # One statement (Superset strips a trailing ';' but refuses two), no Jinja
    # (dataset SQL is a template), no '%' (escaped as '%%' by a pyformat
    # dialect), no ':name' (a bind parameter inside SQLAlchemy text()).
    assert ";" not in SQL_TEXT
    assert not re.search(r"\{[{%#]", SQL_TEXT)
    assert "%" not in SQL_TEXT
    assert not re.search(r"(?<![:\w\\]):\w", SQL_TEXT)
    assert SQL_CODE.startswith("SELECT")
    assert SQL_CODE.count("SELECT") == 1
    assert "ORDER BY" not in SQL_CODE


def test_spec_types_and_time_column():
    assert spec.MAIN_DTTM_COL == "month"
    assert [name for name, _, is_dttm in spec.COLUMNS if is_dttm] == ["month"]
    assert dict((n, t) for n, t, _ in spec.COLUMNS)["sgr"] == "BIGINT"


# ── Charts and dashboard ────────────────────────────────────────────────


def referenced_columns(params: dict) -> set[str]:
    cols = set(params.get("groupby", []))
    cols |= set(params.get("groupbyRows", [])) | set(params.get("groupbyColumns", []))
    if "x_axis" in params:
        cols.add(params["x_axis"])
    cols |= set(params.get("temporal_columns_lookup", {}))
    for metric in params.get("metrics", []):
        if metric.get("expressionType") == "SIMPLE":
            cols.add(metric["column"]["column_name"])
    for flt in params.get("adhoc_filters", []):
        if flt.get("expressionType") == "SIMPLE":
            cols.add(flt["subject"])
    return cols


@pytest.mark.parametrize("chart", spec.CHARTS, ids=lambda c: c["slice_name"])
def test_chart_params_only_reference_sql_columns(chart):
    params = chart["params"]
    assert params["viz_type"] == chart["viz_type"]
    cols = referenced_columns(params)
    assert cols, "a chart that references no column checks nothing"
    assert cols <= set(COLUMN_NAMES)
    assert params["metrics"] == [spec.SGR_METRIC]
    assert spec.SGR_METRIC["aggregate"] == "SUM" and spec.SGR_METRIC["column"]["column_name"] == "sgr"
    json.dumps(params)  # stored as JSON


def test_charts_are_the_planned_pair():
    by_type = {c["viz_type"]: c for c in spec.CHARTS}
    bar = by_type["echarts_timeseries_bar"]["params"]
    assert (bar["x_axis"], bar["time_grain_sqla"], bar["groupby"], bar["stack"]) == ("month", "P1M", ["bucket"], "Stack")
    pivot = by_type["pivot_table_v2"]["params"]
    assert (pivot["groupbyRows"], pivot["groupbyColumns"]) == (["month"], ["bucket"])
    # The pivot only applies its time grain to columns in this lookup.
    assert pivot["temporal_columns_lookup"] == {"month": True}
    assert {c["slice_name"] for c in spec.CHARTS} == {"LiteLLM SGRs per month", "LiteLLM SGRs by month and bucket"}


def test_uuids_are_fixed_and_distinct():
    ids = [spec.DATASET_UUID, spec.DASHBOARD["uuid"], *(c["uuid"] for c in spec.CHARTS)]
    assert len({str(uuid.UUID(i)) for i in ids}) == len(ids)


def test_dashboard_position_is_a_consistent_tree():
    position = spec.DASHBOARD["position"]
    components = {k: v for k, v in position.items() if isinstance(v, dict)}
    assert position["DASHBOARD_VERSION_KEY"] == "v2"
    assert components["ROOT_ID"]["children"] == ["GRID_ID"]
    for key, comp in components.items():
        assert comp["id"] == key
        for child in comp.get("children", []):
            assert components[child]["parents"] == [*comp.get("parents", []), key]
        if comp["type"] == "ROW":
            assert sum(components[c]["meta"]["width"] for c in comp["children"]) <= 12
    reachable, todo = set(), ["ROOT_ID"]
    while todo:
        reachable.add(node := todo.pop())
        todo += components[node].get("children", [])
    assert reachable == set(components) - {"HEADER_ID"}
    chart_uuids = [c["meta"]["uuid"] for c in components.values() if c["type"] == "CHART"]
    assert sorted(chart_uuids) == sorted(c["uuid"] for c in spec.CHARTS)
    json.dumps(spec.DASHBOARD)


# ── Image wiring ────────────────────────────────────────────────────────


def test_dockerfile_copies_the_provisioner_and_runs_bootstrap_first():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^COPY provision_sgr\.py sgr_spec\.py\s+/app/provision/$", text, re.M)
    assert re.search(r"^COPY assets/\s+/app/provision/assets/$", text, re.M)
    assert re.search(r"^COPY bootstrap\.sh\s+/app/docker-entrypoint-init\.d/bootstrap\.sh$", text, re.M)
    # The base image's CMD only starts gunicorn; ours runs bootstrap.sh
    # first and starts Superset even if it fails.
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", text, re.M).group(1))
    script = cmd[-1]
    assert script.startswith("/app/docker-entrypoint-init.d/bootstrap.sh || ")
    assert script.endswith("; exec /usr/bin/run-server.sh")
    assert (SUPERSET_DIR / spec.SQL_FILE).parent.name == "assets"


def test_bootstrap_runs_the_provisioner_after_registration_without_failing():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    register = text.index("registering Trino connection")
    call = re.search(r"^python /app/provision/provision_sgr\.py \\\n\s+\|\| echo ", text, re.M)
    assert call, "provision_sgr.py must run with an `|| echo` fallback (set -e is on)"
    assert call.start() > register
    # The registration step must not abort the script before the provisioner.
    assert re.search(r"^python <<'PY' \|\| echo ", text, re.M)
    # Superset's models need an app context outside the CLI.
    assert "create_app().app_context()" in text


def test_provisioner_exits_zero_without_superset(capsys):
    if importlib.util.find_spec("superset") is not None:
        pytest.skip("superset is importable here; this checks the no-Superset path")
    module_spec = importlib.util.spec_from_file_location("provision_sgr", SUPERSET_DIR / "provision_sgr.py")
    provision = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(provision)
    assert provision.main() == 0
    assert "setup failed, nothing provisioned" in capsys.readouterr().out
