"""The dashboard-spec reference embedded in the agent's system prompt.

This text is the contract between the LLM and the compiler: the agent writes
specs in exactly this YAML shape; the compiler (compiler/core.py) turns them
into import bundles deterministically.
"""

SPEC_GUIDE = """\
# Dashboard spec schema (YAML)

title: <dashboard title>
slug: <url-slug>            # names the bundle + seeds all uuids; never change it later
datasets:                   # virtual (SQL) datasets on the target database
  <name>:
    main_dttm_col: <col>    # main time column (or omit if none)
    catalog: <catalog>      # optional (engines with catalogs, e.g. Trino/BigQuery)
    schema: <schema>        # optional
    sql: SELECT ...
    columns:                # EVERY column the SQL returns, with engine types
      - {name: c1, type: DATE, is_dttm: true}
      - {name: c2, type: VARCHAR}   # DATE/TIMESTAMP/VARCHAR/BIGINT/DOUBLE...
charts:
  <chart_slug>:
    title: <slice name>
    type: line | bar | area | big_number | big_number_total | cal_heatmap | pie
          | treemap | pivot | sunburst | histogram | bubble | word_cloud | gauge | heatmap
    description: <one sentence>   # stored on the chart AND rendered as an
                                  # italic caption row under the chart
    dataset: <dataset name>
    metric: {column: <col>, aggregate: SUM|AVG|COUNT|MAX|MIN, label: <optional>}
    # SQL-expression metric: {sql: 'APPROX_PERCENTILE(x, 0.5)', label: Median}
    # line/bar/pivot also accept `metrics:` (a list) instead of `metric:`
    time_range: 'No filter' # or 'Last month', 'previous calendar month',
                            # '2025-01-01T00:00:00 : now', ...
    filters:                # optional
      - {column: <col>, op: NOT IN, values: [Unknown]}
      - {column: <col>, op: IS NOT NULL}   # IS NULL / IS NOT NULL need no values
    # per-type extras:
    #  line:        groupby: [...], time_grain: P1D, time_compare: [1 year ago],
    #               color_scheme, x_axis_time_format, x_axis_label_rotation,
    #               row_limit, area
    #  bar:         x_axis: <categorical col> (required), row_limit (top-N,
    #               sorted by first metric desc), sort_ascending, orientation,
    #               show_value, groupby, sort_axis: true (sort by x values asc),
    #               y_axis_format (e.g. '.0%')
    #  big_number:  compare_lag, compare_suffix, aggregation (LAST_VALUE),
    #               color: {r,g,b}
    #  big_number_total: subtitle, header_font_size, timestamp: true (formats a
    #               MAX(dttm) metric as a date/time, time_format optional)
    #  area:        same as line, stacked
    #  cal_heatmap: domain (month), subdomain (day), linear_color_scheme.
    #               REQUIRES a bounded time_range with both ends, e.g.
    #               '2026-01-01T00:00:00 : now' — never 'No filter'
    #  pie:         groupby (str or list), donut, row_limit
    #  treemap:     groupby (list)
    #  pivot:       rows: [...], columns: [...], row/col_totals, row/col_subtotals
    #  sunburst:    groupby (list, hierarchy outer->inner)
    #  histogram:   column (numeric), bins (20), groupby (optional series)
    #  bubble:      entity (dim), x/y/size (metric dicts), series (optional),
    #               x_axis_title, y_axis_title, row_limit
    #  word_cloud:  column (dim), metric, row_limit, rotation (square)
    #  gauge:       metric, min/max (default 0-100), number_format (e.g. '.0%')
    #  heatmap:     x_axis (dim), groupby (y dim), metric, normalize_across,
    #               linear_color_scheme, show_values
markdown:                   # optional named markdown blocks for section headers
  <key>: |
    ### Some title
layout:
  tabs:                     # optional
    <Tab name>:
      - [[chart_slug, width(1-12), height], [chart_slug, w, h]]  # one row
      - [[<markdown key>, 12, 8]]
      - divider
  rows:                     # optional, outside tabs (renders on every tab)
    - [[chart_slug, 12, 69]]
filters:                    # optional native (dashboard) filters
  - name: <label>
    dataset: <dataset name>
    column: <col>
    tab: <Tab name>         # optional: scope to one tab
    charts: [chart_slug]    # optional: default = all charts
    cascade: <parent filter name>   # optional
    multi: true             # default

# Rules
- Chart widths per row must sum to <= 12. Typical heights: 45-90.
- Every chart must appear in the layout.
- Dataset `columns` must list every column the SQL emits, with correct types
  and is_dttm on time columns.
- Unsupported viz types hard-error at compile time — stick to the list above.
- Re-applying an updated spec updates the dashboard in place; changed charts
  and datasets arrive as new content-versioned objects automatically.
"""
