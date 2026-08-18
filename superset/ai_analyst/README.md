# AI Analyst

A chat agent built into this Superset fork: it explores your data, answers
questions, and creates/modifies dashboards — deterministically, through a
spec compiler, never by hand-editing charts.

## Architecture

```
chat (React page / CLI / REST)
  └─ AnalystAgent (agent.py)            Claude Tool Runner loop
       ├─ metadata + SQL tools          RBAC of the logged-in user applies
       │    ├─ InProcessSupersetService (service.py)   inside the web app
       │    └─ SupersetClient (superset_client.py)     REST, any instance
       ├─ validate_spec / apply_spec    -> Compiler (compiler/core.py)
       │      spec YAML -> import bundle zip; the LLM only ever writes specs
       └─ verify_dashboard              dataset SQL checks + chart status
```

Key properties:
- **run_sql is read-only** (sql_guard.py) on top of Superset RBAC
  (`security_manager.raise_for_access`).
- **apply is approval-gated**: the web API parks the compiled bundle and
  imports only after the user posts approval (`defer_apply`); the CLI asks
  interactively.
- **Charts/datasets are content-versioned** (uuid5 of content hash) because
  Superset's dashboard import never overwrites existing charts/datasets —
  changed objects arrive as new ones, unchanged keep their uuid.
- Specs are persisted in the `ai_analyst_spec` table (models.py) so
  "modify my dashboard" round-trips: spec → edit → recompile → re-import.
- Dashboards with NO stored spec are handled by the **reverse compiler**
  (reverse.py): dashboard model → best-effort spec + explicit warnings about
  anything lossy (unsupported viz types, physical-table datasets, missing
  slug). Applying a recovered spec replaces the dashboard with what the spec
  expresses.
- Every apply is followed by dataset-level verification; failures are fed
  back into the chat session so the agent proposes a repair.

## Dev setup

```bash
# boot (backend serves built assets; webpack dev server not needed)
SUPERSET_LOAD_EXAMPLES=no docker compose \
  -f docker-compose-light.yml -f docker-compose-light-host.yml up -d

# enable the feature (both files are gitignored):
echo "AI_ANALYST_ENABLED = True" > docker/pythonpath_dev/superset_config_docker.py
echo "ANTHROPIC_API_KEY=sk-ant-..." >> docker/.env-local

# the dev image doesn't bake the extra yet:
docker exec superset-ai-superset-light-1 /app/.venv/bin/pip install 'anthropic>=0.116.0'
docker compose -f docker-compose-light.yml -f docker-compose-light-host.yml \
  restart superset-light
docker exec superset-ai-superset-light-1 superset init   # sync permissions
```

http://localhost:8088 (admin/admin).

## REST API

- `POST /api/v1/ai_analyst/chat`
  `{message, chat_id?, plan_mode?, attachments?: [{name, mime, data_b64}]}`
  → SSE stream (`chat`, `text`, `tool`, `approval_request`, `done`, `error`).
  Attachments: png/jpeg/gif/webp images (≤4 MB, sent to the model as vision
  input) and text/CSV/JSON/SQL/YAML files (≤100 KB text used as context).
  `plan_mode` makes build/modify tasks present a plan and stop for
  confirmation before anything is created.
- `POST /api/v1/ai_analyst/apply` `{chat_id, approval_id, approve}`
- `GET/DELETE /api/v1/ai_analyst/chats[/<id>]` — conversations are persisted
  per user (`ai_analyst_chat`) after every turn and survive restarts; the
  UI's sidebar, transcript restore, and the docked dashboard preview
  (`?standalone=3` iframe) are built on these.

Note: after adding/renaming API endpoints, run `superset init` so
Flask-AppBuilder grants the new method permissions to roles.

## Data catalog

Per-database snapshot the agent reads instead of re-exploring every request
(`ai_analyst_catalog`): a STRUCTURAL part (schemas/tables/columns/row
counts/date ranges) generated deterministically by `catalog.py` — refreshed
in a background thread every `AI_ANALYST_CATALOG_REFRESH_HOURS` (default 2,
0 disables) for databases that have a catalog row, and lazily on first
`get_data_catalog` call — plus agent-written NOTES (semantic quirks) that
refreshes never touch.

## Plan mode

With "Plan first" on, build/modify requests call the `propose_plan` tool:
the chat renders the plan with an "Approve & build" button; nothing is
compiled until the user approves (button or free text).

Config (superset_config.py): `AI_ANALYST_ENABLED`, `AI_ANALYST_MODEL`
(default `claude-opus-5`), `AI_ANALYST_API_KEY` (falls back to env
`ANTHROPIC_API_KEY`; never exposed to the frontend).

## CLI (works against ANY Superset, no install needed)

```bash
ANTHROPIC_API_KEY=... SUPERSET_URL=http://localhost:8088 \
SUPERSET_USERNAME=admin SUPERSET_PASSWORD=admin \
python scripts/ai_analyst_cli.py
```

## Tests

```bash
python -m pytest tests/ai_analyst/ --noconftest   # compiler golden files
```

The golden bundles were import-tested and render-verified against a real
Superset instance; the compiler must reproduce them byte-identically.

## Production install

```bash
docker build --target lean -t superset-ai:<version> .
```

The image bakes `requirements/ai-analyst.txt` (the `anthropic` SDK), so it
runs anywhere stock Superset runs. Minimum production config
(`superset_config.py`):

```python
AI_ANALYST_ENABLED = True
AI_ANALYST_API_KEY = "..."          # or env ANTHROPIC_API_KEY (server-side only)
# AI_ANALYST_MODEL = "claude-opus-5"
# AI_ANALYST_CATALOG_REFRESH_HOURS = 2   # 0 disables the background refresher
```

Then run `superset init` once so Flask-AppBuilder grants the AiAnalyst
permissions to roles.

**Known deployment limits (MVP):**
- Live chat sessions are held in the web worker's memory (the DB copy is
  authoritative and chats survive restarts, but a multi-worker gunicorn
  needs sticky sessions so a conversation keeps hitting the same worker).
- The catalog refresher runs in every worker; refreshes are idempotent, so
  extra workers just duplicate work.
- SSE streaming: disable response buffering on the reverse proxy for
  `/api/v1/ai_analyst/chat` (nginx: `proxy_buffering off`; the response
  already sends `X-Accel-Buffering: no`).

## Security model

- `run_sql` is guarded twice: sql_guard.py (single read-only statement) and
  `security_manager.raise_for_access(database=..., sql=...)` — the logged-in
  user's RBAC. `describe_table` checks table-level access; dataset SQL
  verification and chart data run through Superset's own layers.
- The Anthropic key lives only in server config/env; it is never sent to the
  frontend and never appears in chat transcripts.
- Writes are constrained: `apply_spec` (dashboards) is approval-gated in the
  UI; `create_chart` is additive-only; both go through Superset's official
  import commands, so ownership and audit behave like a manual import.
- Chats are private per user (owner checks on every chat endpoint).

## Upgrading with upstream Superset

1. `git fetch upstream --tags`
2. `git merge <new-tag>` — conflicts should only appear in the five
   touch-point files below; re-apply the small fork-local blocks.
3. Rebuild frontend + image, run `superset init`, run
   `pytest tests/ai_analyst --noconftest`.

## Fork policy

All AI Analyst code lives in `superset/ai_analyst/` and
`superset-frontend/src/aiAnalyst/`. Upstream touch-points (keep this list
current):
1. `superset/initialization/__init__.py` — API + view registration, catalog
   refresher start (config-gated)
2. `superset/config.py` — `AI_ANALYST_*` defaults
3. `pyproject.toml` — `ai-analyst` optional dependency
4. `superset-frontend/src/views/routes.tsx` — `/aianalyst/` route
5. `Dockerfile` — installs `requirements/ai-analyst.txt` in lean + dev stages
   (plus a fork banner at the top of the root `README.md`)

When merging an upstream Superset release tag, conflicts should only ever
appear in these files.
