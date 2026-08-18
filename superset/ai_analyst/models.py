"""Spec store: the YAML spec behind every AI-built dashboard, keyed by slug.

This is what makes "modify my dashboard" round-trip: load spec -> edit ->
recompile -> re-import (content-versioned uuids keep it an in-place update).

The table is created lazily with checkfirst instead of an alembic migration:
a fork-local migration would race upstream's migration heads on every
Superset release merge; a single additive table avoids that entirely.
"""
from __future__ import annotations

from flask_appbuilder import Model
from sqlalchemy import Column, DateTime, Integer, String, Text, func, inspect


class AiAnalystSpec(Model):
    __tablename__ = "ai_analyst_spec"

    id = Column(Integer, primary_key=True)
    slug = Column(String(255), unique=True, nullable=False)
    database_id = Column(Integer, nullable=False)
    spec_yaml = Column(Text, nullable=False)
    created_on = Column(DateTime, server_default=func.now())
    changed_on = Column(DateTime, server_default=func.now(), onupdate=func.now())


def ensure_table() -> None:
    from superset.extensions import db

    if not inspect(db.engine).has_table(AiAnalystSpec.__tablename__):
        AiAnalystSpec.__table__.create(db.engine, checkfirst=True)


def upsert_spec(slug: str, database_id: int, spec_yaml: str) -> None:
    from superset.extensions import db

    row = db.session.query(AiAnalystSpec).filter_by(slug=slug).one_or_none()
    if row is None:
        row = AiAnalystSpec(slug=slug, database_id=database_id,
                            spec_yaml=spec_yaml)
        db.session.add(row)
    else:
        row.database_id = database_id
        row.spec_yaml = spec_yaml
    db.session.commit()


def all_specs() -> dict[str, str]:
    from superset.extensions import db

    return {
        row.slug: row.spec_yaml
        for row in db.session.query(AiAnalystSpec).all()
    }
