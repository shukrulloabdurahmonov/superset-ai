"""REST API for the AI Analyst chat.

POST /api/v1/ai_analyst/chat        {message, chat_id?, plan_mode?, attachments?}
                                    -> SSE stream (chat, text, tool,
                                       approval_request, done, error)
POST /api/v1/ai_analyst/apply       {chat_id, approval_id, approve} -> import
GET  /api/v1/ai_analyst/chats       -> current user's saved chats
GET  /api/v1/ai_analyst/chats/<id>  -> transcript + pending approvals
POST /api/v1/ai_analyst/chats/<id>/stop -> interrupt the running turn
DELETE /api/v1/ai_analyst/chats/<id>

Chats are persisted per user in ai_analyst_chat at the start and end of
every turn and after every apply, so they survive restarts, are resumable,
and show up in the chat list while a long first turn is still running. The in-memory _SESSIONS
dict is only a cache of live agent objects (single-worker assumption; the
DB copy is authoritative).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid

from flask import Response, current_app, g, request, stream_with_context
from flask_appbuilder.api import expose, protect, safe

from superset.ai_analyst import models
from superset.ai_analyst.agent import DEFAULT_MODEL, AnalystAgent
from superset.ai_analyst.service import InProcessSupersetService
from superset.views.base_api import BaseSupersetApi

logger = logging.getLogger(__name__)

_SESSIONS: dict[str, AnalystAgent] = {}
_SESSIONS_LOCK = threading.Lock()
# chat ids with a turn currently executing (same single-worker assumption
# as _SESSIONS; only ever advisory — used to badge chats in the UI)
_RUNNING: set[str] = set()
# live turns by chat id: {"agent", "user_id", "events"} — lets the stop
# endpoint interrupt a turn and close its SSE stream
_TURNS: dict[str, dict] = {}

MAX_REQUEST_BYTES = 20 * 1024 * 1024


def _api_key() -> str | None:
    return (
        current_app.config.get("AI_ANALYST_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )


def _current_user():
    return getattr(g.user, "_get_current_object", lambda: g.user)()


def _new_agent() -> AnalystAgent:
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
    agent.ui = []  # persisted transcript, mirrors what the page renders
    return agent


def _get_session(chat_id: str | None, user_id: int):
    """-> (chat_id, agent) or (None, None) if chat_id exists but isn't the
    user's. New id is minted when chat_id is None/unknown-empty."""
    with _SESSIONS_LOCK:
        if chat_id and chat_id in _SESSIONS:
            return chat_id, _SESSIONS[chat_id]
        if chat_id:
            row = models.load_chat(chat_id, user_id)
            if row is None:
                return None, None
            agent = _new_agent()
            agent.load_messages(json.loads(row.model_messages))
            agent.ui = json.loads(row.ui_transcript)
            _SESSIONS[chat_id] = agent
            return chat_id, agent
        cid = uuid.uuid4().hex
        agent = _new_agent()
        _SESSIONS[cid] = agent
        return cid, agent


def _persist(chat_id: str, user_id: int, agent: AnalystAgent,
             messages: list[dict] | None = None) -> None:
    title = "New chat"
    for m in agent.ui:
        if m.get("kind") == "user":
            title = (m.get("text") or "New chat")[:120]
            break
    try:
        models.save_chat(chat_id, user_id, title,
                         json.dumps(messages if messages is not None
                                    else agent.export_messages(),
                                    default=str),
                         json.dumps(agent.ui, default=str))
    except Exception:  # noqa: BLE001 - persistence must never kill a turn
        logger.exception("ai_analyst: failed to persist chat %s", chat_id)


def _stopped_messages(agent: AnalystAgent) -> list[dict]:
    """Message history safe to resume from after an interrupted turn: any
    trailing assistant tool_use blocks get a synthetic tool_result, since
    the API rejects histories with unanswered tool calls."""
    msgs = agent.export_messages()
    if msgs and msgs[-1].get("role") == "assistant":
        content = msgs[-1].get("content")
        ids = [
            b.get("id") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ] if isinstance(content, list) else []
        if ids:
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": i,
                 "content": "(interrupted: the user stopped this turn)"}
                for i in ids
            ]})
    return msgs


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
        if request.content_length and request.content_length > MAX_REQUEST_BYTES:
            return self.response_400(message="request too large (20 MB cap)")
        body = request.json or {}
        message = (body.get("message") or "").strip()
        if not message:
            return self.response_400(message="'message' is required")
        attachments = body.get("attachments") or []
        plan_mode = bool(body.get("plan_mode"))
        user = _current_user()
        chat_id, agent = _get_session(body.get("chat_id"), user.id)
        if agent is None:
            return self.response_404()

        agent.ui.append({
            "kind": "user", "text": message,
            **({"attachments": [a.get("name", "?") for a in attachments]}
               if attachments else {}),
        })
        # persist before the (possibly long) turn runs so the chat is
        # already listed in the sidebar and reachable from other windows;
        # model_messages catches up at the end-of-turn persist
        _persist(chat_id, user.id, agent)

        events: queue.Queue = queue.Queue()
        with _SESSIONS_LOCK:
            _RUNNING.add(chat_id)
            _TURNS[chat_id] = {"agent": agent, "user_id": user.id,
                               "events": events}

        def emit(event: str, payload: dict) -> None:
            events.put((event, payload))

        def on_text(t: str) -> None:
            agent.ui.append({"kind": "assistant", "text": t})
            emit("text", {"text": t})

        def on_tool(name: str, args: dict) -> None:
            short = {k: str(v)[:200] for k, v in args.items()}
            agent.ui.append({"kind": "tool", "name": name, "args": short})
            emit("tool", {"name": name, "args": short})

        def on_approval(aid: str, summary: str, spec_yaml: str) -> None:
            agent.ui.append({"kind": "approval", "approvalId": aid,
                             "summary": summary, "specYaml": spec_yaml,
                             "state": "pending"})
            emit("approval_request", {"approval_id": aid, "summary": summary,
                                      "spec_yaml": spec_yaml})

        def on_embed(payload: dict) -> None:
            agent.ui.append({"kind": "embed", **payload})
            emit("embed", payload)

        def on_plan(plan_markdown: str) -> None:
            agent.ui.append({"kind": "plan", "text": plan_markdown})
            emit("plan", {"text": plan_markdown})

        agent.on_text = on_text
        agent.on_tool = on_tool
        agent.on_approval = on_approval
        agent.on_embed = on_embed
        agent.on_plan = on_plan

        app = current_app._get_current_object()

        def run() -> None:
            with app.app_context():
                # the captured User belongs to the request thread's session;
                # writes in this thread (e.g. owner assignment during chart
                # import) need a session-local instance
                try:
                    from superset.extensions import db
                    g.user = db.session.merge(user, load=False)
                except Exception:  # noqa: BLE001
                    g.user = user
                try:
                    final = agent.chat(message, attachments=attachments,
                                       plan_mode=plan_mode)
                    emit("done", {"final": final})
                except ValueError as e:  # bad attachments etc.
                    agent.ui.append({"kind": "error", "text": str(e)})
                    emit("error", {"message": str(e)[:500]})
                except Exception as e:  # noqa: BLE001 - surfaced to the UI
                    logger.exception("ai_analyst chat turn failed")
                    agent.ui.append({"kind": "error", "text": str(e)[:500]})
                    emit("error", {"message": str(e)[:500]})
                finally:
                    # a stopped turn was already persisted (sanitized) and
                    # unregistered by the stop endpoint; a newer turn may
                    # own the registry entries by the time we get here
                    with _SESSIONS_LOCK:
                        if _TURNS.get(chat_id, {}).get("events") is events:
                            _TURNS.pop(chat_id, None)
                        if not agent.stopped:
                            _RUNNING.discard(chat_id)
                    if not agent.stopped:
                        _persist(chat_id, user.id, agent)
                    events.put(None)

        threading.Thread(target=run, daemon=True).start()

        def sse():
            yield f"event: chat\ndata: {json.dumps({'chat_id': chat_id})}\n\n"
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
        user = _current_user()
        chat_id, agent = _get_session(body.get("chat_id"), user.id)
        aid = body.get("approval_id")
        if agent is None or aid not in agent.pending:
            return self.response_400(message="unknown chat or approval id")

        def mark(state: str, url: str | None = None, detail: str | None = None):
            for m in agent.ui:
                if m.get("kind") == "approval" and m.get("approvalId") == aid:
                    m["state"] = state
                    if url:
                        m["url"] = url
                    if detail:
                        m["detail"] = detail

        if not body.get("approve"):
            agent.decline_pending(aid)
            mark("declined")
            _persist(chat_id, user.id, agent)
            return self.response(200, result={"status": "declined"})

        database_id = agent.pending[aid]["database_id"]
        spec_yaml = agent.pending[aid]["spec_yaml"]
        try:
            slug = agent.execute_pending(aid)
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            logger.exception("ai_analyst apply failed")
            mark("failed", detail=str(e)[:500])
            _persist(chat_id, user.id, agent)
            return self.response(500, message=str(e)[:500])
        models.upsert_spec(slug, database_id, spec_yaml)
        url = f"/superset/dashboard/{slug}/"
        mark("applied", url=url)
        # post-apply verification: run every dataset's SQL so failures are
        # caught immediately; feed problems back into the session so the
        # agent can repair on the next turn
        verification = None
        try:
            verification = agent.superset.verify_dashboard(slug)
            problems = [d for d in verification["datasets"]
                        if d["status"] == "error"]
            if problems:
                agent.messages.append({
                    "role": "user",
                    "content": "[system note] Post-apply verification found "
                               f"dataset errors on '{slug}': "
                               f"{json.dumps(problems)}. Propose a fix.",
                })
        except Exception:  # noqa: BLE001 - verification is best-effort
            logger.exception("ai_analyst post-apply verification failed")
        _persist(chat_id, user.id, agent)
        return self.response(200, result={
            "status": "applied", "slug": slug, "url": url,
            "verification": verification,
        })

    @expose("/chats/<chat_id>/stop", methods=("POST",))
    @protect()
    @safe
    def stop_chat(self, chat_id: str) -> Response:
        """Interrupt the running turn of a chat. The stop is immediate for
        the user: the chat is persisted as-is (with a sanitized message
        history), the SSE stream is closed, and the session is dropped so
        the next turn starts from the saved state. The turn's thread exits
        at its next step; a tool call already executing runs to completion
        in the background but its result is discarded."""
        user = _current_user()
        with _SESSIONS_LOCK:
            turn = _TURNS.get(chat_id)
            if turn is None or turn["user_id"] != user.id:
                turn = None
            else:
                _TURNS.pop(chat_id, None)
                _RUNNING.discard(chat_id)
                _SESSIONS.pop(chat_id, None)
        if turn is None:
            return self.response_404()
        agent = turn["agent"]
        agent.stopped = True
        agent.cancel_event.set()
        agent.ui.append({"kind": "error", "text": "Stopped by user."})
        _persist(chat_id, user.id, agent, messages=_stopped_messages(agent))
        turn["events"].put(("error", {"message": "Stopped by user."}))
        turn["events"].put(None)
        return self.response(200, result={"status": "stopped"})

    @expose("/chats", methods=("GET",))
    @protect()
    @safe
    def chats(self) -> Response:
        rows = models.list_chats(_current_user().id)
        with _SESSIONS_LOCK:
            for row in rows:
                row["in_progress"] = row["id"] in _RUNNING
        return self.response(200, result=rows)

    @expose("/chats/<chat_id>", methods=("GET",))
    @protect()
    @safe
    def get_chat(self, chat_id: str) -> Response:
        user = _current_user()
        cid, agent = _get_session(chat_id, user.id)
        if agent is None:
            return self.response_404()
        with _SESSIONS_LOCK:
            in_progress = cid in _RUNNING
        return self.response(200, result={
            "chat_id": cid,
            "in_progress": in_progress,
            "transcript": agent.ui,
            "pending": [
                {"approval_id": a, "summary": p["summary"], "slug": p["slug"]}
                for a, p in agent.pending.items()
            ],
        })

    @expose("/chats/<chat_id>", methods=("DELETE",))
    @protect()
    @safe
    def delete_chat(self, chat_id: str) -> Response:
        if not models.delete_chat(chat_id, _current_user().id):
            return self.response_404()
        with _SESSIONS_LOCK:
            _SESSIONS.pop(chat_id, None)
        return self.response(200, result={"status": "deleted"})
