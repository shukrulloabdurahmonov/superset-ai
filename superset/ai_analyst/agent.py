"""The AI Analyst agent: a Claude tool-use loop over Superset tools.

Architecture (per Anthropic's SDK Tool Runner):
- tools are plain functions decorated with @beta_tool, closing over a
  SupersetClient (REST mode) — Phase 2 swaps in in-process implementations
- apply_spec is APPROVAL-GATED: the tool itself asks the approval callback
  (CLI prompt / UI Apply button) before compiling+importing anything
- run_sql is read-only-guarded (sql_guard) on top of Superset's own RBAC
"""
from __future__ import annotations

import json
from typing import Callable

import yaml
from anthropic import Anthropic, beta_tool

from .compiler import Compiler, DatabaseRef, SpecError, SUPPORTED_VIZ_TYPES
from .spec_guide import SPEC_GUIDE
from .sql_guard import SQLGuardError, assert_read_only
from .superset_client import SupersetAPIError, SupersetClient

DEFAULT_MODEL = "claude-opus-5"

SYSTEM_PROMPT = f"""\
You are AI Analyst, an agent embedded in Apache Superset. You help users
explore their data, answer questions about it, and create or modify Superset
dashboards. You work for any company: discover what data exists with your
tools instead of assuming anything about it.

# Workflow for building a dashboard
1. Discover: list databases/schemas/tables, describe candidate tables.
2. Profile: run small read-only queries (counts, distincts, min/max dates,
   null rates) so charts are designed around the data that actually exists.
3. Design: write a dashboard spec (schema below). Prefer varied viz types,
   group charts into sections with markdown headers, give every chart a
   one-sentence business description.
4. validate_spec — fix anything it reports.
5. apply_spec — this asks the user for approval and then imports.
6. verify_dashboard — if charts error or come back empty, diagnose (often a
   SQL or column-type mistake in the spec), fix the spec, re-apply.

# Rules
- Your SQL is strictly read-only; the run_sql tool enforces this.
- Never invent tables or columns: describe them first.
- Keep dataset SQL efficient: aggregate in SQL when a chart needs it.
- Answer data questions directly with run_sql; you don't need a dashboard
  for every question.
- Supported viz types: {", ".join(SUPPORTED_VIZ_TYPES)}. No others.

{SPEC_GUIDE}
"""


class AnalystAgent:
    def __init__(
        self,
        superset: SupersetClient,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        approve: Callable[[str, str], bool] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict], None] | None = None,
        namespace: str = "superset.ai-analyst",
    ):
        self.superset = superset
        self.client = Anthropic(api_key=api_key) if api_key else Anthropic()
        self.model = model
        self.approve = approve or (lambda spec_yaml, summary: True)
        self.on_text = on_text or print
        self.on_tool = on_tool or (lambda name, args: None)
        self.namespace = namespace
        self.messages: list[dict] = []
        self.specs: dict[str, str] = {}  # slug -> spec yaml (Phase 2: DB table)
        self._db_refs: dict[int, DatabaseRef] = {}
        self.tools = self._build_tools()

    # -------------------------------------------------------------- helpers

    def _database_ref(self, database_id: int) -> DatabaseRef:
        if database_id not in self._db_refs:
            name, uid, text = self.superset.export_database_yaml(database_id)
            self._db_refs[database_id] = DatabaseRef(name=name, uuid=uid,
                                                     yaml_text=text)
        return self._db_refs[database_id]

    def _compile(self, spec_yaml: str, database_id: int):
        spec = yaml.safe_load(spec_yaml)
        if not isinstance(spec, dict):
            raise SpecError("spec must be a YAML mapping")
        compiler = Compiler(self._database_ref(database_id),
                            namespace=self.namespace)
        return spec, compiler.compile(spec)

    # ---------------------------------------------------------------- tools

    def _build_tools(self):
        superset = self.superset
        agent = self

        @beta_tool
        def list_databases() -> str:
            """List the databases registered in this Superset instance."""
            return json.dumps(superset.list_databases())

        @beta_tool
        def list_schemas(database_id: int) -> str:
            """List schemas in a database.

            Args:
                database_id: numeric id from list_databases.
            """
            return json.dumps(superset.list_schemas(database_id))

        @beta_tool
        def list_tables(database_id: int, schema: str) -> str:
            """List tables in a schema.

            Args:
                database_id: numeric id from list_databases.
                schema: schema name from list_schemas.
            """
            return json.dumps(superset.list_tables(database_id, schema))

        @beta_tool
        def describe_table(database_id: int, schema: str, table: str) -> str:
            """Get a table's columns and types.

            Args:
                database_id: numeric id from list_databases.
                schema: schema name.
                table: table name.
            """
            return json.dumps(superset.describe_table(database_id, schema, table))

        @beta_tool
        def run_sql(database_id: int, sql: str, schema: str = "",
                    catalog: str = "", row_limit: int = 100) -> str:
            """Run a single read-only SELECT and return rows as JSON.

            Args:
                database_id: numeric id from list_databases.
                sql: one read-only SELECT statement.
                schema: optional default schema.
                catalog: optional catalog (engines like Trino/BigQuery).
                row_limit: max rows to return (default 100, cap 1000).
            """
            try:
                safe_sql = assert_read_only(sql)
                result = superset.run_sql(
                    database_id, safe_sql, schema=schema or None,
                    catalog=catalog or None, row_limit=min(int(row_limit), 1000),
                )
            except (SQLGuardError, SupersetAPIError) as e:
                return json.dumps({"error": str(e)})
            text = json.dumps(result, default=str)
            if len(text) > 50_000:
                result["rows"] = result["rows"][:20]
                result["truncated"] = True
                text = json.dumps(result, default=str)
            return text

        @beta_tool
        def validate_spec(spec_yaml: str, database_id: int) -> str:
            """Dry-run compile a dashboard spec; returns ok or actionable errors.

            Args:
                spec_yaml: the full dashboard spec as YAML.
                database_id: database the spec's datasets run on.
            """
            try:
                spec, (bundle, blob) = agent._compile(spec_yaml, database_id)
            except (SpecError, yaml.YAMLError) as e:
                return json.dumps({"ok": False, "error": str(e)})
            return json.dumps({
                "ok": True,
                "charts": len(spec.get("charts", {})),
                "datasets": len(spec.get("datasets", {})),
                "bundle_bytes": len(blob),
            })

        @beta_tool
        def apply_spec(spec_yaml: str, database_id: int, summary: str) -> str:
            """Compile a spec and import it into Superset. Requires the user's
            approval — the user is shown your summary and can decline.

            Args:
                spec_yaml: the full dashboard spec as YAML.
                database_id: database the spec's datasets run on.
                summary: 1-3 sentences describing what will be created/changed.
            """
            try:
                spec, (bundle, blob) = agent._compile(spec_yaml, database_id)
            except (SpecError, yaml.YAMLError) as e:
                return json.dumps({"ok": False, "error": str(e)})
            if not agent.approve(spec_yaml, summary):
                return json.dumps({"ok": False,
                                   "error": "user declined the apply"})
            try:
                agent.superset.import_dashboard(blob, filename=f"{bundle}.zip")
            except SupersetAPIError as e:
                return json.dumps({"ok": False, "error": str(e)})
            agent.specs[spec["slug"]] = spec_yaml
            return json.dumps({"ok": True, "slug": spec["slug"],
                               "url": f"{superset.base_url}/superset/dashboard/{spec['slug']}/"})

        @beta_tool
        def get_dashboard_spec(slug: str) -> str:
            """Get the stored spec of a dashboard previously created here.

            Args:
                slug: the dashboard slug.
            """
            if slug in agent.specs:
                return agent.specs[slug]
            return json.dumps({"error": f"no stored spec for '{slug}' — it was "
                               "not created in this session"})

        @beta_tool
        def verify_dashboard(id_or_slug: str) -> str:
            """Verify a dashboard: every dataset's SQL is executed (LIMIT 5),
            and every chart's saved query is run when possible. Charts report
            'not_rendered_yet' until first opened in a browser — that is
            normal for a fresh import, not a failure; dataset errors ARE
            failures to fix.

            Args:
                id_or_slug: dashboard numeric id or slug.
            """
            try:
                return json.dumps(superset.verify_dashboard(id_or_slug))
            except SupersetAPIError as e:
                return json.dumps({"error": str(e)})

        return [list_databases, list_schemas, list_tables, describe_table,
                run_sql, validate_spec, apply_spec, get_dashboard_spec,
                verify_dashboard]

    # ----------------------------------------------------------------- chat

    def chat(self, user_input: str) -> str:
        """One user turn; runs the tool loop to completion, returns final text."""
        self.messages.append({"role": "user", "content": user_input})
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            tools=self.tools,
            messages=self.messages,
        )
        final_text = ""
        for message in runner:
            for block in message.content:
                if block.type == "text":
                    final_text = block.text
                    self.on_text(block.text)
                elif block.type == "tool_use":
                    self.on_tool(block.name, block.input)
            # mirror history so the next chat() turn has full context
            self.messages.append(
                {"role": "assistant", "content": message.content}
            )
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)
            if message.stop_reason == "refusal":
                final_text = "(The model declined to continue with this request.)"
                self.on_text(final_text)
        return final_text
