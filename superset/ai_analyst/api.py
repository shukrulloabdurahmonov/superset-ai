"""REST API for the AI Analyst chat.

POST /api/v1/ai_analyst/chat   {message, session_id?} -> SSE event stream
POST /api/v1/ai_analyst/apply  {session_id, approval_id, approve} -> import
GET  /api/v1/ai_analyst/session/<id> -> pending approvals (UI resync)

SSE event types: session (id), text, tool, approval_request, done, error.

Sessions are in-process (one worker) for the MVP; the chat itself is
stateless per turn, so a lost session only loses conversational context.
Applies are approval-gated: apply_spec parks the compiled bundle and the
import runs only when the user posts /apply with approve=true.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid

from flask import Response, current_app, request, stream_with_context
from flask_appbuilder.api import expose, protect, safe

from superset.ai_analyst import models
from superset.ai_analyst.agent import DEFAULT_MODEL, AnalystAgent
from superset.ai_analyst.service import InProcessSupersetService
from superset.views.base_api import BaseSupersetApi

logger = logging.getLogger(__name__)

_SESSIONS: dict[str, AnalystAgent] = {}
_SESSIONS_LOCK = threading.Lock()


def _api_key() -> str | None:
    return (
        current_app.config.get("AI_ANALYST_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )


def _get_or_create_session(session_id: str | None) -> tuple[str, AnalystAgent]:
    with _SESSIONS_LOCK:
        if session_id and session_id in _SESSIONS:
            return session_id, _SESSIONS[session_id]
        sid = session_id or uuid.uuid4().hex[:16]
        agent = AnalystAgent(
            InProcessSupersetService(),
            api_key=_api_key(),
            model=current_app.config.get("AI_ANALYST_MODEL", DEFAULT_MODEL),
            defer_apply=True,
        )
        try:
            agent.specs.update(models.all_specs())  # enable round-trip edits
        except Exception:  # noqa: BLE001 - table may not exist yet on first boot
            logger.warning("ai_analyst_spec table not readable yet")
        _SESSIONS[sid] = agent
        return sid, agent


class AiAnalystRestApi(BaseSupersetApi):
    resource_name = "ai_analyst"
    allow_browser_login = True
    class_permission_name = "AiAnalyst"
    openapi_spec_tag = "AI Analyst"

    @expose("/chat", methods=("POST",))
    @protect()
    @safe
    def chat(self) -> Response:
        """Run one chat turn; stream agent events as SSE."""
        if not _api_key():
            return self.response(500, message="ANTHROPIC_API_KEY / "
                                 "AI_ANALYST_API_KEY is not configured")
        body = request.json or {}
        message = (body.get("message") or "").strip()
        if not message:
            return self.response_400(message="'message' is required")
        sid, agent = _get_or_create_session(body.get("session_id"))

        events: queue.Queue = queue.Queue()

        def emit(event: str, payload: dict) -> None:
            events.put((event, payload))

        agent.on_text = lambda t: emit("text", {"text": t})
        agent.on_tool = lambda name, args: emit(
            "tool", {"name": name,
                     "args": {k: str(v)[:200] for k, v in args.items()}}
        )
        agent.on_approval = lambda aid, summary, spec_yaml: emit(
            "approval_request",
            {"approval_id": aid, "summary": summary, "spec_yaml": spec_yaml},
        )

        app = current_app._get_current_object()
        # keep the caller's identity for RBAC inside the worker thread;
        # g.user can be a request-bound LocalProxy — capture the real object
        from flask import g
        user = getattr(g.user, "_get_current_object", lambda: g.user)()

        def run() -> None:
            with app.app_context():
                g.user = user
                try:
                    final = agent.chat(message)
                    emit("done", {"final": final})
                except Exception as e:  # noqa: BLE001 - surfaced to the UI
                    logger.exception("ai_analyst chat turn failed")
                    emit("error", {"message": str(e)[:500]})
                finally:
                    events.put(None)

        threading.Thread(target=run, daemon=True).start()

        def sse():
            yield f"event: session\ndata: {json.dumps({'session_id': sid})}\n\n"
            while True:
                item = events.get()
                if item is None:
                    break
                event, payload = item
                yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"

        return Response(
            stream_with_context(sse()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @expose("/apply", methods=("POST",))
    @protect()
    @safe
    def apply(self) -> Response:
        """Execute (or decline) a pending, user-approved apply."""
        body = request.json or {}
        sid = body.get("session_id")
        aid = body.get("approval_id")
        agent = _SESSIONS.get(sid or "")
        if agent is None or aid not in agent.pending:
            return self.response_400(message="unknown session or approval id")
        if not body.get("approve"):
            agent.decline_pending(aid)
            return self.response(200, result={"status": "declined"})
        database_id = agent.pending[aid]["database_id"]
        spec_yaml = agent.pending[aid]["spec_yaml"]
        try:
            slug = agent.execute_pending(aid)
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            logger.exception("ai_analyst apply failed")
            return self.response(500, message=str(e)[:500])
        models.upsert_spec(slug, database_id, spec_yaml)
        return self.response(200, result={
            "status": "applied", "slug": slug,
            "url": f"/superset/dashboard/{slug}/",
        })

    @expose("/session/<session_id>", methods=("GET",))
    @protect()
    @safe
    def session(self, session_id: str) -> Response:
        agent = _SESSIONS.get(session_id)
        if agent is None:
            return self.response_404()
        return self.response(200, result={
            "pending": [
                {"approval_id": aid, "summary": p["summary"], "slug": p["slug"]}
                for aid, p in agent.pending.items()
            ],
            "turns": len(agent.messages),
        })
