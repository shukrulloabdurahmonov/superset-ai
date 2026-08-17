"""Terminal chat harness for the AI Analyst agent (dogfooding, Phase 1).

Run via scripts/ai_analyst_cli.py (works without a full Superset install):

    ANTHROPIC_API_KEY=... \
    SUPERSET_URL=http://localhost:8088 \
    SUPERSET_USERNAME=admin SUPERSET_PASSWORD=admin \
    python scripts/ai_analyst_cli.py

The apply step always asks for confirmation in the terminal.
"""
from __future__ import annotations

import os
import sys

from .agent import DEFAULT_MODEL, AnalystAgent
from .superset_client import SupersetClient


def _approve(spec_yaml: str, summary: str) -> bool:
    print("\n=== APPLY REQUEST " + "=" * 50)
    print(summary.strip())
    print("-" * 68)
    lines = spec_yaml.strip().splitlines()
    print("\n".join(lines[:40]))
    if len(lines) > 40:
        print(f"... ({len(lines) - 40} more lines)")
    print("=" * 68)
    return input("Apply this to Superset? [y/N] ").strip().lower() == "y"


def main() -> int:
    url = os.environ.get("SUPERSET_URL", "http://localhost:8088")
    username = os.environ.get("SUPERSET_USERNAME", "admin")
    password = os.environ.get("SUPERSET_PASSWORD", "admin")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    print(f"connecting to {url} as {username} ...")
    superset = SupersetClient(url, username, password)
    agent = AnalystAgent(
        superset,
        model=os.environ.get("AI_ANALYST_MODEL", DEFAULT_MODEL),
        approve=_approve,
        on_text=lambda t: print(f"\n{t}"),
        on_tool=lambda name, args: print(
            f"  · {name}({', '.join(f'{k}={str(v)[:60]}' for k, v in args.items())})"
        ),
    )
    print("AI Analyst ready. Ctrl-D or 'exit' to quit.\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user:
            continue
        if user.lower() in ("exit", "quit"):
            return 0
        try:
            agent.chat(user)
        except Exception as e:  # noqa: BLE001 - REPL must survive errors
            print(f"[error] {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
