"""Compile a dashboard spec (dict) into a Superset import bundle (.zip).

This is the deterministic core of AI Analyst: the LLM only ever writes a
spec; this compiler emits the bundle, so the malformed-bundle class of
errors is structurally impossible.

Design (deliberately thin, ported from the proven standalone build.py):
- Chart params are literal dicts copied verbatim from a real UI export of
  each viz type; only fields that vary are substituted.
- Chart and dataset uuids are CONTENT-VERSIONED (uuid5 of slug + content
  hash): Superset's dashboard import never overwrites existing charts or
  datasets, so a changed definition must arrive as a NEW object; unchanged
  objects keep their uuid. Superseded versions orphan and can be bulk-deleted.
- query_context is always null; Superset regenerates it on first render.
- The database yaml is embedded verbatim (import binds by database uuid; the
  masked password is never needed when the connection already exists).
- Numeric chartIds in the position tree / metadata are placeholders (1..N);
  import remaps them via the uuid in each CHART node.
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

DEFAULT_NAMESPACE = "superset.ai-analyst"


class SpecError(ValueError):
    """The spec is invalid. The message is written for the agent to act on."""


@dataclass
class DatabaseRef:
    """The existing Superset database the dashboard's datasets bind to."""

    name: str  # database_name, used for bundle paths (datasets/<name>/...)
    uuid: str  # binds datasets on import; connection must exist or import prompts
    yaml_text: str  # verbatim databases/<name>.yaml content to embed


class Compiler:
    def __init__(
        self,
        database: DatabaseRef,
        namespace: str = DEFAULT_NAMESPACE,
        default_catalog: str | None = None,
    ):
        self.db = database
        self.ns = uuid.uuid5(uuid.NAMESPACE_DNS, namespace)
        self.default_catalog = default_catalog

    # ------------------------------------------------------------- identity

    def u5(self, kind: str, slug: str) -> str:
        return str(uuid.uuid5(self.ns, f"{kind}:{slug}"))

    @staticmethod
    def content_hash(obj) -> str:
        return hashlib.sha1(
            json.dumps(obj, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]

    def nid(self, kind: str, slug: str) -> str:
        """Deterministic position-tree node id, e.g. CHART-3f9a1c20b7d4."""
        return f"{kind}-{uuid.uuid5(self.ns, f'node:{kind}:{slug}').hex[:12]}"

    @staticmethod
    def ydump(obj) -> str:
        return yaml.dump(
            obj, sort_keys=False, allow_unicode=True, default_flow_style=False
        )

    # ------------------------------------------------------------- datasets

    def build_dataset(self, name: str, ds: dict) -> dict:
        """Template copied from a real UI dataset export."""
        columns = []
        for c in ds["columns"]:
            columns.append({
                "column_name": c["name"],
                "verbose_name": c.get("verbose_name"),
                "is_dttm": bool(c.get("is_dttm", False)),
                "is_active": True,
                "type": c["type"],
                "advanced_data_type": None,
                "groupby": True,
                "filterable": True,
                "expression": None,
                "description": None,
                "python_date_format": None,
                "datetime_format": None,
                "extra": {},
            })
        return {
            "table_name": name,
            "main_dttm_col": ds.get("main_dttm_col"),
            "currency_code_column": None,
            "description": ds.get("description"),
            "default_endpoint": None,
            "offset": 0,
            "cache_timeout": None,
            "catalog": ds.get("catalog", self.default_catalog),
            "schema": ds.get("schema"),
            "sql": ds["sql"],
            "params": None,
            "template_params": None,
            "filter_select_enabled": True,
            "fetch_values_predicate": None,
            "extra": None,
            "normalize_columns": False,
            "always_filter_main_dttm": False,
            "folders": None,
            # Content-versioned: dashboard import never overwrites an existing
            # dataset, so a changed definition must arrive as a NEW uuid.
            "uuid": self.u5("dataset", f"{name}:{self.content_hash(ds)}"),
            "metrics": [{
                "metric_name": "count",
                "verbose_name": "COUNT(*)",
                "metric_type": "count",
                "expression": "COUNT(*)",
                "description": None,
                "d3format": None,
                "currency": None,
                "extra": {"warning_markdown": ""},
                "warning_text": None,
            }],
            "columns": columns,
            "version": "1.0.0",
            "database_uuid": self.db.uuid,
        }

    # --------------------------------------------------------------- charts

    # Boilerplate present verbatim at the top of every reference chart's params.
    @staticmethod
    def _matrixify() -> dict:
        return {
            "matrixify_enable": False,
            "matrixify_mode_columns": "disabled",
            "matrixify_dimension_selection_mode_columns": "members",
            "matrixify_dimension_columns": {"dimension": "", "values": []},
            "matrixify_topn_value_columns": 10,
            "matrixify_all_sort_by_columns": "a_to_z",
            "matrixify_topn_order_columns": True,
            "matrixify_show_column_headers": True,
            "matrixify_fit_columns_dynamically": True,
            "matrixify_mode_rows": "disabled",
            "matrixify_dimension_selection_mode_rows": "members",
            "matrixify_dimension_rows": {"dimension": "", "values": []},
            "matrixify_topn_value_rows": 10,
            "matrixify_all_sort_by_rows": "a_to_z",
            "matrixify_topn_order_rows": True,
            "matrixify_show_row_labels": True,
            "matrixify_row_height": 300,
            "matrixify_charts_per_row": 3,
            "matrixify_cell_title_template": "",
        }

    def _metric(self, spec: dict, chart_slug: str, dataset: dict) -> dict:
        """Adhoc metric. SIMPLE (column + aggregate) or SQL ({sql:..., label:...}).

        Instance-local column id/uuid are omitted: Superset resolves by
        column_name (import-verified).
        """
        label = spec.get("label")
        if spec.get("sql"):
            sql_expr = spec["sql"]
            opt = uuid.uuid5(self.ns, f"metric:{chart_slug}:sql:{sql_expr}").hex[:12]
            return {
                "expressionType": "SQL",
                "column": None,
                "aggregate": None,
                "sqlExpression": sql_expr,
                "datasourceWarning": False,
                "hasCustomLabel": label is not None,
                "label": label or sql_expr,
                "optionName": f"metric_{opt}",
            }
        col_name = spec["column"]
        aggregate = spec.get("aggregate", "SUM")
        col_def = next((c for c in dataset["columns"] if c["name"] == col_name), {})
        return {
            "expressionType": "SIMPLE",
            "column": {
                "advanced_data_type": None,
                "certification_details": None,
                "certified_by": None,
                "column_name": col_name,
                "description": None,
                "expression": None,
                "filterable": True,
                "groupby": True,
                "is_certified": False,
                "is_dttm": bool(col_def.get("is_dttm", False)),
                "python_date_format": None,
                "type": col_def.get("type", "BIGINT"),
                "type_generic": 2 if col_def.get("is_dttm") else 0,
                "verbose_name": None,
                "warning_markdown": None,
            },
            "aggregate": aggregate,
            "sqlExpression": None,
            "datasourceWarning": False,
            "hasCustomLabel": label is not None,
            "label": label or f"{aggregate}({col_name})",
            "optionName": (
                "metric_"
                + uuid.uuid5(
                    self.ns, f"metric:{chart_slug}:{aggregate}:{col_name}"
                ).hex[:12]
            ),
        }

    def _metrics(self, c: dict, chart_slug: str, dataset: dict) -> list:
        """Spec may give a single `metric:` or a `metrics:` list."""
        specs = c.get("metrics") or [c["metric"]]
        return [self._metric(m, chart_slug, dataset) for m in specs]

    def _adhoc_filters(self, chart: dict, chart_slug: str, dataset: dict) -> list:
        """Temporal range on main_dttm_col + optional categorical filters."""
        time_col = chart.get("time_column") or dataset.get("main_dttm_col")
        out = [{
            "expressionType": "SIMPLE",
            "subject": time_col,
            "operator": "TEMPORAL_RANGE",
            "comparator": chart.get("time_range", "No filter"),
            "clause": "WHERE",
            "sqlExpression": None,
            "isExtra": False,
            "isNew": False,
            "datasourceWarning": False,
            "filterOptionName": (
                "filter_" + uuid.uuid5(self.ns, f"filter:{chart_slug}:time").hex[:12]
            ),
        }]
        for i, f in enumerate(chart.get("filters", [])):
            op = f["op"].upper()
            flt = {
                "expressionType": "SIMPLE",
                "subject": f["column"],
                "operator": op,
                "clause": "WHERE",
                "sqlExpression": None,
                "isExtra": False,
                "isNew": False,
                "datasourceWarning": False,
                "filterOptionName": (
                    "filter_" + uuid.uuid5(self.ns, f"filter:{chart_slug}:{i}").hex[:12]
                ),
            }
            if op in ("IN", "NOT IN", "IS NULL", "IS NOT NULL"):
                flt["operatorId"] = op.replace(" ", "_")
            if op not in ("IS NULL", "IS NOT NULL"):
                flt["comparator"] = f.get("values", f.get("value"))
            out.append(flt)
        return out

    def params_line(self, c, slug, ds):
        # Verbatim from a UI export of echarts_timeseries_line.
        p = self._matrixify()
        p.update({
            "x_axis": c.get("x_axis") or ds.get("main_dttm_col"),
            "time_grain_sqla": c.get("time_grain", "P1D"),
            "xAxisForceCategorical": False,
            "x_axis_sort_asc": True,
            "metrics": self._metrics(c, slug, ds),
            "groupby": c.get("groupby", []),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "group_others_when_limit_reached": False,
            "order_desc": True,
            "row_limit": c.get("row_limit", 10000),
            "truncate_metric": True,
            "show_empty_columns": True,
            "comparison_type": "values",
            "annotation_layers": [],
            "forecastEnabled": False,
            "forecastPeriods": 10,
            "forecastInterval": 0.8,
            "x_axis_title": "",
            "x_axis_title_margin": 0,
            "y_axis_title": "",
            "y_axis_title_margin": 0,
            "y_axis_title_position": "Left",
            "sort_series_type": "sum",
            "sort_series_ascending": False,
            "color_scheme": c.get("color_scheme", "supersetColors"),
            "time_shift_color": True,
            "show_value": False,
            "only_total": True,
            "percentage_threshold": 0,
            "area": bool(c.get("area", False)),
            "opacity": 0.2,
            "markerEnabled": False,
            "markerSize": 6,
            "zoomable": True,
            "minorTicks": False,
            "show_legend": True,
            "legendType": "scroll",
            "legendOrientation": "top",
            "x_axis_time_format": c.get("x_axis_time_format", "smart_date"),
            "x_axis_number_format": "~g",
            "xAxisLabelRotation": c.get("x_axis_label_rotation", 0),
            "xAxisLabelInterval": "auto",
            "force_max_interval": False,
            "rich_tooltip": True,
            "showTooltipTotal": False,
            "showTooltipPercentage": False,
            "tooltipSortByMetric": False,
            "tooltipTimeFormat": "smart_date",
            "y_axis_format": "SMART_NUMBER",
            "logAxis": False,
            "minorSplitLine": False,
            "truncateXAxis": True,
            "truncateYAxis": False,
            "y_axis_bounds": [None, None],
            "echart_options": "",
        })
        if c.get("time_compare"):
            p["time_compare"] = c["time_compare"]
        return p

    def params_big_number(self, c, slug, ds):
        # Verbatim from a UI export of big_number.
        color = c.get("color", {"r": 0, "g": 122, "b": 135})
        p = self._matrixify()
        p.update({
            "x_axis": c.get("x_axis") or ds.get("main_dttm_col"),
            "time_grain_sqla": c.get("time_grain", "P1D"),
            "aggregation": c.get("aggregation", "LAST_VALUE"),
            "metric": self._metric(c["metric"], slug, ds),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "compare_lag": c.get("compare_lag", 1),
            "compare_suffix": c.get("compare_suffix", ""),
            "show_timestamp": True,
            "show_trend_line": True,
            "start_y_axis_at_zero": True,
            "color_picker": {"r": color["r"], "g": color["g"], "b": color["b"], "a": 1},
            "header_font_size": 0.4,
            "subheader_font_size": 0.125,
            "subtitle_font_size": 0.125,
            "show_metric_name": False,
            "show_x_axis": False,
            "show_y_axis": False,
            "y_axis_format": "SMART_NUMBER",
            "time_format": "smart_date",
            "force_timestamp_formatting": False,
            # Superset stores the literal string "None" here, not null.
            "rolling_type": "None",
        })
        return p

    def params_cal_heatmap(self, c, slug, ds):
        # Verbatim from a UI export of cal_heatmap.
        p = self._matrixify()
        p.update({
            "granularity_sqla": c.get("time_column") or ds.get("main_dttm_col"),
            "time_range": c.get("time_range", "No filter"),
            "domain_granularity": c.get("domain", "month"),
            "subdomain_granularity": c.get("subdomain", "day"),
            "metrics": [self._metric(c["metric"], slug, ds)],
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "linear_color_scheme": c.get("linear_color_scheme", "dark_blue"),
            "cell_size": 14,
            "cell_padding": 3,
            "cell_radius": 5,
            "steps": 10,
            "y_axis_format": "SMART_NUMBER",
            "x_axis_time_format": "smart_date",
            "show_legend": True,
            "show_values": False,
            "show_metric_name": False,
        })
        return p

    def params_pie(self, c, slug, ds):
        # Verbatim from a UI export of pie.
        groupby = c["groupby"]
        p = self._matrixify()
        p.update({
            "groupby": groupby if isinstance(groupby, list) else [groupby],
            "metric": self._metric(c["metric"], slug, ds),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "row_limit": c.get("row_limit", 100),
            "sort_by_metric": True,
            "color_scheme": c.get("color_scheme", "supersetColors"),
            "show_labels_threshold": 5,
            "threshold_for_other": 0,
            "show_legend": True,
            "legendType": "scroll",
            "legendOrientation": "top",
            "label_type": "key",
            "number_format": "SMART_NUMBER",
            "date_format": "smart_date",
            "show_labels": True,
            "labels_outside": True,
            "label_line": True,
            "show_total": False,
            "outerRadius": 70,
            "donut": bool(c.get("donut", False)),
            "innerRadius": 30,
        })
        return p

    def params_treemap(self, c, slug, ds):
        # Verbatim from a UI export of treemap_v2.
        p = self._matrixify()
        p["matrixify_charts_per_row"] = 4
        p.update({
            "groupby": c["groupby"],
            "metric": self._metric(c["metric"], slug, ds),
            "row_limit": c.get("row_limit", 10000),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "color_scheme": c.get("color_scheme", "supersetColors"),
            "show_labels": True,
            "show_upper_labels": True,
            "label_type": "Key",
            "number_format": "SMART_NUMBER",
            "date_format": "smart_date",
        })
        return p

    def params_pivot(self, c, slug, ds):
        # Verbatim from a UI export of pivot_table_v2.
        dttm_cols = {col["name"] for col in ds["columns"] if col.get("is_dttm")}
        group_cols = c.get("columns", [])
        p = self._matrixify()
        p["matrixify_charts_per_row"] = 4
        p.update({
            "groupbyColumns": group_cols,
            "groupbyRows": c.get("rows", []),
            "temporal_columns_lookup": {
                col: True for col in group_cols if col in dttm_cols
            },
            "metrics": self._metrics(c, slug, ds),
            "metricsLayout": "COLUMNS",
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "row_limit": c.get("row_limit", 10000),
            "order_desc": True,
            "aggregateFunction": "Sum",
            "rowTotals": bool(c.get("row_totals", False)),
            "rowSubTotals": bool(c.get("row_subtotals", False)),
            "colTotals": bool(c.get("col_totals", False)),
            "colSubTotals": bool(c.get("col_subtotals", False)),
            "transposePivot": False,
            "combineMetric": False,
            "valueFormat": "SMART_NUMBER",
            "date_format": "smart_date",
            "rowOrder": "key_a_to_z",
            "colOrder": "key_a_to_z",
            "rowSubtotalPosition": False,
            "colSubtotalPosition": False,
            "allow_render_html": True,
        })
        return p

    def params_bar(self, c, slug, ds):
        # synthesized from the ECharts timeseries plugin defaults (same family
        # as params_line) — categorical bars sorted by metric.
        # synthesized — render-verified 2026-08-17
        metrics = self._metrics(c, slug, ds)
        sort_axis = bool(c.get("sort_axis", False))
        p = self._matrixify()
        p.update({
            "x_axis": c["x_axis"],
            "xAxisForceCategorical": True,
            "x_axis_sort": c["x_axis"] if sort_axis else metrics[0]["label"],
            "x_axis_sort_asc": True if sort_axis else bool(c.get("sort_ascending", False)),
            "metrics": metrics,
            "groupby": c.get("groupby", []),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "order_desc": True,
            "row_limit": c.get("row_limit", 100),
            "truncate_metric": True,
            "show_empty_columns": True,
            "comparison_type": "values",
            "annotation_layers": [],
            "forecastEnabled": False,
            "forecastPeriods": 10,
            "forecastInterval": 0.8,
            "orientation": c.get("orientation", "vertical"),
            "x_axis_title": "",
            "x_axis_title_margin": 0,
            "y_axis_title": "",
            "y_axis_title_margin": 0,
            "y_axis_title_position": "Left",
            "sort_series_type": "sum",
            "sort_series_ascending": False,
            "color_scheme": c.get("color_scheme", "supersetColors"),
            "show_value": bool(c.get("show_value", True)),
            "only_total": True,
            "percentage_threshold": 0,
            "show_legend": False,
            "legendType": "scroll",
            "legendOrientation": "top",
            "x_axis_time_format": "smart_date",
            "xAxisLabelRotation": c.get("x_axis_label_rotation", 45),
            "xAxisLabelInterval": "auto",
            "rich_tooltip": True,
            "showTooltipTotal": False,
            "showTooltipPercentage": False,
            "tooltipSortByMetric": False,
            "tooltipTimeFormat": "smart_date",
            "y_axis_format": c.get("y_axis_format", "SMART_NUMBER"),
            "logAxis": False,
            "minorSplitLine": False,
            "truncateXAxis": True,
            "truncateYAxis": False,
            "y_axis_bounds": [None, None],
        })
        return p

    def params_big_number_total(self, c, slug, ds):
        # synthesized from the BigNumberTotal plugin defaults.
        # synthesized — render-verified 2026-08-17
        p = self._matrixify()
        p.update({
            "metric": self._metric(c["metric"], slug, ds),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "header_font_size": c.get("header_font_size", 0.4),
            "subheader": c.get("subtitle", ""),
            "subheader_font_size": 0.15,
            "subtitle_font_size": 0.125,
            "y_axis_format": "SMART_NUMBER",
            "time_format": c.get("time_format", "smart_date"),
            "force_timestamp_formatting": False,
        })
        if c.get("timestamp"):
            p["force_timestamp_formatting"] = True
            p["time_format"] = c.get("time_format", "%d.%m.%Y %H:%M")
        return p

    def params_area(self, c, slug, ds):
        # synthesized: same ECharts timeseries family as params_line, stacked.
        # synthesized — render-verified 2026-08-17
        p = self.params_line(c, slug, ds)
        p.update({
            "opacity": 0.7,
            "stack": "Stack",
            "show_extra_controls": True,
            "only_total": True,
            "markerEnabled": False,
        })
        return p

    def params_sunburst(self, c, slug, ds):
        # synthesized from the Sunburst (sunburst_v2) plugin defaults.
        # synthesized — render-verified 2026-08-17
        p = self._matrixify()
        p.update({
            "columns": c["groupby"],
            "metric": self._metric(c["metric"], slug, ds),
            "secondary_metric": None,
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "row_limit": c.get("row_limit", 10000),
            "sort_by_metric": True,
            "color_scheme": c.get("color_scheme", "supersetColors"),
            "show_labels": True,
            "show_labels_threshold": 5,
            "show_total": False,
            "label_type": "key",
            "number_format": "SMART_NUMBER",
            "date_format": "smart_date",
        })
        return p

    def params_histogram(self, c, slug, ds):
        # synthesized from the Histogram (histogram_v2) plugin defaults.
        # synthesized — render-verified 2026-08-17
        p = self._matrixify()
        p.update({
            "column": c["column"],
            "groupby": c.get("groupby", []),
            "bins": c.get("bins", 20),
            "normalize": False,
            "cumulative": False,
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "row_limit": c.get("row_limit", 10000),
            "color_scheme": c.get("color_scheme", "supersetColors"),
            "show_value": False,
            "show_legend": bool(c.get("groupby")),
            "x_axis_title": c.get("x_axis_title", ""),
            "y_axis_title": c.get("y_axis_title", ""),
        })
        return p

    def params_bubble(self, c, slug, ds):
        # synthesized from the Bubble Chart (bubble_v2) plugin defaults.
        # synthesized — render-verified 2026-08-17
        p = self._matrixify()
        p.update({
            "entity": c["entity"],
            "series": c.get("series"),
            "x": self._metric(c["x"], f"{slug}:x", ds),
            "y": self._metric(c["y"], f"{slug}:y", ds),
            "size": self._metric(c["size"], f"{slug}:size", ds),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "row_limit": c.get("row_limit", 100),
            "limit": c.get("row_limit", 100),
            "color_scheme": c.get("color_scheme", "supersetColors"),
            "show_legend": bool(c.get("series")),
            "legendType": "scroll",
            "legendOrientation": "top",
            "max_bubble_size": "25",
            "opacity": 0.6,
            "x_axis_title": c.get("x_axis_title", ""),
            "x_axis_title_margin": 30,
            "y_axis_title": c.get("y_axis_title", ""),
            "y_axis_title_margin": 30,
            "y_axis_title_position": "Left",
            "xAxisLabelRotation": 0,
            "xAxisFormat": "SMART_NUMBER",
            "yAxisFormat": "SMART_NUMBER",
            "tooltipSizeFormat": "SMART_NUMBER",
            "logXAxis": False,
            "logYAxis": False,
            "truncateXAxis": False,
            "truncateYAxis": False,
            "x_axis_bounds": [None, None],
            "y_axis_bounds": [None, None],
        })
        return p

    def params_word_cloud(self, c, slug, ds):
        # synthesized from the Word Cloud plugin defaults.
        # synthesized — render-verified 2026-08-17
        p = self._matrixify()
        p.update({
            "series": c["column"],
            "metric": self._metric(c["metric"], slug, ds),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "row_limit": c.get("row_limit", 100),
            "size_from": 10,
            "size_to": 70,
            "rotation": c.get("rotation", "square"),
            "color_scheme": c.get("color_scheme", "supersetColors"),
        })
        return p

    def params_heatmap(self, c, slug, ds):
        # synthesized from the Heatmap (heatmap_v2) plugin defaults.
        # synthesized — render-verified 2026-08-17
        p = self._matrixify()
        p.update({
            "x_axis": c["x_axis"],
            "groupby": c["groupby"],
            "metric": self._metric(c["metric"], slug, ds),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "row_limit": c.get("row_limit", 10000),
            "sort_x_axis": c.get("sort_x_axis", "alpha_asc"),
            "sort_y_axis": c.get("sort_y_axis", "alpha_asc"),
            "normalize_across": c.get("normalize_across", "heatmap"),
            "legend_type": "continuous",
            "linear_color_scheme": c.get("linear_color_scheme", "blue_white_yellow"),
            "xscale_interval": -1,
            "yscale_interval": -1,
            "left_margin": "auto",
            "bottom_margin": "auto",
            "value_bounds": [None, None],
            "y_axis_format": "SMART_NUMBER",
            "x_axis_time_format": "smart_date",
            "show_legend": True,
            "show_percentage": True,
            "show_values": bool(c.get("show_values", False)),
            "normalized": False,
        })
        return p

    def params_gauge(self, c, slug, ds):
        # synthesized from the Gauge Chart plugin defaults.
        # synthesized — render-verified 2026-08-17
        p = self._matrixify()
        p.update({
            "metric": self._metric(c["metric"], slug, ds),
            "adhoc_filters": self._adhoc_filters(c, slug, ds),
            "groupby": [],
            "row_limit": 10,
            "min_val": c.get("min", 0),
            "max_val": c.get("max", 100),
            "start_angle": 225,
            "end_angle": -45,
            "color_scheme": c.get("color_scheme", "supersetColors"),
            "font_size": 15,
            "number_format": c.get("number_format", "SMART_NUMBER"),
            "value_formatter": "{value}",
            "show_pointer": True,
            "animation": True,
            "show_axis_tick": False,
            "show_split_line": False,
            "split_number": 10,
            "show_progress": True,
            "overlap": True,
            "round_cap": False,
            "intervals": "",
            "interval_color_indices": "",
        })
        return p

    VIZ_TYPES = {
        "line": ("echarts_timeseries_line", "params_line"),
        "bar": ("echarts_timeseries_bar", "params_bar"),
        "area": ("echarts_area", "params_area"),
        "big_number_total": ("big_number_total", "params_big_number_total"),
        "sunburst": ("sunburst_v2", "params_sunburst"),
        "histogram": ("histogram_v2", "params_histogram"),
        "bubble": ("bubble_v2", "params_bubble"),
        "word_cloud": ("word_cloud", "params_word_cloud"),
        "gauge": ("gauge_chart", "params_gauge"),
        "heatmap": ("heatmap_v2", "params_heatmap"),
        "big_number": ("big_number", "params_big_number"),
        "cal_heatmap": ("cal_heatmap", "params_cal_heatmap"),
        "pie": ("pie", "params_pie"),
        "treemap": ("treemap_v2", "params_treemap"),
        "pivot": ("pivot_table_v2", "params_pivot"),
    }
    # Full viz_type names are accepted too.
    VIZ_TYPES.update({v[0]: v for v in list(VIZ_TYPES.values())})

    def build_chart(
        self, slug: str, c: dict, dash_slug: str, datasets: dict, ds_uuids: dict
    ) -> dict:
        ctype = c["type"]
        if ctype not in self.VIZ_TYPES:
            raise SpecError(
                f"chart '{slug}': unsupported type '{ctype}'. Supported: "
                + ", ".join(sorted(k for k in self.VIZ_TYPES if "_timeseries" not in k))
            )
        viz_type, params_fn = self.VIZ_TYPES[ctype]
        ds_name = c["dataset"]
        if ds_name not in datasets:
            raise SpecError(f"chart '{slug}': unknown dataset '{ds_name}'")
        params = {"viz_type": viz_type}
        params.update(getattr(self, params_fn)(c, slug, datasets[ds_name]))
        params["extra_form_data"] = {}
        params["dashboards"] = []
        dataset_uuid = ds_uuids[ds_name]
        return {
            "slice_name": c["title"],
            "description": c.get("description"),
            "certified_by": None,
            "certification_details": None,
            "viz_type": viz_type,
            "params": params,
            "query_context": None,
            "cache_timeout": None,
            # Content-versioned (same reason as datasets): a changed chart must
            # import as a new object; unchanged charts keep their uuid.
            "uuid": self.u5(
                "chart",
                f"{dash_slug}:{slug}:{self.content_hash([viz_type, params, dataset_uuid])}",
            ),
            "version": "1.0.0",
            "dataset_uuid": dataset_uuid,
        }

    # --------------------------------------------------------------- layout

    def build_position(self, spec: dict, charts: dict, chart_ids: dict):
        """Tabs/rows of [chart_slug, width, height] -> Superset v2 position
        tree. Node shapes copied from a real dashboard export."""
        pos = {
            "DASHBOARD_VERSION_KEY": "v2",
            "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
            "HEADER_ID": {
                "id": "HEADER_ID", "meta": {"text": spec["title"]}, "type": "HEADER"
            },
        }
        md_blocks = spec.get("markdown", {})
        grid_children = []

        def add_rows(rows, parents, scope_key):
            for i, row in enumerate(rows):
                if row == "divider":
                    did = self.nid("DIVIDER", f"{scope_key}:{i}")
                    pos[did] = {"children": [], "id": did, "meta": {},
                                "parents": list(parents), "type": "DIVIDER"}
                    yield did
                    continue
                rid = self.nid("ROW", f"{scope_key}:{i}")
                row_children = []
                captions = []  # (entry_key, width, caption text or "")
                for entry in row:
                    cslug, width, height = entry
                    if cslug in md_blocks:
                        mid = self.nid("MARKDOWN", cslug)
                        pos[mid] = {
                            "children": [],
                            "id": mid,
                            "meta": {"code": md_blocks[cslug],
                                     "height": height, "width": width},
                            "parents": list(parents) + [rid],
                            "type": "MARKDOWN",
                        }
                        row_children.append(mid)
                        captions.append((cslug, width, ""))
                        continue
                    if cslug not in charts:
                        raise SpecError(
                            f"layout references unknown chart or markdown block '{cslug}'"
                        )
                    cid = self.nid("CHART", cslug)
                    pos[cid] = {
                        "children": [],
                        "id": cid,
                        "meta": {
                            "chartId": chart_ids[cslug],
                            "height": height,
                            "sliceName": charts[cslug]["slice_name"],
                            "uuid": charts[cslug]["uuid"],
                            "width": width,
                        },
                        "parents": list(parents) + [rid],
                        "type": "CHART",
                    }
                    row_children.append(cid)
                    captions.append(
                        (cslug, width, charts[cslug].get("description") or "")
                    )
                pos[rid] = {"children": row_children, "id": rid,
                            "meta": {"background": "BACKGROUND_TRANSPARENT"},
                            "parents": list(parents), "type": "ROW"}
                yield rid
                # Superset builds that don't render chart descriptions on
                # dashboards get them as an aligned caption row of small
                # markdown blocks instead.
                if any(text for _, _, text in captions):
                    crid = self.nid("ROW", f"{scope_key}:{i}:cap")
                    cap_children = []
                    for key, width, text in captions:
                        mid = self.nid("MARKDOWN", f"cap:{scope_key}:{i}:{key}")
                        pos[mid] = {
                            "children": [],
                            "id": mid,
                            # empty markdown renders Superset's placeholder
                            # text, so blank fillers get &nbsp;; narrow blocks
                            # need more height to wrap without clipping
                            "meta": {"code": f"*{text}*" if text else "&nbsp;",
                                     "height": 8 if width >= 4 else 14,
                                     "width": width},
                            "parents": list(parents) + [crid],
                            "type": "MARKDOWN",
                        }
                        cap_children.append(mid)
                    pos[crid] = {"children": cap_children, "id": crid,
                                 "meta": {"background": "BACKGROUND_TRANSPARENT"},
                                 "parents": list(parents), "type": "ROW"}
                    yield crid

        layout = spec.get("layout", {})
        tab_node_ids = {}
        if layout.get("tabs"):
            tabs_id = self.nid("TABS", spec["slug"])
            tab_children = []
            for tab_name, rows in layout["tabs"].items():
                tab_id = self.nid("TAB", tab_name)
                tab_node_ids[tab_name] = tab_id
                children = list(add_rows(
                    rows, ["ROOT_ID", "GRID_ID", tabs_id, tab_id], f"tab:{tab_name}"
                ))
                pos[tab_id] = {
                    "children": children,
                    "id": tab_id,
                    "meta": {"defaultText": "Tab title",
                             "placeholder": "Tab title", "text": tab_name},
                    "parents": ["ROOT_ID", "GRID_ID", tabs_id],
                    "type": "TAB",
                }
                tab_children.append(tab_id)
            pos[tabs_id] = {"children": tab_children, "id": tabs_id, "meta": {},
                            "parents": ["ROOT_ID", "GRID_ID"], "type": "TABS"}
            grid_children.append(tabs_id)
        grid_children.extend(
            add_rows(layout.get("rows", []), ["ROOT_ID", "GRID_ID"], "grid")
        )

        pos["GRID_ID"] = {"children": grid_children, "id": "GRID_ID",
                          "parents": ["ROOT_ID"], "type": "GRID"}
        return pos, tab_node_ids

    def build_dashboard(self, spec: dict, charts: dict, ds_uuids: dict) -> dict:
        # Placeholder numeric chart ids: Superset's import remaps them to real
        # slice ids via the uuid in each position CHART node.
        chart_ids = {slug: i + 1 for i, slug in enumerate(charts)}
        position, tab_node_ids = self.build_position(spec, charts, chart_ids)

        # Show each chart's description under its title where the build
        # supports expanded_slices (ids are remapped on import like chartIds).
        expanded_slices = {
            str(chart_ids[slug]): True
            for slug, c in charts.items()
            if c.get("description")
        }

        all_ids = sorted(chart_ids.values())
        chart_configuration = {
            str(i): {"id": i,
                     "crossFilters": {"scope": "global",
                                      "chartsInScope": [j for j in all_ids if j != i]}}
            for i in all_ids
        }

        native_filters = []
        for f in spec.get("filters", []):
            fid = (
                "NATIVE_FILTER-"
                + uuid.uuid5(self.ns, f"nf:{spec['slug']}:{f['name']}").hex[:21]
            )
            f["_id"] = fid
        for f in spec.get("filters", []):
            scope_root = ["ROOT_ID"]
            entry_tabs = []
            if f.get("tab"):
                if f["tab"] not in tab_node_ids:
                    raise SpecError(f"filter '{f['name']}': unknown tab '{f['tab']}'")
                scope_root = [tab_node_ids[f["tab"]]]
                entry_tabs = [tab_node_ids[f["tab"]]]
            in_scope = (
                [chart_ids[s] for s in f["charts"]] if f.get("charts") else all_ids
            )
            cascade = []
            if f.get("cascade"):
                parent = next(
                    (g for g in spec["filters"] if g["name"] == f["cascade"]), None
                )
                if parent is None:
                    raise SpecError(
                        f"filter '{f['name']}': unknown cascade parent '{f['cascade']}'"
                    )
                cascade = [parent["_id"]]
            native_filters.append({
                "cascadeParentIds": cascade,
                "chartsInScope": in_scope,
                "controlValues": {
                    "creatable": True,
                    "defaultToFirstItem": False,
                    "enableEmptyFilter": False,
                    "inverseSelection": False,
                    "multiSelect": bool(f.get("multi", True)),
                    "searchAllOptions": False,
                },
                "defaultDataMask": {"extraFormData": {}, "filterState": {},
                                    "ownState": {}},
                "description": "",
                "filterType": "filter_select",
                "id": f["_id"],
                "name": f["name"],
                "scope": {"excluded": [], "rootPath": scope_root},
                "sortMetric": None,
                "tabsInScope": entry_tabs,
                "targets": [{"column": {"name": f["column"]},
                             "datasetUuid": ds_uuids[f["dataset"]]}],
                "type": "NATIVE_FILTER",
            })

        return {
            "dashboard_title": spec["title"],
            "description": spec.get("description"),
            "css": None,
            "slug": spec["slug"],
            "certified_by": None,
            "certification_details": None,
            "published": True,
            "uuid": self.u5("dashboard", spec["slug"]),
            "position": position,
            "metadata": {
                "chart_configuration": chart_configuration,
                "global_chart_configuration": {
                    "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
                    "chartsInScope": all_ids,
                },
                "native_filter_configuration": native_filters,
                "chart_customization_config": [],
                "color_scheme": "",
                "refresh_frequency": 0,
                "expanded_slices": expanded_slices,
                "label_colors": {},
                "timed_refresh_immune_slices": [],
                "cross_filters_enabled": True,
                "default_filters": "{}",
                "show_chart_timestamps": False,
                "shared_label_colors": [],
                "map_label_colors": {},
                "color_scheme_domain": [],
            },
            "theme_uuid": None,
            "version": "1.0.0",
        }

    # --------------------------------------------------------------- bundle

    @staticmethod
    def validate_refs(charts: dict, ds_uuids: dict, dashboard: dict):
        uuids = set(ds_uuids.values())
        for slug, c in charts.items():
            if c["dataset_uuid"] not in uuids:
                raise SpecError(f"chart '{slug}': dangling dataset_uuid")
        chart_uuids = {c["uuid"] for c in charts.values()}
        pos_uuids = {v["meta"]["uuid"] for v in dashboard["position"].values()
                     if isinstance(v, dict) and v.get("type") == "CHART"}
        if pos_uuids != chart_uuids:
            missing = [s for s, c in charts.items() if c["uuid"] not in pos_uuids]
            raise SpecError(f"charts missing from layout: {missing}")
        for f in dashboard["metadata"]["native_filter_configuration"]:
            for t in f["targets"]:
                if t["datasetUuid"] not in uuids:
                    raise SpecError(f"filter '{f['name']}': dangling datasetUuid")

    def compile(self, spec: dict, timestamp: str | None = None) -> tuple[str, bytes]:
        """spec dict -> (bundle_name, zip bytes). Raises SpecError on bad input."""
        for key in ("title", "slug", "datasets", "charts"):
            if not spec.get(key):
                raise SpecError(f"spec is missing required key '{key}'")
        slug = spec["slug"]

        datasets = {n: self.build_dataset(n, d) for n, d in spec["datasets"].items()}
        ds_uuids = {n: d["uuid"] for n, d in datasets.items()}
        charts = {s: self.build_chart(s, c, slug, spec["datasets"], ds_uuids)
                  for s, c in spec["charts"].items()}
        dashboard = self.build_dashboard(spec, charts, ds_uuids)
        self.validate_refs(charts, ds_uuids, dashboard)

        bundle = f"{slug}_bundle"
        files = {
            f"{bundle}/metadata.yaml": self.ydump({
                "version": "1.0.0",
                "type": "Dashboard",
                "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            }),
            f"{bundle}/databases/{self.db.name}.yaml": self.db.yaml_text,
            f"{bundle}/dashboards/{slug}.yaml": self.ydump(dashboard),
        }
        for name, d in datasets.items():
            files[f"{bundle}/datasets/{self.db.name}/{name}.yaml"] = self.ydump(d)
        for s, c in charts.items():
            files[f"{bundle}/charts/{s}.yaml"] = self.ydump(c)

        for content in files.values():  # every file must round-trip
            yaml.safe_load(io.StringIO(content))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path, content in sorted(files.items()):
                z.writestr(path, content)
        return bundle, buf.getvalue()


SUPPORTED_VIZ_TYPES = sorted(
    k for k in Compiler.VIZ_TYPES if "_timeseries" not in k
)
