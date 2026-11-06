"""What provision_sgr.py builds in Superset: the ``litellm_sgr_monthly``
dataset, its two charts and the "LiteLLM SGRs" dashboard.

Pure data (stdlib only), so unit-tests/trino can check it against the SQL
without a Superset install. The chart params are Superset 4.0.2 form data,
keyed the way that release's control panels name their controls
(plugin-chart-echarts Timeseries/Regular/Bar, plugin-chart-pivot-table; the
shared ``x_axis`` / ``time_grain_sqla`` / ``adhoc_filters`` shape is the one
``superset/migrations/shared/migrate_viz`` writes). Keys left out take the
plugin defaults.

The UUIDs are fixed: provision_sgr.py finds its objects by them, creates a
chart or the dashboard only when no object carries its UUID, and never
updates one after that. The dataset is the exception: its SQL and columns
are re-synced on every boot. ai/trino/TRINO.md § Counting LiteLLM SGRs.
"""

from __future__ import annotations

# Registered by bootstrap.sh from TRINO_URL.
DATABASE_NAME = "trino"

DATASET_NAME = "litellm_sgr_monthly"
DATASET_UUID = "e0bd0a94-ec75-492a-bfea-5e57646ea6b7"
DATASET_DESCRIPTION = (
    "LiteLLM Successful Gateway Requests per UTC month, by bucket. SQL: "
    "ai/trino/superset/assets/litellm_sgr_monthly.sql, re-synced from the "
    "repo on every Superset start -- edits made here are overwritten."
)
# Relative to this file, in the repo and in the image (/app/provision/).
SQL_FILE = "assets/litellm_sgr_monthly.sql"

# The SQL's output columns as (name, type, is_dttm), in SELECT order. The
# types are what Superset's Trino engine spec records for them when it syncs
# columns from the source (the Trino type name, upper-cased), so declaring
# them here gives the same columns without running the query at boot.
COLUMNS = (
    ("month", "TIMESTAMP(3)", True),
    ("bucket", "VARCHAR", False),
    ("call_type", "VARCHAR", False),
    ("target", "VARCHAR", False),
    ("sgr", "BIGINT", False),
)
MAIN_DTTM_COL = "month"

# Every label the SQL's bucket CASE can produce.
BUCKETS = (
    "model",
    "mcp",
    "a2a",
    "pass-through: job status",
    "pass-through: other",
    "other",
)
# One fixed color per bucket (dashboard label_colors), so a bucket keeps its
# color whichever buckets a month has. Categorical slots 1-6 of the dataviz
# reference palette, in BUCKETS order; adjacent pairs pass its CVD and
# normal-vision checks on a white surface. The pivot table carries the
# numbers, so color never has to.
BUCKET_COLORS = {
    "model": "#2a78d6",
    "mcp": "#eb6834",
    "a2a": "#1baf7a",
    "pass-through: job status": "#eda100",
    "pass-through: other": "#e87ba4",
    "other": "#008300",
}

# SUM(sgr), shown as "SGRs". An adhoc metric, so the charts do not depend on
# a saved metric someone could delete.
SGR_METRIC = {
    "expressionType": "SIMPLE",
    "column": {"column_name": "sgr", "type": "BIGINT"},
    "aggregate": "SUM",
    "sqlExpression": None,
    "hasCustomLabel": True,
    "label": "SGRs",
    "optionName": "metric_sgr_sum",
}

# The 4.0 form of "no time filter": a TEMPORAL_RANGE adhoc filter on the
# time column with the "No filter" range.
ALL_MONTHS = {
    "clause": "WHERE",
    "subject": "month",
    "operator": "TEMPORAL_RANGE",
    "comparator": "No filter",
    "expressionType": "SIMPLE",
}

PER_MONTH_CHART_UUID = "1a684fef-7c3c-4fe7-9ae9-de30c324f0e1"
BY_BUCKET_CHART_UUID = "ab162b49-5dc4-46ad-87ec-3098f3329a00"

CHARTS = (
    {
        "uuid": PER_MONTH_CHART_UUID,
        "slice_name": "LiteLLM SGRs per month",
        "viz_type": "echarts_timeseries_bar",
        "description": (
            "Successful Gateway Requests per UTC month, stacked by bucket; "
            "the label on each bar is the month's total."
        ),
        "params": {
            "viz_type": "echarts_timeseries_bar",
            "x_axis": "month",
            "time_grain_sqla": "P1M",
            "metrics": [SGR_METRIC],
            "groupby": ["bucket"],
            "adhoc_filters": [ALL_MONTHS],
            "row_limit": 10000,
            "order_desc": True,
            "truncate_metric": True,
            "show_empty_columns": True,
            "comparison_type": "values",
            "annotation_layers": [],
            "orientation": "vertical",
            "stack": "Stack",
            "show_value": True,
            "only_total": True,
            "sort_series_type": "sum",
            "sort_series_ascending": False,
            "color_scheme": "supersetColors",
            "show_legend": True,
            "legendType": "scroll",
            "legendOrientation": "top",
            "x_axis_time_format": "%Y-%m",
            "y_axis_format": "SMART_NUMBER",
            "y_axis_title": "SGRs",
            "y_axis_title_margin": 30,
            "y_axis_title_position": "Left",
            "truncateYAxis": False,
            "y_axis_bounds": [None, None],
            "rich_tooltip": True,
            "tooltipTimeFormat": "%Y-%m",
            "zoomable": False,
            "extra_form_data": {},
        },
    },
    {
        "uuid": BY_BUCKET_CHART_UUID,
        "slice_name": "LiteLLM SGRs by month and bucket",
        "viz_type": "pivot_table_v2",
        "description": (
            "Exact Successful Gateway Requests per UTC month (rows) and "
            "bucket (columns), with totals."
        ),
        "params": {
            "viz_type": "pivot_table_v2",
            "groupbyRows": ["month"],
            "groupbyColumns": ["bucket"],
            # The pivot applies a time grain only to columns listed here.
            "time_grain_sqla": "P1M",
            "temporal_columns_lookup": {"month": True},
            "metrics": [SGR_METRIC],
            "metricsLayout": "COLUMNS",
            "adhoc_filters": [ALL_MONTHS],
            "row_limit": 10000,
            "order_desc": True,
            "aggregateFunction": "Sum",
            "rowTotals": True,
            "colTotals": True,
            "rowSubTotals": False,
            "colSubTotals": False,
            "transposePivot": False,
            "combineMetric": False,
            "valueFormat": ",d",
            "date_format": "%Y-%m",
            "rowOrder": "key_a_to_z",
            "colOrder": "key_a_to_z",
            "conditional_formatting": [],
            "extra_form_data": {},
        },
    },
)

_NOTE = """\
### What counts

One **SGR** is one call LiteLLM logged with a success status (a missing \
status counts), once per request. Months are **UTC**.

- **pass-through: job status** -- classifier (and other) `GET /jobs/{id}` \
and `/progress` polls, plus the one `DELETE /jobs/{id}` per job: the method \
is not logged.
- **pass-through: other** -- every other pass-through call, artifact \
downloads included.
- **model** / **mcp** / **a2a** -- LLM, MCP and agent-to-agent calls.

Definition and SQL: `ai/trino/TRINO.md` § Counting LiteLLM SGRs.
"""

DASHBOARD = {
    "uuid": "bf2d5a38-0016-4d59-9285-bb8971bf54d5",
    "dashboard_title": "LiteLLM SGRs",
    # Dropped (the dashboard gets no slug) if another dashboard holds it.
    "slug": "litellm-sgrs",
    # Superset 4.0 position_json (v2): ROOT > GRID > rows > components, widths
    # out of 12 columns, heights in 8 px units. provision_sgr.py fills each
    # CHART's meta.chartId and meta.sliceName from the chart with meta.uuid.
    "position": {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {
            "type": "GRID",
            "id": "GRID_ID",
            "children": ["ROW-sgr-trend", "ROW-sgr-table"],
            "parents": ["ROOT_ID"],
        },
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": "LiteLLM SGRs"}},
        "ROW-sgr-trend": {
            "type": "ROW",
            "id": "ROW-sgr-trend",
            "children": ["CHART-sgr-per-month", "MARKDOWN-sgr-note"],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        },
        "CHART-sgr-per-month": {
            "type": "CHART",
            "id": "CHART-sgr-per-month",
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID", "ROW-sgr-trend"],
            "meta": {"uuid": PER_MONTH_CHART_UUID, "width": 8, "height": 60},
        },
        "MARKDOWN-sgr-note": {
            "type": "MARKDOWN",
            "id": "MARKDOWN-sgr-note",
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID", "ROW-sgr-trend"],
            "meta": {"code": _NOTE, "width": 4, "height": 60},
        },
        "ROW-sgr-table": {
            "type": "ROW",
            "id": "ROW-sgr-table",
            "children": ["CHART-sgr-by-bucket"],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        },
        "CHART-sgr-by-bucket": {
            "type": "CHART",
            "id": "CHART-sgr-by-bucket",
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID", "ROW-sgr-table"],
            "meta": {"uuid": BY_BUCKET_CHART_UUID, "width": 12, "height": 60},
        },
    },
    # Keys from 4.0.2's DashboardJSONMetadataSchema.
    "metadata": {
        "color_scheme": "supersetColors",
        "label_colors": BUCKET_COLORS,
        "shared_label_colors": {},
        "color_scheme_domain": [],
        "refresh_frequency": 0,
        "timed_refresh_immune_slices": [],
        "expanded_slices": {},
        "default_filters": "{}",
        "chart_configuration": {},
        "native_filter_configuration": [],
        "cross_filters_enabled": False,
    },
}
