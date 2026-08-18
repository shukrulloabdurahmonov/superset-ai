<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Superset AI — Apache Superset with a built-in AI Analyst

A fork of [Apache Superset](https://github.com/apache/superset) (base:
**6.1.0**) that adds an **AI chat which does the work of a data analyst**:
it explores your databases, answers questions with real SQL, and creates or
modifies dashboards and charts — deterministically, through a spec compiler,
never by hand-editing JSON.

Talk to it like a colleague:

> *"What data do we have?"* · *"Which region drives revenue, and is that
> changing?"* · *"Build a dashboard about regional sales performance"* ·
> *"Make that bar chart horizontal and add a gauge vs our $300 target"*

## What it does

- **Builds dashboards from a conversation.** The agent profiles your data
  first, proposes a plan you approve with one click, compiles it into a
  Superset import bundle, and imports it — 15 chart types, tabs, sections,
  native filters, per-chart captions. Nothing is applied without your
  explicit approval.
- **Modifies any dashboard.** Dashboards it built round-trip through their
  stored spec; dashboards it didn't build are reverse-engineered into a spec
  (with explicit warnings about anything lossy). Changed charts update in
  place; untouched charts stay untouched.
- **Answers data questions.** Read-only SQL through Superset's own query
  layer — the logged-in user's permissions always apply. Answers come back
  as prose + tables, or as real Superset charts created and embedded live in
  the chat.
- **Opens what it builds next to the chat.** One click docks the dashboard
  (or chart editor) beside the conversation; keep prompting and every
  applied change refreshes the preview.
- **Remembers your data.** A per-database **data catalog** (schemas, tables,
  columns, row counts, date ranges — refreshed in the background every 2h,
  no LLM tokens spent) plus agent-written notes about quirks, so every new
  conversation starts already knowing your warehouse.
- **Chats are saved per user** (private, resumable, survive restarts), with
  image + CSV/text attachments the agent can actually read.

## Why a fork, and why it's robust

- **The LLM never writes Superset internals.** It writes a small, versioned
  YAML spec; a deterministic compiler emits the import bundle. Malformed
  dashboards are structurally impossible, and every generated object is
  content-versioned so re-applies update in place without duplicating.
- **Everything runs in-process with your RBAC.** Metadata, SQL, imports —
  all through Superset's own security manager and official import commands.
  The Anthropic API key stays server-side, always.
- **Tiny upstream footprint.** All product code lives in
  [`superset/ai_analyst/`](superset/ai_analyst/) and
  [`superset-frontend/src/pages/AiAnalyst/`](superset-frontend/src/pages/AiAnalyst/);
  exactly five upstream files carry small registration hooks, so merging new
  Superset releases stays mechanical.

## Quickstart (development)

```bash
git clone https://github.com/shukrulloabdurahmonov/superset-ai.git
cd superset-ai

# enable the feature (both files are gitignored)
echo "AI_ANALYST_ENABLED = True" > docker/pythonpath_dev/superset_config_docker.py
echo "ANTHROPIC_API_KEY=sk-ant-..." >> docker/.env-local

SUPERSET_LOAD_EXAMPLES=no docker compose \
  -f docker-compose-light.yml -f docker-compose-light-host.yml up -d
docker exec superset-ai-superset-light-1 superset init   # sync permissions
```

Open http://localhost:8088 (admin/admin) → **AI Analyst** in the menu.

## Production

```bash
docker build --target lean -t superset-ai:0.1.0 .
```

Config (`superset_config.py`):

```python
AI_ANALYST_ENABLED = True
AI_ANALYST_API_KEY = "sk-ant-..."     # or env ANTHROPIC_API_KEY
AI_ANALYST_MODEL = "claude-opus-5"    # default; any Claude model id
AI_ANALYST_CATALOG_REFRESH_HOURS = 2  # 0 disables the background refresher
```

Full details — architecture, REST API, security model, deployment limits,
and the upstream-upgrade playbook — in
**[superset/ai_analyst/README.md](superset/ai_analyst/README.md)**.

## Everything else

This is otherwise a faithful Apache Superset 6.1.0 — all upstream features,
docs and configuration apply unchanged: see the
[Apache Superset documentation](https://superset.apache.org/docs/intro).

## License & attribution

Apache License 2.0, same as upstream. Powered by
[Apache Superset](https://superset.apache.org/); this fork is a separate
project and is not endorsed by the Apache Software Foundation. "Apache
Superset" is a trademark of the ASF.
