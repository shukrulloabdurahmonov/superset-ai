#!/usr/bin/env python3
"""Launcher for the AI Analyst CLI that works WITHOUT a full Superset
install (needs only: requests, pyyaml, anthropic — see .venv-cli).

Importing `superset.ai_analyst` normally executes superset/__init__.py,
which requires the whole Superset dependency tree. This launcher registers
a stub `superset` parent package pointing at the source directory so only
the self-contained ai_analyst package is imported.
"""
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

if "superset" not in sys.modules:
    stub = types.ModuleType("superset")
    stub.__path__ = [str(REPO / "superset")]
    sys.modules["superset"] = stub

from superset.ai_analyst.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
