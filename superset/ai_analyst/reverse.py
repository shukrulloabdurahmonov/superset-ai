"""Reverse compiler: existing Superset dashboard -> best-effort spec.

Used when the user wants to modify a dashboard that has NO stored spec
(not created by AI Analyst). The inverse of compiler/core.py: only the
fields the compiler treats as varying are extracted; boilerplate params are
dropped. Anything that can't be expressed in the spec is reported as a
warning so the agent (and the user) know re-applying is lossy there.

In-process only: works directly on the Dashboard model.
"""
from __future__ import annotations

import json
import re
from typing import Any

VIZ_REVERSE = {
    "echarts_timeseries_line": "line",
    "echarts_timeseries_bar": "bar",
    "echarts_area": "area",
    "big_number": "big_number",
    "big_number_total": "big_number_total",
    "cal_heatmap": "cal_heatmap",
    "pie": "pie",
    "treemap_v2": "treemap",
    "pivot_table_v2": "pivot",
    "sunburst_v2": "sunburst",
    "histogram_v2": "histogram",
    "bubble_v2": "bubble",
    "word_cloud": "word_cloud",
    "gauge_chart": "gauge",
    "heatmap_v2": "heatmap",
}


def _slugify(name: str, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "chart"
    base, i = slug, 2
    while slug in taken:
        slug = f"{base}_{i}"
        i += 1
    taken.add(slug)
    return slug


def _rev_metric(m: dict | None) -> dict | None:
    if not isinstance(m, dict):
        return None
    if m.get("expressionType") == "SQL":
        out: dict[str, Any] = {"sql": m.get("sqlExpression")}
    else:
        col = m.get("column") or {}
        out = {"column": col.get("column_name"), "aggregate": m.get("aggregate")}
    if m.get("hasCustomLabel") and m.get("label"):
        out["label"] = m["label"]
    return out


def _rev_metrics(p: dict) -> list[dict]:
    return [x for x in (_rev_metric(m) for m in p.get("metrics", [])) if x]


def _rev_filters(p: dict, main_dttm: str | None, chart: dict) -> None:
    """Fills time_range / time_column / filters on the chart spec in place."""
    filters = []
    for f in p.get("adhoc_filters", []):
        if not isinstance(f, dict) or f.get("expressionType") != "SIMPLE":
            continue
        if f.get("operator") == "TEMPORAL_RANGE":
            tr = f.get("comparator", "No filter")
            if tr and tr != "No filter":
                chart["time_range"] = tr
            if f.get("subject") and main_dttm and f["subject"] != main_dttm:
                chart["time_column"] = f["subject"]
            continue
        flt = {"column": f.get("subject"), "op": f.get("operator")}
        if f.get("operator") not in ("IS NULL", "IS NOT NULL"):
            flt["values"] = f.get("comparator")
        filters.append(flt)
    if filters:
        chart["filters"] = filters


def _put(chart: dict, key: str, value, default) -> None:
    """Keep only non-default values so specs stay small."""
    if value is not None and value != default:
        chart[key] = value


def _rev_chart(slc, ds_names: dict[int, str], main_dttms: dict[int, str | None],
               warnings: list[str]) -> dict | None:
    ctype = VIZ_REVERSE.get(slc.viz_type)
    if ctype is None:
        warnings.append(
            f"chart '{slc.slice_name}' has unsupported viz_type "
            f"'{slc.viz_type}' — it is NOT in the spec; re-applying the spec "
            "will drop it from the dashboard layout"
        )
        return None
    try:
        p = json.loads(slc.params or "{}")
    except json.JSONDecodeError:
        warnings.append(f"chart '{slc.slice_name}': unparseable params, skipped")
        return None
    ds = slc.datasource
    if ds is None or ds.id not in ds_names:
        warnings.append(f"chart '{slc.slice_name}': no datasource, skipped")
        return None
    main_dttm = main_dttms.get(ds.id)

    c: dict[str, Any] = {"title": slc.slice_name, "type": ctype,
                         "dataset": ds_names[ds.id]}
    if slc.description:
        c["description"] = slc.description

    single = _rev_metric(p.get("metric"))
    many = _rev_metrics(p)
    if ctype in ("line", "bar", "area", "pivot"):
        if len(many) > 1:
            c["metrics"] = many
        elif many:
            c["metric"] = many[0]
    elif ctype == "cal_heatmap":
        if many:
            c["metric"] = many[0]
    elif ctype != "bubble" and single:
        c["metric"] = single

    _rev_filters(p, main_dttm, c)

    if ctype in ("line", "area"):
        _put(c, "groupby", p.get("groupby"), [])
        _put(c, "time_grain", p.get("time_grain_sqla"), "P1D")
        _put(c, "time_compare", p.get("time_compare"), None)
        _put(c, "color_scheme", p.get("color_scheme"), "supersetColors")
        _put(c, "x_axis_time_format", p.get("x_axis_time_format"), "smart_date")
        _put(c, "x_axis_label_rotation", p.get("xAxisLabelRotation"), 0)
        _put(c, "row_limit", p.get("row_limit"), 10000)
        if ctype == "line":
            _put(c, "area", p.get("area"), False)
    elif ctype == "bar":
        c["x_axis"] = p.get("x_axis")
        _put(c, "groupby", p.get("groupby"), [])
        _put(c, "row_limit", p.get("row_limit"), 100)
        _put(c, "orientation", p.get("orientation"), "vertical")
        _put(c, "show_value", p.get("show_value"), True)
        _put(c, "y_axis_format", p.get("y_axis_format"), "SMART_NUMBER")
        _put(c, "x_axis_label_rotation", p.get("xAxisLabelRotation"), 45)
        if p.get("x_axis_sort") == p.get("x_axis"):
            c["sort_axis"] = True
        else:
            _put(c, "sort_ascending", p.get("x_axis_sort_asc"), False)
    elif ctype == "big_number":
        _put(c, "compare_lag", p.get("compare_lag"), 1)
        _put(c, "compare_suffix", p.get("compare_suffix"), "")
        _put(c, "aggregation", p.get("aggregation"), "LAST_VALUE")
        _put(c, "time_grain", p.get("time_grain_sqla"), "P1D")
        cp = p.get("color_picker") or {}
        if cp and (cp.get("r"), cp.get("g"), cp.get("b")) != (0, 122, 135):
            c["color"] = {"r": cp.get("r"), "g": cp.get("g"), "b": cp.get("b")}
    elif ctype == "big_number_total":
        _put(c, "subtitle", p.get("subheader"), "")
        _put(c, "header_font_size", p.get("header_font_size"), 0.4)
        if p.get("force_timestamp_formatting"):
            c["timestamp"] = True
            _put(c, "time_format", p.get("time_format"), "%d.%m.%Y %H:%M")
    elif ctype == "cal_heatmap":
        c["time_range"] = p.get("time_range", c.get("time_range", "No filter"))
        _put(c, "domain", p.get("domain_granularity"), "month")
        _put(c, "subdomain", p.get("subdomain_granularity"), "day")
        _put(c, "linear_color_scheme", p.get("linear_color_scheme"), "dark_blue")
        if p.get("granularity_sqla") and p["granularity_sqla"] != main_dttm:
            c["time_column"] = p["granularity_sqla"]
    elif ctype == "pie":
        gb = p.get("groupby", [])
        c["groupby"] = gb[0] if isinstance(gb, list) and len(gb) == 1 else gb
        _put(c, "donut", p.get("donut"), False)
        _put(c, "row_limit", p.get("row_limit"), 100)
        _put(c, "color_scheme", p.get("color_scheme"), "supersetColors")
    elif ctype == "treemap":
        c["groupby"] = p.get("groupby", [])
        _put(c, "row_limit", p.get("row_limit"), 10000)
    elif ctype == "pivot":
        _put(c, "rows", p.get("groupbyRows"), [])
        _put(c, "columns", p.get("groupbyColumns"), [])
        _put(c, "row_totals", p.get("rowTotals"), False)
        _put(c, "row_subtotals", p.get("rowSubTotals"), False)
        _put(c, "col_totals", p.get("colTotals"), False)
        _put(c, "col_subtotals", p.get("colSubTotals"), False)
        _put(c, "row_limit", p.get("row_limit"), 10000)
    elif ctype == "sunburst":
        c["groupby"] = p.get("columns", [])
        _put(c, "row_limit", p.get("row_limit"), 10000)
    elif ctype == "histogram":
        c["column"] = p.get("column")
        _put(c, "bins", p.get("bins"), 20)
        _put(c, "groupby", p.get("groupby"), [])
        _put(c, "row_limit", p.get("row_limit"), 10000)
        _put(c, "x_axis_title", p.get("x_axis_title"), "")
        _put(c, "y_axis_title", p.get("y_axis_title"), "")
    elif ctype == "bubble":
        c["entity"] = p.get("entity")
        _put(c, "series", p.get("series"), None)
        for k in ("x", "y", "size"):
            m = _rev_metric(p.get(k))
            if m:
                c[k] = m
        _put(c, "row_limit", p.get("row_limit"), 100)
        _put(c, "x_axis_title", p.get("x_axis_title"), "")
        _put(c, "y_axis_title", p.get("y_axis_title"), "")
    elif ctype == "word_cloud":
        c["column"] = p.get("series")
        _put(c, "row_limit", p.get("row_limit"), 100)
        _put(c, "rotation", p.get("rotation"), "square")
    elif ctype == "gauge":
        _put(c, "min", p.get("min_val"), 0)
        _put(c, "max", p.get("max_val"), 100)
        _put(c, "number_format", p.get("number_format"), "SMART_NUMBER")
    elif ctype == "heatmap":
        c["x_axis"] = p.get("x_axis")
        c["groupby"] = p.get("groupby")
        _put(c, "normalize_across", p.get("normalize_across"), "heatmap")
        _put(c, "linear_color_scheme", p.get("linear_color_scheme"),
             "blue_white_yellow")
        _put(c, "show_values", p.get("show_values"), False)
    return c


def _is_caption_markdown(meta: dict) -> bool:
    code = (meta.get("code") or "").strip()
    return meta.get("height", 99) <= 14 and (
        code == "&nbsp;" or (code.startswith("*") and code.endswith("*"))
    )


def reverse_spec(dashboard) -> tuple[dict, list[str]]:
    """Dashboard model -> (spec dict, warnings). Best effort, lossy."""
    warnings: list[str] = []
    position = json.loads(dashboard.position_json or "{}")
    metadata = json.loads(dashboard.json_metadata or "{}")

    # ------------------------------------------------------------- datasets
    ds_names: dict[int, str] = {}
    main_dttms: dict[int, str | None] = {}
    datasets: dict[str, dict] = {}
    for slc in dashboard.slices:
        ds = slc.datasource
        if ds is None or ds.id in ds_names:
            continue
        ds_names[ds.id] = ds.table_name
        main_dttms[ds.id] = ds.main_dttm_col
        entry: dict[str, Any] = {}
        if ds.main_dttm_col:
            entry["main_dttm_col"] = ds.main_dttm_col
        if getattr(ds, "catalog", None):
            entry["catalog"] = ds.catalog
        if getattr(ds, "schema", None):
            entry["schema"] = ds.schema
        if getattr(ds, "sql", None):
            entry["sql"] = ds.sql
        else:
            warnings.append(
                f"dataset '{ds.table_name}' is a physical table; the spec "
                "format only supports virtual (SQL) datasets — set `sql` "
                f"(e.g. SELECT ... FROM {ds.table_name}) before applying"
            )
        entry["columns"] = [
            {"name": c.column_name, "type": c.type or "VARCHAR",
             **({"is_dttm": True} if c.is_dttm else {})}
            for c in ds.columns
        ]
        datasets[ds.table_name] = entry

    # --------------------------------------------------------------- charts
    taken: set[str] = set()
    charts: dict[str, dict] = {}
    slug_by_slice_id: dict[int, str] = {}
    for slc in dashboard.slices:
        c = _rev_chart(slc, ds_names, main_dttms, warnings)
        if c is None:
            continue
        slug = _slugify(slc.slice_name, taken)
        charts[slug] = c
        slug_by_slice_id[slc.id] = slug

    # --------------------------------------------------------------- layout
    markdown: dict[str, str] = {}
    md_i = 0

    def rows_of(node_ids: list[str]) -> list:
        nonlocal md_i
        rows: list[Any] = []
        for nid in node_ids:
            node = position.get(nid) or {}
            ntype = node.get("type")
            if ntype == "DIVIDER":
                rows.append("divider")
            elif ntype == "ROW":
                row = []
                for child_id in node.get("children", []):
                    child = position.get(child_id) or {}
                    meta = child.get("meta") or {}
                    if child.get("type") == "CHART":
                        slug = slug_by_slice_id.get(meta.get("chartId"))
                        if slug:
                            row.append([slug, meta.get("width", 4),
                                        meta.get("height", 50)])
                    elif child.get("type") == "MARKDOWN":
                        if _is_caption_markdown(meta):
                            continue  # captions regenerate from descriptions
                        md_i += 1
                        key = f"md_{md_i}"
                        markdown[key] = meta.get("code", "")
                        row.append([key, meta.get("width", 12),
                                    meta.get("height", 8)])
                if row:
                    rows.append(row)
        return rows

    layout: dict[str, Any] = {}
    tab_names: dict[str, str] = {}
    grid = position.get("GRID_ID") or {}
    tabs: dict[str, list] = {}
    plain_rows: list = []
    for child_id in grid.get("children", []):
        node = position.get(child_id) or {}
        if node.get("type") == "TABS":
            for tab_id in node.get("children", []):
                tab = position.get(tab_id) or {}
                name = (tab.get("meta") or {}).get("text", tab_id)
                tab_names[tab_id] = name
                tabs[name] = rows_of(tab.get("children", []))
        else:
            plain_rows.extend(rows_of([child_id]))
    if tabs:
        layout["tabs"] = tabs
    if plain_rows:
        layout["rows"] = plain_rows

    laid_out = set()
    for rows in list(tabs.values()) + [plain_rows]:
        for row in rows:
            if isinstance(row, list):
                for entry in row:
                    laid_out.add(entry[0])
    for slug in charts:
        if slug not in laid_out:
            warnings.append(f"chart '{slug}' was not found in the layout; "
                            "add it to layout before applying")

    # -------------------------------------------------------------- filters
    filters = []
    uuid_to_ds: dict[str, str] = {}
    id_to_ds: dict[int, str] = {}
    for slc in dashboard.slices:
        if slc.datasource is not None:
            uuid_to_ds[str(slc.datasource.uuid)] = slc.datasource.table_name
            id_to_ds[slc.datasource.id] = slc.datasource.table_name
    nf_by_id = {f.get("id"): f
                for f in metadata.get("native_filter_configuration", [])}
    for f in metadata.get("native_filter_configuration", []):
        if f.get("filterType") != "filter_select":
            warnings.append(f"native filter '{f.get('name')}' has type "
                            f"'{f.get('filterType')}' — only filter_select is "
                            "supported; it will be dropped on re-apply")
            continue
        target = (f.get("targets") or [{}])[0]
        # bundles carry datasetUuid; Superset's import rewrites it to datasetId
        ds_name = uuid_to_ds.get(target.get("datasetUuid") or "") or id_to_ds.get(
            target.get("datasetId")
        )
        if not ds_name:
            warnings.append(f"native filter '{f.get('name')}': dataset not "
                            "resolvable, dropped")
            continue
        entry = {"name": f.get("name"), "dataset": ds_name,
                 "column": (target.get("column") or {}).get("name")}
        if not f.get("controlValues", {}).get("multiSelect", True):
            entry["multi"] = False
        root = (f.get("scope") or {}).get("rootPath") or []
        if root and root != ["ROOT_ID"] and root[0] in tab_names:
            entry["tab"] = tab_names[root[0]]
        in_scope = f.get("chartsInScope") or []
        slugs = [slug_by_slice_id[i] for i in in_scope if i in slug_by_slice_id]
        if slugs and len(slugs) < len(charts):
            entry["charts"] = slugs
        for pid in f.get("cascadeParentIds") or []:
            parent = nf_by_id.get(pid)
            if parent:
                entry["cascade"] = parent.get("name")
        filters.append(entry)

    spec: dict[str, Any] = {
        "title": dashboard.dashboard_title,
        "slug": dashboard.slug or f"dash-{dashboard.id}",
    }
    if not dashboard.slug:
        warnings.append(
            f"dashboard has no slug; generated 'dash-{dashboard.id}' — "
            "applying will create a NEW dashboard rather than update this one"
        )
    spec["datasets"] = datasets
    spec["charts"] = charts
    if markdown:
        spec["markdown"] = markdown
    spec["layout"] = layout
    if filters:
        spec["filters"] = filters
    return spec, warnings
