"""Read-only guard for agent-issued SQL.

The agent's run_sql tool must never mutate anything. Defense in depth:
this validator rejects everything but a single SELECT statement, and the
execution path additionally applies the caller's Superset RBAC + row limit.
"""
from __future__ import annotations

import re


class SQLGuardError(ValueError):
    pass


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")

# statements that must never run, checked as standalone words anywhere
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|"
    r"call|execute|copy|vacuum|analyze|refresh|set|use|comment)\b",
    re.I,
)


def assert_read_only(sql: str) -> str:
    """Validate and return the (stripped) SQL; raise SQLGuardError otherwise."""
    stripped = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", sql)).strip()
    if not stripped:
        raise SQLGuardError("empty SQL")
    # single statement only: allow one optional trailing semicolon
    body = stripped.rstrip(";").strip()
    if ";" in body:
        raise SQLGuardError("only a single SQL statement is allowed")
    head = body.split(None, 1)[0].lower()
    if head not in ("select", "with", "show", "describe", "explain"):
        raise SQLGuardError(
            f"only read-only queries are allowed (statement starts with '{head}')"
        )
    if head in ("select", "with") and _FORBIDDEN.search(body):
        # WITH ... INSERT INTO / CREATE TABLE AS etc.
        m = _FORBIDDEN.search(body)
        raise SQLGuardError(f"forbidden keyword in query: '{m.group(0)}'")
    return body
