"""In-process implementation of the agent's Superset surface.

Duck-type compatible with superset_client.SupersetClient, but runs inside
Superset's web process: metadata and SQL go through the Database model and
the security manager, so the LOGGED-IN USER'S RBAC applies to everything the
agent can see or run. Imports go through the same command the import API uses.
"""
from __future__ import annotations

import io
import zipfile
from zipfile import ZipFile

from superset.ai_analyst.superset_client import SupersetAPIError
from superset.commands.dashboard.importers.dispatcher import ImportDashboardsCommand
from superset.commands.database.export import ExportDatabasesCommand
from superset.commands.importers.v1.utils import get_contents_from_bundle
from superset.daos.dashboard import DashboardDAO
from superset.daos.database import DatabaseDAO
from superset.extensions import security_manager
from superset.sql.parse import Table


class InProcessSupersetService:
    base_url = ""  # links rendered relative to the current Superset host

    # ------------------------------------------------------------- metadata

    def _database(self, database_id: int):
        database = DatabaseDAO.find_by_id(database_id)
        if database is None or not security_manager.can_access_database(database):
            raise SupersetAPIError(f"database {database_id} not found or not accessible")
        return database

    def list_databases(self) -> list[dict]:
        return [
            {"id": d.id, "name": d.database_name, "uuid": str(d.uuid),
             "backend": d.backend}
            for d in DatabaseDAO.find_all()
            if security_manager.can_access_database(d)
        ]

    def list_schemas(self, database_id: int) -> list[str]:
        return sorted(self._database(database_id).get_all_schema_names())

    def list_tables(self, database_id: int, schema: str) -> list[str]:
        names = self._database(database_id).get_all_table_names_in_schema(
            catalog=None, schema=schema
        )
        return sorted(t[0] for t in names)

    def describe_table(self, database_id: int, schema: str, table: str) -> dict:
        cols = self._database(database_id).get_columns(Table(table, schema))
        return {
            "name": table,
            "columns": [
                {"name": c["column_name"], "type": str(c.get("type"))} for c in cols
            ],
        }

    def export_database_yaml(self, database_id: int) -> tuple[str, str, str]:
        database = self._database(database_id)
        for file_name, content_fn in ExportDatabasesCommand(
            [database.id], export_related=False
        ).run():
            if file_name.startswith("databases/") and file_name.endswith(".yaml"):
                return database.database_name, str(database.uuid), content_fn()
        raise SupersetAPIError("database export produced no databases/*.yaml")

    # ------------------------------------------------------------------ sql

    def run_sql(self, database_id: int, sql: str, schema: str | None = None,
                catalog: str | None = None, row_limit: int = 100) -> dict:
        database = self._database(database_id)
        security_manager.raise_for_access(
            database=database, sql=sql, schema=schema, catalog=catalog
        )
        limited = database.apply_limit_to_sql(sql, row_limit)
        try:
            df = database.get_df(limited, catalog=catalog, schema=schema)
        except Exception as e:  # noqa: BLE001 - message goes back to the agent
            raise SupersetAPIError(str(e)[:500]) from e
        return {
            "columns": list(df.columns),
            "rows": df.head(row_limit).to_dict(orient="records"),
        }

    # --------------------------------------------------------------- import

    def import_dashboard(self, bundle_zip: bytes, filename: str = "bundle.zip") -> None:
        with ZipFile(io.BytesIO(bundle_zip)) as bundle:
            contents = get_contents_from_bundle(bundle)
        ImportDashboardsCommand(contents, overwrite=True).run()

    def import_chart(self, bundle_zip: bytes) -> None:
        from superset.commands.chart.importers.dispatcher import (
            ImportChartsCommand,
        )

        with ZipFile(io.BytesIO(bundle_zip)) as bundle:
            contents = get_contents_from_bundle(bundle)
        ImportChartsCommand(contents, overwrite=True).run()

    def chart_id_by_uuid(self, chart_uuid: str) -> int | None:
        from superset.models.slice import Slice

        try:
            return Slice.get(chart_uuid).id
        except Exception:  # noqa: BLE001 - not found
            return None

    # --------------------------------------------------------------- verify

    def _dashboard(self, id_or_slug: str):
        dashboard = DashboardDAO.get_by_id_or_slug(str(id_or_slug))
        if dashboard is None:
            raise SupersetAPIError(f"dashboard '{id_or_slug}' not found")
        return dashboard

    def reverse_dashboard_spec(self, id_or_slug: str) -> tuple[dict, list[str]]:
        """Best-effort spec for a dashboard with no stored spec (lossy)."""
        from superset.ai_analyst.reverse import reverse_spec

        return reverse_spec(self._dashboard(id_or_slug))

    def dashboard_charts(self, id_or_slug: str) -> list[dict]:
        return [
            {"id": s.id, "slice_name": s.slice_name, "viz_type": s.viz_type}
            for s in self._dashboard(id_or_slug).slices
        ]

    def chart_name(self, slice_id: int) -> str | None:
        from superset.daos.chart import ChartDAO

        slc = ChartDAO.find_by_id(slice_id)
        return slc.slice_name if slc is not None else None

    def verify_dashboard(self, id_or_slug: str) -> dict:
        """Dataset-level verification: run every (virtual) dataset's SQL with
        a LIMIT — this catches bad SQL/columns, the dominant failure class.
        Chart queries themselves first run when the dashboard is opened."""
        dashboard = self._dashboard(id_or_slug)
        datasets, seen = [], set()
        for s in dashboard.slices:
            ds = s.datasource
            if ds is None or ds.uid in seen:
                continue
            seen.add(ds.uid)
            sql = getattr(ds, "sql", None)
            if not sql:
                datasets.append({"dataset": ds.name, "status": "ok",
                                 "detail": "physical table"})
                continue
            try:
                res = self.run_sql(
                    ds.database.id, sql,
                    schema=getattr(ds, "schema", None) or None,
                    catalog=getattr(ds, "catalog", None) or None,
                    row_limit=5,
                )
                datasets.append({
                    "dataset": ds.name,
                    "status": "ok" if res["rows"] else "empty",
                    "detail": f"{len(res['rows'])} sample rows",
                })
            except SupersetAPIError as e:
                datasets.append({"dataset": ds.name, "status": "error",
                                 "detail": str(e)[:400]})
        charts = [
            {"slice_id": s.id, "name": s.slice_name, "status": "imported",
             "detail": "chart queries run on first dashboard open"}
            for s in dashboard.slices
        ]
        return {"datasets": datasets, "charts": charts}
