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

# Modifying an existing dashboard
1. get_dashboard_spec — returns the stored spec (AI-created dashboards) or a
   reverse-engineered one with warnings (anything else). Read the warnings.
2. Edit only what the user asked for; KEEP the slug unchanged so the apply
   updates the dashboard in place (changed charts/datasets re-version
   automatically; unchanged ones are untouched).
3. validate_spec, then apply_spec as usual.
Answer questions about a dashboard's content from its spec instead of
guessing.

# Rules
- Your SQL is strictly read-only; the run_sql tool enforces this.
- Never invent tables or columns: describe them first.
- Keep dataset SQL efficient: aggregate in SQL when a chart needs it.
- Answer data questions directly with run_sql; you don't need a dashboard
  for every question.
- Supported viz types: {", ".join(SUPPORTED_VIZ_TYPES)}. No others.

{SPEC_GUIDE}
"""

PLAN_MODE_PROMPT = """\
PLAN MODE IS ON for this turn. If the user is asking you to BUILD or MODIFY
a dashboard: explore/profile as needed, then present a concise plan —
sections, charts (type + metric + dimension), datasets — as a short readable
list, and END YOUR TURN asking whether to proceed. Do NOT call validate_spec
or apply_spec in the same turn as the plan. Only after the user confirms
(e.g. "yes", "go", "build it") do the actual build in a later turn.
Plain data questions are exempt — answer them directly.
"""

IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_MIME_PREFIXES = ("text/",)
TEXT_MIMES = {"application/json", "application/x-yaml", "application/csv",
              "application/xml", "application/sql"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TEXT_CHARS = 100_000


def _attachment_blocks(attachments: list[dict]) -> list[dict]:
    """[{name, mime, data_b64}] -> Anthropic content blocks. Raises ValueError
    on unsupported/oversized attachments (before any API call is made)."""
    import base64

    blocks: list[dict] = []
    for a in attachments:
        name = a.get("name", "attachment")
        mime = (a.get("mime") or "").split(";")[0].strip().lower()
        data = a.get("data_b64") or ""
        if mime in IMAGE_MIMES:
            if len(data) * 3 // 4 > MAX_IMAGE_BYTES:
                raise ValueError(f"image '{name}' exceeds 4 MB")
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data},
            })
            blocks.append({"type": "text", "text": f"[Attached image: {name}]"})
        elif mime in TEXT_MIMES or mime.startswith(TEXT_MIME_PREFIXES):
            try:
                text = base64.b64decode(data).decode("utf-8", errors="replace")
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"could not decode '{name}': {e}") from e
            truncated = len(text) > MAX_TEXT_CHARS
            text = text[:MAX_TEXT_CHARS]
            note = " (truncated)" if truncated else ""
            blocks.append({
                "type": "text",
                "text": f"[Attached file: {name}{note}]\n```\n{text}\n```",
            })
        else:
            raise ValueError(
                f"attachment '{name}' has unsupported type '{mime}'. "
                "Supported: png/jpeg/gif/webp images and text files "
                "(csv, json, sql, yaml, plain text)."
            )
    return blocks


class AnalystAgent:
    def __init__(
        self,
        superset: SupersetClient,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        approve: Callable[[str, str], bool] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict], None] | None = None,
        on_approval: Callable[[str, str, str], None] | None = None,
        defer_apply: bool = False,
        namespace: str = "superset.ai-analyst",
    ):
        """defer_apply=False (CLI): apply_spec calls `approve` synchronously.
        defer_apply=True (web): apply_spec parks the compiled bundle in
        self.pending and returns 'awaiting_approval'; the UI's Apply button
        triggers the actual import out-of-band (see api.py)."""
        # lazy import: Superset must boot even when the ai-analyst extra
        # is not installed (pip install apache_superset[ai-analyst])
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError(
                "AI Analyst requires the 'anthropic' package: "
                "pip install 'apache_superset[ai-analyst]'"
            ) from e
        self.superset = superset
        self.client = Anthropic(api_key=api_key) if api_key else Anthropic()
        self.model = model
        self.approve = approve or (lambda spec_yaml, summary: True)
        self.on_text = on_text or print
        self.on_tool = on_tool or (lambda name, args: None)
        self.on_approval = on_approval or (lambda aid, summary, spec: None)
        self.defer_apply = defer_apply
        self.namespace = namespace
        self.messages: list[dict] = []
        self.specs: dict[str, str] = {}  # slug -> spec yaml (mirrored to DB in web mode)
        self.pending: dict[str, dict] = {}  # approval_id -> {spec_yaml, database_id, summary}
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
        from anthropic import beta_tool

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
            if agent.defer_apply:
                import uuid as _uuid
                aid = _uuid.uuid4().hex[:12]
                agent.pending[aid] = {"spec_yaml": spec_yaml,
                                      "database_id": database_id,
                                      "summary": summary,
                                      "slug": spec["slug"]}
                agent.on_approval(aid, summary, spec_yaml)
                return json.dumps({
                    "ok": False, "status": "awaiting_approval",
                    "approval_id": aid,
                    "note": "The user has been shown an Apply button with your "
                            "summary. The import runs only after they approve. "
                            "End your turn now and tell the user what the "
                            "dashboard will contain.",
                })
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
            """Get the spec of an existing dashboard so it can be modified.
            Returns the stored spec when the dashboard was created by AI
            Analyst; otherwise reverse-engineers a best-effort spec from the
            dashboard itself and lists warnings about anything lossy.

            Args:
                slug: the dashboard slug or numeric id.
            """
            if slug in agent.specs:
                return agent.specs[slug]
            if not hasattr(superset, "reverse_dashboard_spec"):
                return json.dumps({"error": f"no stored spec for '{slug}', and "
                                   "spec recovery is unavailable in this mode"})
            try:
                spec, warnings = superset.reverse_dashboard_spec(slug)
            except SupersetAPIError as e:
                return json.dumps({"error": str(e)})
            return json.dumps({
                "recovered_spec_yaml": yaml.dump(spec, sort_keys=False,
                                                 allow_unicode=True),
                "warnings": warnings,
                "note": "This spec was reverse-engineered, not stored. "
                        "Review the warnings: re-applying REPLACES the "
                        "dashboard with exactly what the spec expresses.",
            })

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

    # ------------------------------------------------------- deferred apply

    def execute_pending(self, approval_id: str) -> str:
        """Run a user-approved apply out-of-band; returns the dashboard slug."""
        p = self.pending.pop(approval_id)
        spec, (bundle, blob) = self._compile(p["spec_yaml"], p["database_id"])
        self.superset.import_dashboard(blob, filename=f"{bundle}.zip")
        self.specs[spec["slug"]] = p["spec_yaml"]
        self.messages.append({
            "role": "user",
            "content": f"[system note] The user APPROVED apply {approval_id}: "
                       f"dashboard '{spec['slug']}' was compiled and imported "
                       "successfully. Do not apply it again.",
        })
        return spec["slug"]

    def decline_pending(self, approval_id: str) -> None:
        p = self.pending.pop(approval_id)
        self.messages.append({
            "role": "user",
            "content": f"[system note] The user DECLINED apply {approval_id} "
                       f"for dashboard '{p['slug']}'. Ask what to change.",
        })

    # ----------------------------------------------------------------- chat

    # -------------------------------------------------------- serialization

    @staticmethod
    def _dump_message(msg: dict) -> dict:
        content = msg.get("content")
        if isinstance(content, list):
            content = [b.model_dump() if hasattr(b, "model_dump") else b
                       for b in content]
        return {"role": msg["role"], "content": content}

    def export_messages(self) -> list[dict]:
        """JSON-safe message history (thinking signatures preserved)."""
        return [self._dump_message(m) for m in self.messages]

    def load_messages(self, messages: list[dict]) -> None:
        self.messages = list(messages)

    def chat(self, user_input: str, attachments: list[dict] | None = None,
             plan_mode: bool = False) -> str:
        """One user turn; runs the tool loop to completion, returns final text."""
        if attachments:
            content: list[dict] | str = _attachment_blocks(attachments) + [
                {"type": "text", "text": user_input}
            ]
        else:
            content = user_input
        self.messages.append({"role": "user", "content": content})
        system: list[dict] = [{"type": "text", "text": SYSTEM_PROMPT,
                               "cache_control": {"type": "ephemeral"}}]
        if plan_mode:
            system.append({"type": "text", "text": PLAN_MODE_PROMPT})
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system,
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
