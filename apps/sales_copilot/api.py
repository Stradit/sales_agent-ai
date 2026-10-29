"""FastAPI router for the Sales Copilot (README §12, §20.2). Mounted by the main
api.py under /api/copilot. Every endpoint is scoped to the calling user."""

import json
import re
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from apps.sales_copilot import chat, embed, exports, ingest, llm, pipeline, retrieve, sync
from db.connection import get_session
from db.models.user import User

router = APIRouter(prefix="/api/copilot", tags=["Sales Copilot"])



class ChatIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = None
    context: Dict[str, Optional[int]] = Field(default_factory=dict)


class FeedbackIn(BaseModel):
    value: int = Field(..., ge=-1, le=1)
    note: Optional[str] = Field(None, max_length=500)


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
    persona_id: Optional[int] = None
    account_id: Optional[int] = None


class SessionPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=120)
    pinned: Optional[bool] = None


class PrefsIn(BaseModel):
    memory_enabled: Optional[bool] = None
    answer_style: Optional[str] = Field(None, pattern="^(brief|balanced|detailed)$")


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


@router.post("/chat")
def post_chat(body: ChatIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    ctx = {k: v for k, v in (body.context or {}).items() if k in ("account_id", "persona_id") and v}
    acl = retrieve.acl_account_ids(s, user)
    if ctx.get("account_id") and ctx["account_id"] not in acl:
        ctx.pop("account_id")
    try:
        return chat.handle_message(s, user, body.text, body.session_id, ctx)
    except Exception:
        s.rollback()
        raise


@router.get("/sessions")
def list_sessions(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    rows = s.execute(text("""
        SELECT id, title, pinned, last_message_at, created_at FROM copilot_sessions
        WHERE user_id = :u AND archived_at IS NULL
        ORDER BY pinned DESC, coalesce(last_message_at, created_at) DESC LIMIT 100"""), {"u": user.id}).fetchall()
    return [{"id": str(r[0]), "title": r[1], "pinned": r[2], "last_message_at": r[3], "created_at": r[4]} for r in rows]


@router.get("/sessions/{sid}")
def get_session_messages(sid: str, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    sess = s.execute(text("SELECT id, title, active_entities FROM copilot_sessions WHERE id = :s AND user_id = :u"),
                     {"s": sid, "u": user.id}).fetchone()
    if not sess:
        raise HTTPException(404, "Chat not found.")
    rows = s.execute(text("""SELECT id, role, content, mode, intent, citations, extras, feedback, created_at
                             FROM copilot_messages WHERE session_id = :s ORDER BY id"""), {"s": sid}).fetchall()
    return {"id": str(sess[0]), "title": sess[1], "focus": sess[2] or [],
            "messages": [{"id": r[0], "role": r[1], "content": r[2], "mode": r[3], "intent": r[4],
                          "citations": r[5] or [], "extras": r[6] or {}, "feedback": r[7], "created_at": r[8]}
                         for r in rows]}


@router.patch("/sessions/{sid}")
def patch_session(sid: str, body: SessionPatch, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    res = s.execute(text("""UPDATE copilot_sessions SET title = coalesce(:t, title), pinned = coalesce(:p, pinned)
                            WHERE id = :s AND user_id = :u"""),
                    {"t": body.title, "p": body.pinned, "s": sid, "u": user.id})
    s.commit()
    if not res.rowcount:
        raise HTTPException(404, "Chat not found.")
    return {"ok": True}


@router.delete("/sessions/{sid}")
def delete_session(sid: str, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    res = s.execute(text("DELETE FROM copilot_sessions WHERE id = :s AND user_id = :u"), {"s": sid, "u": user.id})
    s.commit()
    if not res.rowcount:
        raise HTTPException(404, "Chat not found.")
    return {"ok": True}


@router.post("/messages/{mid}/feedback")
def feedback(mid: int, body: FeedbackIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    res = s.execute(text("""UPDATE copilot_messages m SET feedback = :v, feedback_note = :n
                            FROM copilot_sessions ss WHERE m.id = :m AND ss.id = m.session_id AND ss.user_id = :u"""),
                    {"v": body.value, "n": body.note, "m": mid, "u": user.id})
    s.commit()
    if not res.rowcount:
        raise HTTPException(404, "Message not found.")
    return {"ok": True}


@router.get("/quota")
def quota(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    return llm.quota_status(s, user.id)


@router.get("/notes")
def list_notes(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    acl = retrieve.acl_account_ids(s, user)
    rows = s.execute(text("""
        SELECT m.id, m.kind, m.text, m.persona_id, coalesce(p.full_name, p.display_name), m.account_id,
               a.display_name, m.pinned, m.expires_at, m.use_count, m.created_at
        FROM copilot_memories m
        LEFT JOIN personas p ON p.id = m.persona_id LEFT JOIN accounts a ON a.id = m.account_id
        WHERE m.user_id = :u AND m.deleted_at IS NULL AND (m.account_id IS NULL OR m.account_id = ANY(:acl))
        ORDER BY m.pinned DESC, m.created_at DESC"""), {"u": user.id, "acl": acl}).fetchall()
    return [{"id": r[0], "kind": r[1], "text": r[2], "persona_id": r[3], "persona": r[4], "account_id": r[5],
             "account": r[6], "pinned": r[7], "expires_at": r[8], "use_count": r[9], "created_at": r[10]} for r in rows]


@router.post("/notes")
def create_note(body: NoteIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    persona = None
    if body.persona_id:
        r = s.execute(text("SELECT id, account_id FROM personas WHERE id = :p"), {"p": body.persona_id}).fetchone()
        if not r or r[1] not in retrieve.acl_account_ids(s, user):
            raise HTTPException(404, "Person not found.")
        persona = {"id": r[0], "account_id": r[1]}
    note = chat.save_note(s, user.id, body.text, persona, [body.account_id] if body.account_id else [])
    s.commit()
    return note


@router.delete("/notes/{nid}")
def delete_note(nid: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    res = s.execute(text("UPDATE copilot_memories SET deleted_at = now() WHERE id = :n AND user_id = :u AND deleted_at IS NULL"),
                    {"n": nid, "u": user.id})
    s.commit()
    if not res.rowcount:
        raise HTTPException(404, "Note not found.")
    return {"ok": True}


@router.get("/prefs")
def get_prefs(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    return chat._prefs(s, user.id)


@router.put("/prefs")
def put_prefs(body: PrefsIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    s.execute(text("""INSERT INTO copilot_user_prefs (user_id, memory_enabled, answer_style)
                      VALUES (:u, coalesce(:m, true), coalesce(:st, 'balanced'))
                      ON CONFLICT (user_id) DO UPDATE SET memory_enabled = coalesce(:m, copilot_user_prefs.memory_enabled),
                        answer_style = coalesce(:st, copilot_user_prefs.answer_style), updated_at = now()"""),
              {"u": user.id, "m": body.memory_enabled, "st": body.answer_style})
    s.commit()
    return chat._prefs(s, user.id)


@router.post("/chat/stream")
async def post_chat_stream(body: ChatIn, request: Request, user: User = Depends(auth.get_current_user)):
    """Server-sent events: status → meta → token* → done (or error). The DB session is
    created here, not via Depends: FastAPI closes yield-dependencies before a streamed
    response finishes. On client disconnect (Stop), the generator is closed, which saves
    the partial answer and finalizes the quota row."""
    s = get_session()
    acl = retrieve.acl_account_ids(s, user)
    ctx = {k: v for k, v in (body.context or {}).items() if k in ("account_id", "persona_id") and v}
    if ctx.get("account_id") and ctx["account_id"] not in acl:
        ctx.pop("account_id")
    gen = chat.stream_message(s, user, body.text, body.session_id, ctx)

    def _next():
        try:
            return next(gen)
        except StopIteration:
            return None

    async def events():
        try:
            while True:
                item = await run_in_threadpool(_next)
                if item is None:
                    break
                ev, data = item
                yield f"event: {ev}\ndata: {json.dumps(data, default=str)}\n\n"
                if await request.is_disconnected():
                    break
        except Exception as e:   # surface to the client instead of a broken stream
            s.rollback()
            yield f"event: error\ndata: {json.dumps({'detail': str(e)[:300]})}\n\n"
        finally:
            await run_in_threadpool(gen.close)
            s.close()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/entities")
def entities(q: str = "", user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """@mention autocomplete: people and accounts the user can access (no contact data)."""
    q = q.strip()
    if not q:
        return []
    acl = retrieve.acl_account_ids(s, user)
    like, starts = f"%{q}%", f"{q}%"
    # A whole-word match ranks first: "vince" -> Robin Vince before Vincelle B.
    wordre = r"\m" + re.escape(q) + r"\M"
    people = s.execute(text("""
        SELECT p.id, coalesce(p.full_name, p.display_name), p.title, a.display_name, p.account_id
        FROM personas p JOIN accounts a ON a.id = p.account_id
        WHERE p.account_id = ANY(:acl) AND coalesce(p.full_name, p.display_name) ILIKE :like
        ORDER BY (coalesce(p.full_name, p.display_name) ~* :wordre) DESC,
                 (coalesce(p.full_name, p.display_name) ILIKE :starts) DESC, p.hierarchy_level NULLS LAST, 2
        LIMIT 7"""), {"acl": acl, "like": like, "starts": starts, "wordre": wordre}).fetchall()
    accts = s.execute(text("""
        SELECT id, display_name FROM accounts
        WHERE id = ANY(:acl) AND (display_name ILIKE :like OR legal_name ILIKE :like
                                  OR EXISTS (SELECT 1 FROM unnest(coalesce(aliases, '{}')) al WHERE al ILIKE :like))
        ORDER BY 2 LIMIT 3"""), {"acl": acl, "like": like}).fetchall()
    return [{"type": "account", "id": r[0], "name": r[1]} for r in accts] + \
           [{"type": "persona", "id": r[0], "name": r[1], "title": r[2], "account": r[3], "account_id": r[4]} for r in people]


def _file(content: bytes, filename: str, media: str) -> Response:
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/messages/{mid}/export")
def export_message(mid: int, format: str = "pdf", part: str = "table",
                   user: User = Depends(auth.get_current_user), s=Depends(_session)):
    row = s.execute(text("""
        SELECT m.content, m.mode, m.citations, m.extras, m.session_id, m.id
        FROM copilot_messages m JOIN copilot_sessions ss ON ss.id = m.session_id
        WHERE m.id = :m AND ss.user_id = :u AND m.role = 'assistant'"""), {"m": mid, "u": user.id}).fetchone()
    if not row:
        raise HTTPException(404, "Answer not found.")
    question = s.execute(text("""SELECT content FROM copilot_messages WHERE session_id = :s AND id < :m AND role = 'user'
                                 ORDER BY id DESC LIMIT 1"""), {"s": row[4], "m": mid}).scalar() or "Answer"
    msg = {"content": row[0], "mode": row[1], "citations": row[2] or [], "extras": row[3] or {}}
    name = exports.safe_filename(question[:50], "")
    if format == "pdf":
        return _file(exports.answer_pdf(question, msg, user.full_name or user.email), name + "pdf", "application/pdf")
    if format == "xlsx" and part == "sources":
        if not msg["citations"]:
            raise HTTPException(400, "This answer has no sources.")
        return _file(exports.sources_xlsx(msg["citations"], question), exports.safe_filename(question[:40] + "-sources", "xlsx"), XLSX)
    if format in ("xlsx", "csv"):
        if not (msg["extras"].get("table") or {}).get("rows"):
            raise HTTPException(400, "This answer has no table to export.")
        if format == "csv":
            return _file(exports.table_csv(msg["extras"]["table"]), name + "csv", "text/csv; charset=utf-8")
        return _file(exports.table_xlsx(msg["extras"]["table"], question), name + "xlsx", XLSX)
    raise HTTPException(400, "format must be pdf, xlsx or csv")


class ActionsDoneIn(BaseModel):
    created: List[Dict[str, Any]] = Field(default_factory=list, max_length=100)   # [{label, href}] of the records made


@router.post("/messages/{mid}/actions-done")
def actions_done(mid: int, body: ActionsDoneIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Marks a "create …" proposal (actions.py) as carried out, so reopening the chat shows what was created
    instead of offering the same records again. The records themselves were made by the CRM endpoints."""
    row = s.execute(text("""SELECT m.extras FROM copilot_messages m JOIN copilot_sessions ss ON ss.id = m.session_id
                            WHERE m.id = :m AND ss.user_id = :u AND m.role = 'assistant'"""), {"m": mid, "u": user.id}).fetchone()
    if not row or not (row[0] or {}).get("actions"):
        raise HTTPException(404, "Proposal not found.")
    extras = row[0]
    extras["actions"]["done"] = [{"label": str(c.get("label", ""))[:200], "href": str(c.get("href", ""))[:300]}
                                 for c in body.created]
    s.execute(text("UPDATE copilot_messages SET extras = :e WHERE id = :m"), {"e": json.dumps(extras, default=str), "m": mid})
    s.commit()
    return {"ok": True, "actions": extras["actions"]}


def _load_chat(s, sid: str, user_id: int):
    sess = s.execute(text("SELECT title FROM copilot_sessions WHERE id = :s AND user_id = :u"), {"s": sid, "u": user_id}).fetchone()
    if not sess:
        raise HTTPException(404, "Chat not found.")
    rows = s.execute(text("""SELECT role, content, mode, citations, extras, created_at FROM copilot_messages
                             WHERE session_id = :s ORDER BY id"""), {"s": sid}).fetchall()
    return sess[0] or "Copilot chat", [{"role": r[0], "content": r[1], "mode": r[2], "citations": r[3] or [],
                                        "extras": r[4] or {}, "created_at": r[5]} for r in rows]


@router.get("/sessions/{sid}/export")
def export_session(sid: str, format: str = "pdf", user: User = Depends(auth.get_current_user), s=Depends(_session)):
    title, msgs = _load_chat(s, sid, user.id)
    base = exports.safe_filename(title, "")
    if format == "pdf":
        return _file(exports.chat_pdf(title, msgs, user.full_name or user.email), base + "pdf", "application/pdf")
    if format == "xlsx":
        return _file(exports.chat_xlsx(title, msgs), base + "xlsx", XLSX)
    if format == "md":
        return _file(exports.chat_markdown(title, msgs), base + "md", "text/markdown; charset=utf-8")
    raise HTTPException(400, "format must be pdf, xlsx or md")


@router.get("/notes/export")
def export_notes(format: str = "xlsx", user: User = Depends(auth.get_current_user), s=Depends(_session)):
    notes = list_notes(user=user, s=s)
    if format == "xlsx":
        return _file(exports.notes_xlsx(notes), "my-copilot-notes.xlsx", XLSX)
    raise HTTPException(400, "format must be xlsx")


class ClearIn(BaseModel):
    confirm: str


@router.post("/notes/clear")
def clear_notes(body: ClearIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    if body.confirm.strip().upper() != "CLEAR":
        raise HTTPException(400, 'Type "CLEAR" to confirm.')
    res = s.execute(text("UPDATE copilot_memories SET deleted_at = now() WHERE user_id = :u AND deleted_at IS NULL"),
                    {"u": user.id})
    s.commit()
    return {"cleared": res.rowcount}


@router.get("/export")
def export_my_data(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Everything the copilot stores about the caller (notes, prefs, chats) as JSON (README §18.5)."""
    sessions = s.execute(text("SELECT id, title, created_at FROM copilot_sessions WHERE user_id = :u ORDER BY created_at"),
                         {"u": user.id}).fetchall()
    chats = []
    for sid, title, created in sessions:
        _, msgs = _load_chat(s, str(sid), user.id)
        chats.append({"id": str(sid), "title": title, "created_at": created, "messages": msgs})
    payload = {"exported_at": exports._now(), "prefs": chat._prefs(s, user.id),
               "notes": list_notes(user=user, s=s), "chats": chats}
    return _file(json.dumps(payload, default=str, indent=2).encode("utf-8"), "my-copilot-data.json", "application/json")


@router.get("/context")
def context_options(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Accounts + people for the scope picker (ACL-scoped, no contact data)."""
    acl = retrieve.acl_account_ids(s, user)
    accts = s.execute(text("SELECT id, display_name FROM accounts WHERE id = ANY(:a) ORDER BY 2"), {"a": acl}).fetchall()
    return {"accounts": [{"id": r[0], "name": r[1]} for r in accts]}


@router.get("/admin/status")
def admin_status(user: User = Depends(auth.require_role("super_admin")), s=Depends(_session)):
    pending = s.execute(text("SELECT count(*) FROM rag_outbox WHERE processed_at IS NULL")).scalar()
    return {"index": retrieve.index_stats(), "sync": sync.state(), "outbox_pending": pending,
            "quota": llm.quota_status(s, user.id)}


@router.post("/admin/sync")
def admin_sync(user: User = Depends(auth.require_role("super_admin"))):
    """Run a full sync + retention now, inside the API process (which owns the embedded Chroma store)."""
    if sync.state()["running"]:
        return {"started": False, "reason": "A sync is already running."}
    threading.Thread(target=lambda: _safe(sync.process, force_full=True), name="copilot-sync-now", daemon=True).start()
    return {"started": True}


def _safe(fn, **kw):
    try:
        fn(**kw)
    except Exception:
        pass   # recorded in sync.state()


# ── Pipeline console (/copilot-pipeline page) — super_admin only ─────────────
pipeline_router = APIRouter(prefix="/api/copilotpipeline", tags=["Sales Copilot pipeline"])
_admin = auth.require_role("super_admin")


class RunIn(BaseModel):
    mode: str = Field("sync", pattern="^(sync|reembed|eval)$")
    guardrails: bool = True
    eval: bool = True


class QueryTraceIn(BaseModel):
    q: str = Field(..., min_length=1, max_length=500)
    account_id: Optional[int] = None
    persona_id: Optional[int] = None


@pipeline_router.get("/status")
def pipeline_status(user: User = Depends(_admin)):
    """Index stats, embedding config, the live run (if any) and recent runs."""
    return pipeline.overview()


@pipeline_router.post("/runs")
def pipeline_start(body: RunIn, user: User = Depends(_admin)):
    """Start sync (incremental), reembed (full re-embed, swapped in when complete) or eval-only,
    optionally followed by the guardrail suite and the golden-set eval."""
    try:
        return pipeline.start(body.mode, body.guardrails, body.eval, getattr(user, "id", None))
    except pipeline.Busy as e:
        raise HTTPException(409, str(e))


@pipeline_router.get("/runs")
def pipeline_runs(limit: int = 20, user: User = Depends(_admin)):
    return pipeline.history(min(max(limit, 1), 100))


@pipeline_router.get("/runs/current")
def pipeline_current(user: User = Depends(_admin)):
    return pipeline.current()


@pipeline_router.get("/runs/{run_id}")
def pipeline_run(run_id: str, user: User = Depends(_admin)):
    run = pipeline.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found.")
    return run


@pipeline_router.get("/documents")
def pipeline_documents(q: str = "", doc_type: str = "", user: User = Depends(_admin), s=Depends(_session)):
    return pipeline.search_documents(s, q, doc_type)


@pipeline_router.get("/documents/{doc_id}/trace")
def pipeline_document_trace(doc_id: int, chunk: int = 0, user: User = Depends(_admin), s=Depends(_session)):
    """One document step by step: sources → versions → entities → chunks → vectors → Chroma → neighbours."""
    out = pipeline.trace_document(s, doc_id, chunk)
    if not out:
        raise HTTPException(404, "Document not found.")
    return out


@pipeline_router.post("/query-trace")
def pipeline_query_trace(body: QueryTraceIn, user: User = Depends(_admin), s=Depends(_session)):
    """One question through retrieval: embed, vector leg, keyword leg, RRF, verify/boost/budget."""
    return pipeline.trace_query(s, body.q, [body.account_id] if body.account_id else None, body.persona_id)


def install(app) -> None:
    """Called from the main api.py: ensure tables, mount routes, warm the embedding model."""
    try:
        ingest.ensure_schema()
    except Exception as e:
        print(f"[copilot] schema check failed: {e}")
    app.include_router(router)
    app.include_router(pipeline_router)
    embed.warm_up_in_background()
    try:
        from apps.sales_copilot import privacy
        privacy.shared_lines()   # warm the switchboard cache at startup, not inside a request
    except Exception:
        pass
    sync.start_background()      # drains the trigger outbox every few minutes (COPILOT_AUTOSYNC=0 to disable)
