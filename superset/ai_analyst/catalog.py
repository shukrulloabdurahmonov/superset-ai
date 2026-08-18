"""Data-catalog snapshots.

The catalog a chat agent reads has two parts:
- a STRUCTURAL snapshot (this module): schemas, tables, columns + types,
  row counts and date ranges — generated deterministically through
  Superset's metadata layer, no LLM involved. Refreshed in the background
  every AI_ANALYST_CATALOG_REFRESH_HOURS (default 2) for every database
  that already has a catalog row, and lazily on first use.
- agent NOTES (models.AiAnalystCatalog.notes): semantic quirks the agent
  learned (value meanings, partial periods, join hints). Never touched by
  the refresher.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_SCHEMAS = 20
MAX_TABLES_PER_SCHEMA = 100
MAX_PROFILED_TABLES = 50  # row counts + date ranges run real queries

_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "sys", "performance_schema"}

_started = False


def generate_snapshot(database) -> str:
    """Structural snapshot of one database as markdown."""
    from superset.sql.parse import Table

    lines = [
        f"# {database.database_name} ({database.backend}, "
        f"database_id {database.id})",
        f"_structural snapshot, generated "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}_",
        "",
    ]
    schemas = sorted(database.get_all_schema_names())
    schemas = [s for s in schemas if s not in _SYSTEM_SCHEMAS][:MAX_SCHEMAS]
    profiled = 0
    for schema in schemas:
        try:
            tables = sorted(
                t[0] for t in database.get_all_table_names_in_schema(
                    catalog=None, schema=schema)
            )[:MAX_TABLES_PER_SCHEMA]
        except Exception as e:  # noqa: BLE001 - keep going per schema
            lines.append(f"## {schema}\n(unreadable: {str(e)[:120]})\n")
            continue
        lines.append(f"## {schema} — {len(tables)} table(s)")
        for table in tables:
            try:
                cols = database.get_columns(Table(table, schema))
            except Exception as e:  # noqa: BLE001
                lines.append(f"### {schema}.{table}\n(unreadable: {str(e)[:120]})")
                continue
            col_desc = ", ".join(
                f"{c['column_name']} {str(c.get('type', '?')).split('(')[0]}"
                for c in cols
            )
            lines.append(f"### {schema}.{table}")
            lines.append(f"columns: {col_desc}")
            dttm_cols = [
                c["column_name"] for c in cols
                if c.get("is_dttm")
                or str(c.get("type", "")).upper().startswith(
                    ("DATE", "TIMESTAMP", "DATETIME"))
            ]
            if profiled < MAX_PROFILED_TABLES:
                profiled += 1
                try:
                    df = database.get_df(
                        f'SELECT COUNT(*) AS n FROM "{schema}"."{table}"',
                        schema=schema,
                    )
                    lines.append(f"rows: ~{int(df.iloc[0]['n'])}")
                    for col in dttm_cols[:2]:
                        df = database.get_df(
                            f'SELECT MIN("{col}") AS lo, MAX("{col}") AS hi '
                            f'FROM "{schema}"."{table}"',
                            schema=schema,
                        )
                        lines.append(
                            f"{col} range: {df.iloc[0]['lo']} → {df.iloc[0]['hi']}"
                        )
                except Exception as e:  # noqa: BLE001 - stats are best-effort
                    lines.append(f"(stats unavailable: {str(e)[:100]})")
            lines.append("")
    if profiled >= MAX_PROFILED_TABLES:
        lines.append(f"_(row stats limited to {MAX_PROFILED_TABLES} tables)_")
    return "\n".join(lines)


def refresh_database(database_id: int) -> bool:
    """Regenerate the structural snapshot for one database."""
    from superset.ai_analyst import models
    from superset.daos.database import DatabaseDAO

    database = DatabaseDAO.find_by_id(database_id)
    if database is None:
        return False
    doc = generate_snapshot(database)
    models.save_catalog(database_id, doc=doc)
    return True


def refresh_all() -> int:
    """Refresh every database that already has a catalog row."""
    from superset.ai_analyst import models

    n = 0
    for database_id in models.catalog_database_ids():
        try:
            if refresh_database(database_id):
                n += 1
        except Exception:  # noqa: BLE001 - one bad db must not stop the rest
            logger.exception("catalog refresh failed for database %s",
                             database_id)
    return n


def start_background_refresher(app, interval_hours: float) -> None:
    """Daemon thread: refresh_all() every interval_hours."""
    global _started  # noqa: PLW0603 - single-start guard
    if _started or interval_hours <= 0:
        return
    _started = True

    def loop() -> None:
        while True:
            time.sleep(interval_hours * 3600)
            try:
                with app.app_context():
                    n = refresh_all()
                    logger.info("ai_analyst: refreshed %s data catalog(s)", n)
            except Exception:  # noqa: BLE001 - the loop must survive
                logger.exception("ai_analyst catalog refresh cycle failed")

    threading.Thread(target=loop, daemon=True,
                     name="ai-analyst-catalog-refresh").start()
    logger.info("ai_analyst: catalog refresher started (every %sh)",
                interval_hours)
