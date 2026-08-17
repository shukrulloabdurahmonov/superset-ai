"""Thin REST client for a (possibly remote) Superset instance.

Used by the CLI harness (Phase 1) and by tests. Inside the fork's own web
process the tools use Superset's Python internals instead (Phase 2); this
client lets the same agent drive ANY reachable Superset — including existing
instances that are not this fork.

Only stable /api/v1 endpoints are used.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass

import requests


class SupersetAPIError(RuntimeError):
    pass


@dataclass
class ChartStatus:
    slice_id: int
    name: str
    status: str  # ok | empty | error
    detail: str = ""


class SupersetClient:
    def __init__(self, base_url: str, username: str, password: str,
                 provider: str = "db", timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        r = self.s.post(
            f"{self.base_url}/api/v1/security/login",
            json={"username": username, "password": password,
                  "provider": provider, "refresh": True},
            timeout=timeout,
        )
        if r.status_code != 200:
            raise SupersetAPIError(f"login failed ({r.status_code}): {r.text[:300]}")
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        r = self.s.get(f"{self.base_url}/api/v1/security/csrf_token/", timeout=timeout)
        if r.status_code == 200:
            self.s.headers["X-CSRFToken"] = r.json()["result"]
        self.s.headers["Referer"] = self.base_url

    # ------------------------------------------------------------ internals

    def _get(self, path: str, **kw):
        r = self.s.get(f"{self.base_url}{path}", timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise SupersetAPIError(f"GET {path} -> {r.status_code}: {r.text[:500]}")
        return r

    def _post(self, path: str, **kw):
        r = self.s.post(f"{self.base_url}{path}", timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise SupersetAPIError(f"POST {path} -> {r.status_code}: {r.text[:500]}")
        return r

    @staticmethod
    def _rison(obj) -> str:
        # minimal rison encoding good enough for the filters we send
        return (
            json.dumps(obj, separators=(",", ":"))
            .replace('"', "'")
            .replace("{", "(").replace("}", ")")
            .replace("[", "!(").replace("]", ")")
        )

    # ------------------------------------------------------------- metadata

    def list_databases(self) -> list[dict]:
        r = self._get("/api/v1/database/?q=(page_size:100)")
        return [
            {"id": d["id"], "name": d["database_name"], "uuid": d.get("uuid"),
             "backend": d.get("backend")}
            for d in r.json()["result"]
        ]

    def list_schemas(self, database_id: int) -> list[str]:
        return self._get(f"/api/v1/database/{database_id}/schemas/").json()["result"]

    def list_tables(self, database_id: int, schema: str) -> list[str]:
        q = self._rison({"schema_name": schema, "page": 0, "page_size": 200})
        r = self._get(f"/api/v1/database/{database_id}/tables/?q={q}")
        return [t["value"] for t in r.json()["result"]]

    def describe_table(self, database_id: int, schema: str, table: str) -> dict:
        r = self._get(f"/api/v1/database/{database_id}/table/{table}/{schema}/")
        d = r.json()
        return {
            "name": d.get("name", table),
            "columns": [
                {"name": c["name"], "type": c.get("type")} for c in d.get("columns", [])
            ],
        }

    def export_database_yaml(self, database_id: int) -> tuple[str, str, str]:
        """-> (database_name, database_uuid, verbatim databases/<name>.yaml text)."""
        q = self._rison([database_id])
        r = self._get(f"/api/v1/database/export/?q={q}")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        for name in z.namelist():
            if "/databases/" in name and name.endswith(".yaml"):
                text = z.read(name).decode()
                import yaml as _yaml
                d = _yaml.safe_load(text)
                return d["database_name"], d["uuid"], text
        raise SupersetAPIError("database export contained no databases/*.yaml")

    # ------------------------------------------------------------------ sql

    def run_sql(self, database_id: int, sql: str, schema: str | None = None,
                catalog: str | None = None, row_limit: int = 100) -> dict:
        payload = {
            "database_id": database_id,
            "sql": sql,
            "runAsync": False,
            "queryLimit": row_limit,
        }
        if schema:
            payload["schema"] = schema
        if catalog:
            payload["catalog"] = catalog
        r = self._post("/api/v1/sqllab/execute/", json=payload)
        d = r.json()
        return {
            "columns": [c["column_name"] for c in d.get("columns", [])],
            "rows": d.get("data", []),
        }

    # --------------------------------------------------------------- import

    def import_dashboard(self, bundle_zip: bytes, filename: str = "bundle.zip") -> None:
        r = self._post(
            "/api/v1/dashboard/import/",
            files={"formData": (filename, bundle_zip, "application/zip")},
            data={"overwrite": "true", "passwords": "{}",
                  "ssh_tunnel_passwords": "{}"},
        )
        if not r.json().get("message") == "OK":
            raise SupersetAPIError(f"import response: {r.text[:300]}")

    # --------------------------------------------------------------- verify

    def get_dashboard(self, id_or_slug: str) -> dict:
        return self._get(f"/api/v1/dashboard/{id_or_slug}").json()["result"]

    def dashboard_charts(self, id_or_slug: str) -> list[dict]:
        r = self._get(f"/api/v1/dashboard/{id_or_slug}/charts")
        return r.json()["result"]

    def dashboard_datasets(self, id_or_slug: str) -> list[dict]:
        r = self._get(f"/api/v1/dashboard/{id_or_slug}/datasets")
        return r.json()["result"]

    def verify_dashboard(self, id_or_slug: str) -> dict:
        """Two-level check.

        datasets: every dataset's SQL is executed (LIMIT 5) — catches bad
        SQL, missing columns, and connection errors, the dominant failure
        class for generated dashboards.
        charts: each chart's saved data query is executed when possible.
        Freshly imported charts have no query_context until first rendered
        in a browser (by design — the bundle ships query_context null), so
        those report status "not_rendered_yet", which is NOT a failure.
        """
        datasets = []
        for ds in self.dashboard_datasets(id_or_slug):
            name = ds.get("table_name", "?")
            sql = ds.get("sql")
            if not sql:
                datasets.append({"dataset": name, "status": "ok",
                                 "detail": "physical table"})
                continue
            try:
                res = self.run_sql(
                    ds.get("database", {}).get("id"),
                    f"SELECT * FROM (\n{sql}\n) AS _ai_verify LIMIT 5",
                    schema=ds.get("schema") or None,
                    catalog=ds.get("catalog") or None,
                    row_limit=5,
                )
                status = "ok" if res["rows"] else "empty"
                datasets.append({"dataset": name, "status": status,
                                 "detail": f"{len(res['rows'])} sample rows"})
            except SupersetAPIError as e:
                datasets.append({"dataset": name, "status": "error",
                                 "detail": str(e)[:400]})

        charts = []
        for ch in self.dashboard_charts(id_or_slug):
            sid = ch.get("id") or ch.get("slice_id")
            name = ch.get("slice_name", str(sid))
            try:
                r = self.s.get(f"{self.base_url}/api/v1/chart/{sid}/data/",
                               timeout=self.timeout)
                if r.status_code >= 400:
                    if "query context" in r.text.lower():
                        charts.append(ChartStatus(
                            sid, name, "not_rendered_yet",
                            "runs on first dashboard open").__dict__)
                    else:
                        charts.append(ChartStatus(sid, name, "error",
                                                  r.text[:300]).__dict__)
                    continue
                d = r.json()
                rows = sum(len(q.get("data") or []) for q in d.get("result", []))
                charts.append(ChartStatus(sid, name, "ok" if rows else "empty",
                                          f"{rows} rows").__dict__)
            except Exception as e:  # noqa: BLE001 - surfaced to the agent
                charts.append(ChartStatus(sid, name, "error",
                                          str(e)[:300]).__dict__)
        return {"datasets": datasets, "charts": charts}
