"""Activities: one timeline of customer interactions linked to accounts, contacts, deals and
introductions (apps/sales_crm/README.md §2.4, §4, M3).

  GET    /api/crm/activities?object_type=&object_id=     timeline for one record
  POST   /api/crm/activities                              log an email / meeting / call / note / LinkedIn touch
  PATCH  /api/crm/activities/{id}                         edit, make private, add/remove links (owner or admin)
  DELETE /api/crm/activities/{id}                         owner or admin
  POST   /api/crm/activities/transcript                   upload a meeting transcript (.vtt .srt .txt .docx, base64 JSON)

Linking: every link to a contact, deal or introduction also links its account ("derived"), so
account timelines show everything. Visibility: 'team' (anyone who can open the account) or
'private' (owner only; never indexed by the copilot). A meeting / call / transcript linked to an
open introduction that is not yet past "intro made" moves it to *meeting held*.
Transcript summaries and action items are extractive — no AI requests.
"""

import base64
import io
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from apps.sales_crm import permissions
from db.connection import get_session
from db.models.user import User

router = APIRouter(prefix="/api/crm/activities", tags=["CRM activities"])

TYPES = ["email", "meeting", "call", "note", "transcript", "linkedin", "task_done"]
TYPE_LABEL = {"email": "Email", "meeting": "Meeting", "call": "Call", "note": "Note", "transcript": "Meeting transcript",
              "linkedin": "LinkedIn", "task_done": "Task done"}
OBJECT_TYPES = ["account", "persona", "deal", "introduction"]
MEETING_TYPES = ("meeting", "call", "transcript")
MAX_UPLOAD_BYTES = 3 * 1024 * 1024
READERS = ("sales_manager", "user", "viewer")


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _q(s, sql: str, **kw) -> List[Dict[str, Any]]:
    return [dict(r) for r in s.execute(text(sql), kw).mappings()]


# ── Link resolution & access ──────────────────────────────────────────────────


def _object_account(s, object_type: str, object_id: int) -> Optional[int]:
    sql = {"account": "SELECT id FROM accounts WHERE id = :i",
           "persona": "SELECT account_id FROM personas WHERE id = :i",
           "deal": "SELECT account_id FROM deals WHERE id = :i",
           "introduction": "SELECT account_id FROM introductions WHERE id = :i"}[object_type]
    return s.execute(text(sql), {"i": object_id}).scalar()


def _check_object(s, user, object_type: str, object_id: int) -> int:
    if object_type not in OBJECT_TYPES:
        raise HTTPException(400, f"object_type must be one of {', '.join(OBJECT_TYPES)}")
    aid = _object_account(s, object_type, object_id)
    if aid is None or aid not in auth.account_scope(s, user):
        raise HTTPException(404, f"{object_type.capitalize()} not found.")
    return aid


def _visible_sql() -> str:
    return "(a.visibility = 'team' OR a.owner_user_id = :me)"


def _link_names(s, activity_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not activity_ids:
        return {}
    rows = _q(s, """
        SELECT l.activity_id, l.object_type, l.object_id, l.matched_by,
               CASE l.object_type
                 WHEN 'account' THEN (SELECT display_name FROM accounts WHERE id = l.object_id)
                 WHEN 'persona' THEN (SELECT coalesce(full_name, display_name) FROM personas WHERE id = l.object_id)
                 WHEN 'deal' THEN (SELECT name FROM deals WHERE id = l.object_id)
                 WHEN 'introduction' THEN (SELECT 'Intro via ' || c.name FROM introductions i JOIN connectors c ON c.id = i.connector_id
                                           WHERE i.id = l.object_id)
               END AS name,
               CASE WHEN l.object_type = 'persona' THEN (SELECT account_id FROM personas WHERE id = l.object_id) END AS persona_account_id
        FROM activity_links l WHERE l.activity_id = ANY(:ids)
        ORDER BY array_position(ARRAY['deal','introduction','persona','account'], l.object_type)""", ids=activity_ids)
    out: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r.pop("activity_id"), []).append(r)
    return out


def _serialize(rows: List[Dict[str, Any]], s, user, with_body: bool = False) -> List[Dict[str, Any]]:
    links = _link_names(s, [r["id"] for r in rows])
    me = getattr(user, "id", None)
    admin = permissions.is_admin(user)
    out = []
    for r in rows:
        item = {k: r[k] for k in ("id", "type", "direction", "subject", "summary", "occurred_at", "duration_min", "source",
                                  "visibility", "participants", "created_at", "owner_user_id")}
        item["type_label"] = TYPE_LABEL.get(r["type"], r["type"])
        item["owner_name"] = r.get("owner_name")
        item["links"] = links.get(r["id"], [])
        meta = r.get("metadata") or {}
        item["action_items"] = meta.get("action_items") or []
        item["speakers"] = meta.get("speakers") or []
        item["file_name"] = meta.get("file_name")
        item["has_body"] = bool(r.get("body"))
        if with_body:
            item["body"] = r.get("body")
        item["can_edit"] = (admin or r["owner_user_id"] == me) and not (auth.AUTH_ENFORCED and user.role in auth.READ_ONLY_ROLES)
        out.append(item)
    return out


def _add_links(s, activity_id: int, links: List[Tuple[str, int, str]]) -> None:
    """Insert links + the derived account link for each non-account object."""
    seen = set()
    for otype, oid, how in links:
        rows = [(otype, oid, how)]
        if otype != "account":
            aid = _object_account(s, otype, oid)
            if aid:
                rows.append(("account", aid, "derived"))
        for r in rows:
            if (r[0], r[1]) in seen:
                continue
            seen.add((r[0], r[1]))
            s.execute(text("""INSERT INTO activity_links (activity_id, object_type, object_id, matched_by)
                              VALUES (:a, :t, :o, :m) ON CONFLICT DO NOTHING"""), {"a": activity_id, "t": r[0], "o": r[1], "m": r[2]})


def _after_write(s, activity_id: int, user_id: Optional[int]) -> None:
    """Denormalised last-activity dates + intro auto-progress to 'meeting held'."""
    act = s.execute(text("SELECT type, occurred_at, visibility FROM activities WHERE id = :a"), {"a": activity_id}).mappings().fetchone()
    if not act:
        return
    for otype, table in (("persona", "personas"), ("deal", "deals")):
        s.execute(text(f"""UPDATE {table} t SET last_activity_at = greatest(coalesce(t.last_activity_at, :w), :w)
                           FROM activity_links l WHERE l.activity_id = :a AND l.object_type = :ot AND l.object_id = t.id"""),
                  {"w": act["occurred_at"], "a": activity_id, "ot": otype})
    if act["type"] not in MEETING_TYPES or act["occurred_at"] > datetime.now(timezone.utc):
        return
    intros = s.execute(text("""
        SELECT DISTINCT i.id, i.status FROM introductions i
        JOIN activity_links l ON l.activity_id = :a AND (
             (l.object_type = 'introduction' AND l.object_id = i.id)
          OR (l.object_type = 'persona' AND l.object_id = i.persona_id))
        WHERE i.status IN ('requested','accepted','intro_made')"""), {"a": activity_id}).fetchall()
    for iid, status in intros:
        s.execute(text("""UPDATE introductions SET status = 'meeting_held', meeting_at = coalesce(meeting_at, :w), updated_at = now()
                          WHERE id = :i"""), {"w": act["occurred_at"], "i": iid})
        s.execute(text("""INSERT INTO introduction_events (introduction_id, kind, from_status, to_status, note, partner_visible, by_user)
                          VALUES (:i, 'status', :f, 'meeting_held', :n, true, :u)"""),
                  {"i": iid, "f": status, "n": f"{TYPE_LABEL[act['type']]} logged", "u": user_id})
        s.execute(text("""INSERT INTO activity_links (activity_id, object_type, object_id, matched_by)
                          VALUES (:a, 'introduction', :i, 'rule') ON CONFLICT DO NOTHING"""), {"a": activity_id, "i": iid})


def _load(s, user, activity_id: int) -> Dict[str, Any]:
    rows = _q(s, f"""SELECT a.*, coalesce(u.full_name, u.email) AS owner_name FROM activities a
                     LEFT JOIN users u ON u.id = a.owner_user_id WHERE a.id = :i AND {_visible_sql()}""",
              i=activity_id, me=getattr(user, "id", None))
    if not rows:
        raise HTTPException(404, "Activity not found.")
    accts = {r[0] for r in s.execute(text("SELECT object_id FROM activity_links WHERE activity_id = :a AND object_type = 'account'"),
                                      {"a": activity_id})}
    scope = set(auth.account_scope(s, user))
    if accts and not (accts & scope):
        raise HTTPException(404, "Activity not found.")
    return rows[0]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("")
def timeline(object_type: str, object_id: int, type: Optional[str] = None, limit: int = 100,
             user: User = Depends(permissions.require_roles(*READERS)), s=Depends(_session)):
    _check_object(s, user, object_type, object_id)
    rows = _q(s, f"""
        SELECT a.*, coalesce(u.full_name, u.email) AS owner_name FROM activities a
        JOIN activity_links l ON l.activity_id = a.id AND l.object_type = :t AND l.object_id = :o
        LEFT JOIN users u ON u.id = a.owner_user_id
        WHERE {_visible_sql()} AND (CAST(:ty AS text) IS NULL OR a.type = :ty)
        ORDER BY a.occurred_at DESC, a.id DESC LIMIT :lim""",
              t=object_type, o=object_id, me=user.id, ty=type, lim=max(1, min(limit, 500)))
    items = _serialize(rows, s, user)
    counts = Counter(i["type"] for i in items)
    return {"activities": items, "counts": dict(counts), "types": [{"key": k, "label": TYPE_LABEL[k]} for k in TYPES],
            "last_activity_at": items[0]["occurred_at"] if items else None}


@router.get("/{activity_id}")
def get_activity(activity_id: int, user: User = Depends(permissions.require_roles(*READERS)), s=Depends(_session)):
    return _serialize([_load(s, user, activity_id)], s, user, with_body=True)[0]


class LinkIn(BaseModel):
    object_type: str
    object_id: int


class ActivityIn(BaseModel):
    type: str
    direction: Optional[str] = None
    subject: Optional[str] = Field(None, max_length=300)
    summary: Optional[str] = Field(None, max_length=20000)
    occurred_at: Optional[datetime] = None
    duration_min: Optional[int] = Field(None, ge=0, le=1440)
    visibility: str = "team"
    links: List[LinkIn] = Field(default_factory=list)
    participant_persona_ids: List[int] = Field(default_factory=list)


def _validate_common(type_: str, direction: Optional[str], visibility: str) -> None:
    if type_ not in TYPES or type_ == "transcript":
        raise HTTPException(400, "type must be email, meeting, call, note, linkedin or task_done (use the transcript upload for transcripts)")
    if direction is not None and direction not in ("inbound", "outbound", "internal"):
        raise HTTPException(400, "direction must be inbound, outbound or internal")
    if visibility not in ("team", "private"):
        raise HTTPException(400, "visibility must be team or private")


@router.post("")
def create_activity(body: ActivityIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    _validate_common(body.type, body.direction, body.visibility)
    if not body.links:
        raise HTTPException(400, "Link the activity to at least one account, contact, deal or introduction.")
    if not (body.subject or body.summary):
        raise HTTPException(400, "Add a subject or some notes.")
    links = []
    for l in body.links:
        _check_object(s, user, l.object_type, l.object_id)
        links.append((l.object_type, l.object_id, "manual"))
    participants = []
    for pid in dict.fromkeys(body.participant_persona_ids):
        _check_object(s, user, "persona", pid)
        name = s.execute(text("SELECT coalesce(full_name, display_name) FROM personas WHERE id = :p"), {"p": pid}).scalar()
        participants.append({"name": name, "persona_id": pid, "is_internal": False})
        links.append(("persona", pid, "manual"))
    occurred = body.occurred_at or datetime.now(timezone.utc)
    aid = s.execute(text("""INSERT INTO activities (type, direction, subject, summary, occurred_at, duration_min, owner_user_id,
                                                    source, visibility, participants)
                            VALUES (:t, :d, :su, :sm, :o, :du, :u, 'manual', :v, CAST(:p AS jsonb)) RETURNING id"""),
                    {"t": body.type, "d": body.direction, "su": (body.subject or "").strip() or None,
                     "sm": (body.summary or "").strip() or None, "o": occurred, "du": body.duration_min, "u": user.id,
                     "v": body.visibility, "p": json.dumps(participants)}).scalar()
    _add_links(s, aid, links)
    _after_write(s, aid, user.id)
    s.commit()
    return get_activity(aid, user, s)


class ActivityPatch(BaseModel):
    subject: Optional[str] = Field(None, max_length=300)
    summary: Optional[str] = Field(None, max_length=20000)
    occurred_at: Optional[datetime] = None
    duration_min: Optional[int] = Field(None, ge=0, le=1440)
    visibility: Optional[str] = None
    add_links: List[LinkIn] = Field(default_factory=list)
    remove_links: List[LinkIn] = Field(default_factory=list)


def _require_owner(s, user, act: Dict[str, Any]) -> None:
    if auth.AUTH_ENFORCED and user.role in auth.READ_ONLY_ROLES:
        raise HTTPException(403, "Your role is read-only.")
    if not (permissions.is_admin(user) or act["owner_user_id"] == user.id):
        raise HTTPException(403, "Only the person who logged this activity (or an admin) can change it.")


@router.patch("/{activity_id}")
def update_activity(activity_id: int, body: ActivityPatch, user: User = Depends(permissions.require_write), s=Depends(_session)):
    act = _load(s, user, activity_id)
    _require_owner(s, user, act)
    ch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in ("subject", "summary", "occurred_at", "duration_min", "visibility")}
    if "visibility" in ch and ch["visibility"] not in ("team", "private"):
        raise HTTPException(400, "visibility must be team or private")
    for col, val in ch.items():
        s.execute(text(f"UPDATE activities SET {col} = :v, updated_at = now() WHERE id = :a"), {"v": val, "a": activity_id})
    new_links = []
    for l in body.add_links:
        _check_object(s, user, l.object_type, l.object_id)
        new_links.append((l.object_type, l.object_id, "manual"))
    _add_links(s, activity_id, new_links)
    for l in body.remove_links:
        s.execute(text("DELETE FROM activity_links WHERE activity_id = :a AND object_type = :t AND object_id = :o"),
                  {"a": activity_id, "t": l.object_type, "o": l.object_id})
    if not s.execute(text("SELECT 1 FROM activity_links WHERE activity_id = :a LIMIT 1"), {"a": activity_id}).scalar():
        raise HTTPException(400, "An activity must stay linked to at least one record.")
    _after_write(s, activity_id, user.id)
    s.commit()
    return get_activity(activity_id, user, s)


@router.delete("/{activity_id}")
def delete_activity(activity_id: int, user: User = Depends(permissions.require_write), s=Depends(_session)):
    act = _load(s, user, activity_id)
    _require_owner(s, user, act)
    s.execute(text("DELETE FROM activities WHERE id = :a"), {"a": activity_id})
    s.commit()
    return {"ok": True}


# ── Transcripts ───────────────────────────────────────────────────────────────

TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        raise HTTPException(400, "That .docx file could not be read.")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        t = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S))
        paras.append(unescape(t))
    return "\n".join(paras)


def parse_transcript(raw: str) -> List[Tuple[str, str]]:
    """→ [(speaker, text)] from Teams/Zoom/Meet .vtt, .srt, .txt or .docx text. Consecutive lines by the same speaker merge."""
    lines = [l.strip() for l in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    turns: List[Tuple[str, str]] = []
    pending_speaker = None

    def add(sp: str, tx: str):
        tx = re.sub(r"\s+", " ", tx).strip()
        if not tx:
            return
        sp = (sp or "Unknown").strip()
        if turns and turns[-1][0] == sp:
            turns[-1] = (sp, turns[-1][1] + " " + tx)
        else:
            turns.append((sp, tx))

    for line in lines:
        if not line or line == "WEBVTT" or re.fullmatch(r"\d+", line) or re.match(rf"^{TS}\s*-->\s*{TS}", line) \
                or line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        m = re.match(r"^<v\s+([^>]+)>(.*?)(?:</v>)?$", line)                      # Teams VTT
        if m:
            add(m.group(1), re.sub(r"<[^>]+>", "", m.group(2)))
            pending_speaker = None
            continue
        m = re.match(rf"^(?:\[?{TS}\]?\s*)?([A-Z][\w.'’\- ]{{1,60}}?)\s*(?:\(\S+\))?\s*:\s+(.+)$", line)   # "Name: text" / "[00:01] Name: text"
        if m and len(m.group(1).split()) <= 5:
            add(m.group(1), m.group(2))
            pending_speaker = None
            continue
        m = re.match(rf"^([A-Z][\w.'’\- ]{{1,60}}?)\s{{1,}}{TS}$", line)          # Teams .docx/.txt header "Name   0:03"
        if m and len(m.group(1).split()) <= 5:
            pending_speaker = m.group(1)
            continue
        m = re.match(rf"^{TS}\s+([A-Z][\w.'’\- ]{{1,60}})$", line)               # Zoom "00:01:02 Name"
        if m and len(m.group(1).split()) <= 5:
            pending_speaker = m.group(1)
            continue
        add(pending_speaker or (turns[-1][0] if turns else "Unknown"), re.sub(r"<[^>]+>", "", line))
    return turns


def _norm(n: str) -> List[str]:
    return [w for w in re.sub(r"[^a-z\s]", " ", (n or "").lower()).split() if len(w) > 1]


def match_speakers(s, account_id: int, speakers: List[str]) -> Dict[str, Dict[str, Any]]:
    people = _q(s, "SELECT id, coalesce(full_name, display_name) AS name FROM personas WHERE account_id = :a", a=account_id)
    staff = _q(s, "SELECT id, coalesce(full_name, email) AS name FROM users WHERE is_active AND role <> 'partner'")
    out = {}
    for sp in speakers:
        toks = _norm(sp)
        if not toks:
            continue
        hit = None
        for p in people:
            pt = _norm(p["name"])
            if not pt:
                continue
            if toks == pt or (len(toks) >= 2 and toks[0] == pt[0] and toks[-1] == pt[-1]):
                hit = {"persona_id": p["id"], "name": p["name"], "is_internal": False}
                break
        if not hit:
            for u in staff:
                ut = _norm(u["name"])
                if ut and (toks == ut or (len(toks) >= 2 and toks[0] == ut[0] and toks[-1] == ut[-1])):
                    hit = {"user_id": u["id"], "name": u["name"], "is_internal": True}
                    break
        out[sp] = hit or {"name": sp, "is_internal": None}
    return out


KEY_TERMS = r"budget|timeline|deadline|decision|decide|approv|priorit|challenge|problem|pain|risk|concern|pilot|proposal|pricing|price|" \
            r"cost|contract|security|complian|competitor|vendor|integration|roadmap|quarter|q[1-4]|next step|follow[- ]?up|success|metric|kpi"
ACTION = r"\b(i'll|i will|we'll|we will|let's|let us|next step|follow[- ]?up|will send|send over|share (the|a)|schedule|set up|book|" \
         r"circle back|by (monday|tuesday|wednesday|thursday|friday|next week|end of (the )?(day|week|month)|eod|eow))\b"


def summarize(turns: List[Tuple[str, str]], who: Dict[str, Dict[str, Any]], max_points: int = 6) -> Tuple[str, List[Dict[str, str]]]:
    sentences = []
    for idx, (sp, tx) in enumerate(turns):
        for sent in re.split(r"(?<=[.!?])\s+", tx):
            w = len(sent.split())
            if 6 <= w <= 45:
                sentences.append((idx, sp, sent.strip()))
    scored = []
    for order, (idx, sp, sent) in enumerate(sentences):
        sl = sent.lower()
        score = 2 * len(re.findall(KEY_TERMS, sl)) + (2 if re.search(r"\d|%|\$|€|£", sent) else 0)
        score += 1 if (who.get(sp) or {}).get("is_internal") is False else 0            # the customer's words matter most
        score += 1 if sent.endswith("?") else 0
        if score:
            scored.append((score, order, sp, sent))
    top = sorted(sorted(scored, key=lambda x: -x[0])[:max_points], key=lambda x: x[1])
    summary = "\n".join(f"- {sp}: {sent}" for _, _, sp, sent in top)
    actions, seen = [], set()
    for _, sp, sent in sentences:
        if re.search(ACTION, sent, re.I) and sent.lower() not in seen:
            seen.add(sent.lower())
            actions.append({"speaker": sp, "text": sent})
            if len(actions) >= 8:
                break
    return summary, actions


class TranscriptIn(BaseModel):
    file_name: str = Field(..., max_length=200)
    content_base64: str
    account_id: int
    deal_id: Optional[int] = None
    introduction_id: Optional[int] = None
    subject: Optional[str] = Field(None, max_length=300)
    occurred_at: Optional[datetime] = None
    duration_min: Optional[int] = Field(None, ge=0, le=1440)
    visibility: str = "team"


@router.post("/transcript")
def upload_transcript(body: TranscriptIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    _check_object(s, user, "account", body.account_id)
    for otype, oid in (("deal", body.deal_id), ("introduction", body.introduction_id)):
        if oid is not None and _check_object(s, user, otype, oid) != body.account_id:
            raise HTTPException(400, f"That {otype} is at a different account.")
    if body.visibility not in ("team", "private"):
        raise HTTPException(400, "visibility must be team or private")
    ext = body.file_name.lower().rsplit(".", 1)[-1] if "." in body.file_name else ""
    if ext not in ("vtt", "srt", "txt", "docx"):
        raise HTTPException(400, "Upload a .vtt, .srt, .txt or .docx transcript.")
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except ValueError:
        raise HTTPException(400, "The file content is not valid base64.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Transcripts are limited to 3 MB.")
    raw = _docx_text(data) if ext == "docx" else data.decode("utf-8-sig", "replace")
    turns = parse_transcript(raw)
    if not turns or sum(len(t.split()) for _, t in turns) < 20:
        raise HTTPException(400, "No transcript text was found in that file.")
    who = match_speakers(s, body.account_id, list(dict.fromkeys(sp for sp, _ in turns)))
    summary, actions = summarize(turns, who)
    talk = Counter()
    for sp, tx in turns:
        talk[sp] += len(tx.split())
    total_words = sum(talk.values())
    speakers = [{"name": sp, "words": n, "share_pct": round(n / total_words * 100), **(who.get(sp) or {})}
                for sp, n in talk.most_common()]
    participants = [{"name": x["name"], "persona_id": x.get("persona_id"), "is_internal": x.get("is_internal")} for x in speakers]
    body_text = "\n".join(f"{sp}: {tx}" for sp, tx in turns)
    meta = {"file_name": body.file_name, "speakers": speakers, "action_items": actions, "words": total_words}
    aid = s.execute(text("""INSERT INTO activities (type, direction, subject, summary, body, occurred_at, duration_min, owner_user_id,
                                                    source, visibility, participants, metadata)
                            VALUES ('transcript', 'internal', :su, :sm, :b, :o, :du, :u, 'upload', :v, CAST(:p AS jsonb), CAST(:m AS jsonb))
                            RETURNING id"""),
                    {"su": body.subject or f"Meeting transcript — {body.file_name}", "sm": summary or None, "b": body_text,
                     "o": body.occurred_at or datetime.now(timezone.utc), "du": body.duration_min, "u": user.id, "v": body.visibility,
                     "p": json.dumps(participants), "m": json.dumps(meta)}).scalar()
    links = [("account", body.account_id, "manual")]
    links += [("deal", body.deal_id, "manual")] if body.deal_id else []
    links += [("introduction", body.introduction_id, "manual")] if body.introduction_id else []
    links += [("persona", x["persona_id"], "speaker") for x in speakers if x.get("persona_id")]
    _add_links(s, aid, links)
    _after_write(s, aid, user.id)
    s.commit()
    out = get_activity(aid, user, s)
    out["matched_contacts"] = sum(1 for x in speakers if x.get("persona_id"))
    out["unmatched_speakers"] = [x["name"] for x in speakers if x.get("is_internal") is None]
    return out
