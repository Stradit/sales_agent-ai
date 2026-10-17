"""Search palette API.

/api/search/suggest  — type-ahead. Plain Postgres (exact > prefix > word-prefix > contains >
                       trigram typo match), grouped by entity type. No embeddings: names, titles
                       and partial words are lexical problems, and this runs on every keystroke.
/api/search/semantic — "related content" for phrase-like queries. Reuses the copilot's hybrid
                       retrieval (Chroma vectors + Postgres full-text, RRF). No LLM call.

/api/search/log     — the palette reports each search session's final query (result count,
                       what was picked). /api/search/misses lists what people looked for and
                       didn't find (super admin). Rows older than LOG_RETENTION_DAYS are pruned.

Both searches are scoped to the accounts the caller may see (same rule as the copilot).
"""
import html
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from db.connection import get_session
from db.models.user import User
from apps.sales_copilot import retrieve

router = APIRouter(prefix="/api/search", tags=["Search"])

# Set by install(): whether pg_trgm is usable, which enables typo-tolerant matching.
_TRGM = False
_FUZZY_MIN = 0.45   # word_similarity threshold; below this, typo matches are mostly noise

CURRENT_ACCOUNT_BOOST = 8   # enough to win ties within a tier, not to beat an exact match elsewhere

LOG_RETENTION_DAYS = 90
LOG_TYPES = {"account", "persona", "lob", "signal", "deal", "task", "doc", "copilot"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_log (
  id           bigserial PRIMARY KEY,
  user_id      int REFERENCES users(id) ON DELETE SET NULL,
  q            text NOT NULL,
  account_id   int,
  n_results    int NOT NULL,
  chosen_type  text,
  semantic     boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS search_log_created ON search_log (created_at);
"""

GROUP_LIMITS = {"accounts": 3, "people": 6, "lobs": 4, "signals": 4, "deals": 3, "tasks": 3}


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _params(q: str, acl: List[int], current: Optional[int]) -> Dict[str, Any]:
    esc = _like_escape(q)
    return {
        "q": q, "ql": q.lower(), "acl": acl, "cur": current or 0,
        "like": f"%{esc}%", "starts": f"{esc}%",
        "wordre": r"\m" + re.escape(q),
        # Multi-word queries: every word must start a word somewhere in the field, any order.
        "toks": [r"\m" + re.escape(w) for w in q.split()] if " " in q else [],
    }


def _score(col: str, weight: float = 1.0) -> str:
    """SQL rank for one text column: exact 100, prefix 90, word-prefix 80, contains 60,
    typo match up to 50. Multiplied by weight (secondary fields rank below names)."""
    col = f"replace({col}, chr(160), ' ')"   # some scraped legal names use non-breaking spaces
    fuzzy = f"WHEN word_similarity(:q, {col}) >= {_FUZZY_MIN} THEN 50 * word_similarity(:q, {col})" if _TRGM else ""
    return f"""({weight} * CASE
        WHEN {col} IS NULL THEN 0
        WHEN lower({col}) = :ql THEN 100
        WHEN {col} ILIKE :starts THEN 90
        WHEN {col} ~* :wordre THEN 80
        WHEN {col} ILIKE :like THEN 60
        WHEN cardinality(CAST(:toks AS text[])) > 0
             AND NOT EXISTS (SELECT 1 FROM unnest(CAST(:toks AS text[])) tok WHERE {col} !~* tok) THEN 55
        {fuzzy}
        ELSE 0 END)"""


def _boost(col: str) -> str:
    return f"CASE WHEN {col} = :cur THEN {CURRENT_ACCOUNT_BOOST} ELSE 0 END"


def _grouped(session, sql: str, params: Dict[str, Any], limit: int) -> List[Any]:
    """sql must select `name`, `score` (match only) and `boost`; the boost only reorders matches."""
    return session.execute(text(f"""SELECT *, score + boost AS rank FROM ({sql}) x
                                    WHERE score > 0 ORDER BY rank DESC, name LIMIT :lim"""),
                           {**params, "lim": limit}).fetchall()


def _clean(s: Optional[str]) -> Optional[str]:
    return html.unescape(s).replace("\xa0", " ") if s else s


def _accounts(s, p):
    score = f"GREATEST({_score('a.display_name')}, {_score('a.legal_name', 0.9)}, " \
            f"{_score('a.stock_symbol', 0.9)}, {_score('a.domain', 0.7)}, " \
            f"coalesce((SELECT max({_score('al')}) FROM unnest(a.aliases) al), 0) * 0.9)"
    rows = _grouped(s, f"""SELECT a.id, a.display_name AS name, a.stock_symbol, a.headquarters_location,
                                  {score} AS score, {_boost('a.id')} AS boost
                           FROM accounts a WHERE a.id = ANY(:acl)""", p, GROUP_LIMITS["accounts"])
    return [{"type": "account", "id": r.id, "account_id": r.id, "title": _clean(r.name),
             "subtitle": " · ".join(x for x in [r.stock_symbol, r.headquarters_location] if x)} for r in rows]


def _people(s, p):
    name = "coalesce(p.full_name, p.display_name)"
    score = f"GREATEST({_score(name)}, {_score('p.title', 0.6)})"
    rows = _grouped(s, f"""SELECT p.id, {name} AS name, p.title, p.account_id, p.lob_id, a.display_name AS account,
                                  {score} AS score,
                                  {_boost('p.account_id')} - coalesce(p.hierarchy_level, 5) * 0.5 AS boost
                           FROM personas p JOIN accounts a ON a.id = p.account_id
                           WHERE p.account_id = ANY(:acl)""", p, GROUP_LIMITS["people"])
    # No contact data here: suggestions only need enough to recognise the person.
    return [{"type": "persona", "id": r.id, "account_id": r.account_id, "lob_id": r.lob_id, "title": _clean(r.name),
             "subtitle": " · ".join(x for x in [r.title, r.account] if x)} for r in rows]


def _lobs(s, p):
    rows = _grouped(s, f"""
        SELECT l.id, l.lob_name AS name, l.account_id, a.display_name AS account, NULL::text AS parent,
               {_score('l.lob_name')} AS score, {_boost('l.account_id')} AS boost
        FROM lobs l JOIN accounts a ON a.id = l.account_id WHERE l.account_id = ANY(:acl)
        UNION ALL
        SELECT l.id, sl.name, l.account_id, a.display_name, l.lob_name,
               GREATEST({_score('sl.name', 0.85)}, {_score('sl.legal_name', 0.8)}), {_boost('l.account_id')}
        FROM sub_lobs sl JOIN lobs l ON l.id = sl.lob_id JOIN accounts a ON a.id = l.account_id
        WHERE l.account_id = ANY(:acl)""", p, GROUP_LIMITS["lobs"])
    return [{"type": "lob", "id": r.id, "account_id": r.account_id, "title": _clean(r.name),
             "subtitle": " · ".join(x for x in [f"in {_clean(r.parent)}" if r.parent else None, r.account] if x)} for r in rows]


def _signals(s, p):
    score = f"GREATEST({_score('o.title')}, {_score('o.category', 0.5)})"
    rows = _grouped(s, f"""SELECT o.id, o.title AS name, o.category, o.account_id, a.display_name AS account,
                                  {score} AS score, {_boost('o.account_id')} AS boost
                           FROM opportunity_signals o JOIN accounts a ON a.id = o.account_id
                           WHERE o.account_id = ANY(:acl)""", p, GROUP_LIMITS["signals"])
    return [{"type": "signal", "id": r.id, "account_id": r.account_id, "category": r.category, "title": _clean(r.name),
             "subtitle": " · ".join(x for x in [(r.category or "").replace("_", " ").title(), r.account] if x)}
            for r in rows]


def _deals(s, p):
    rows = _grouped(s, f"""SELECT d.id, d.name, d.stage, d.account_id, a.display_name AS account,
                                  {_score('d.name')} AS score, {_boost('d.account_id')} AS boost
                           FROM deals d JOIN accounts a ON a.id = d.account_id
                           WHERE d.account_id = ANY(:acl)""", p, GROUP_LIMITS["deals"])
    return [{"type": "deal", "id": r.id, "account_id": r.account_id, "title": _clean(r.name),
             "subtitle": " · ".join(x for x in [(r.stage or "").title(), r.account] if x)} for r in rows]


def _tasks(s, p):
    rows = _grouped(s, f"""SELECT t.id, t.title AS name, t.status, t.account_id, a.display_name AS account,
                                  {_score('t.title')} AS score, {_boost('t.account_id')} AS boost
                           FROM action_items t JOIN accounts a ON a.id = t.account_id
                           WHERE t.account_id = ANY(:acl)""", p, GROUP_LIMITS["tasks"])
    return [{"type": "task", "id": r.id, "account_id": r.account_id, "title": _clean(r.name),
             "subtitle": " · ".join(x for x in [(r.status or "").replace("_", " ").title(), r.account] if x)}
            for r in rows]


GROUPS = [("accounts", "Accounts", _accounts), ("people", "People", _people), ("lobs", "Business units", _lobs),
          ("signals", "Signals", _signals), ("deals", "Deals", _deals), ("tasks", "Action items", _tasks)]


@router.get("/suggest")
def suggest(q: str = Query("", max_length=120), account_id: Optional[int] = None,
            user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Grouped type-ahead suggestions. Results in the current account get a boost."""
    q = " ".join(q.split())
    if len(q) < 2:
        return {"q": q, "groups": []}
    acl = retrieve.acl_account_ids(s, user)
    if not acl:
        return {"q": q, "groups": []}
    params = _params(q, acl, account_id if account_id in acl else None)
    groups = []
    for key, label, fn in GROUPS:
        try:
            items = fn(s, params)
        except Exception as e:   # one missing table (e.g. deals not installed) must not break search
            s.rollback()
            print(f"[search] {key} failed: {e}")
            continue
        if items:
            groups.append({"key": key, "label": label, "items": items})
    return {"q": q, "groups": groups}


@router.get("/semantic")
def semantic(q: str = Query("", max_length=300), account_id: Optional[int] = None,
             user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Related documents by meaning (vector + full-text hybrid). Never calls the LLM."""
    q = " ".join(q.split())
    if len(q) < 3:
        return {"q": q, "items": []}
    acl = retrieve.acl_account_ids(s, user)
    scope = [account_id] if account_id and account_id in acl else None
    hits = retrieve.search(s, q, acl, account_ids=scope, limit=6)
    return {"q": q, "items": [{
        "doc_type": h["doc_type"], "title": h["title"] or h["doc_type"].replace("_", " ").title(),
        "url": h["url"], "snippet": h["snippet"],
        "published_at": h["published_at"].isoformat() if h["published_at"] else None,
        "account_id": (h["accounts"] or [None])[0],
        "persona_id": (h["personas"] or [None])[0],
    } for h in hits]}


class SearchLogIn(BaseModel):
    q: str = Field(..., max_length=300)
    n_results: int = Field(..., ge=0, le=1000)
    chosen_type: Optional[str] = None
    account_id: Optional[int] = None
    semantic: bool = False


@router.post("/log", status_code=204)
def log_search(body: SearchLogIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """One row per palette session (sent on pick or close), not per keystroke."""
    q = " ".join(body.q.split())
    if len(q) < 2:
        return
    s.execute(text("""INSERT INTO search_log (user_id, q, account_id, n_results, chosen_type, semantic)
                      VALUES (:u, :q, :a, :n, :c, :sem)"""),
              {"u": getattr(user, "id", None), "q": q, "a": body.account_id, "n": body.n_results,
               "c": body.chosen_type if body.chosen_type in LOG_TYPES else None, "sem": body.semantic})
    s.commit()


@router.get("/misses")
def search_misses(days: int = Query(30, ge=1, le=LOG_RETENTION_DAYS), limit: int = Query(50, ge=1, le=500),
                  user: User = Depends(auth.require_role("super_admin")), s=Depends(_session)):
    """What people searched for and didn't find: no direct matches and nothing picked."""
    rows = s.execute(text("""
        SELECT lower(q) AS q, count(*) AS searches, count(DISTINCT user_id) AS users,
               bool_or(semantic) AS tried_related, max(created_at) AS last_seen
        FROM search_log
        WHERE created_at > now() - make_interval(days => :days) AND n_results = 0 AND chosen_type IS NULL
        GROUP BY lower(q) ORDER BY searches DESC, last_seen DESC LIMIT :lim"""),
                     {"days": days, "lim": limit}).fetchall()
    totals = s.execute(text("""
        SELECT count(*), count(*) FILTER (WHERE n_results = 0), count(*) FILTER (WHERE chosen_type IS NOT NULL)
        FROM search_log WHERE created_at > now() - make_interval(days => :days)"""), {"days": days}).fetchone()
    return {"days": days, "searches": totals[0], "zero_result": totals[1], "picked": totals[2],
            "misses": [{"q": r.q, "searches": r.searches, "users": r.users, "tried_related": r.tried_related,
                        "last_seen": r.last_seen.isoformat()} for r in rows]}


def _ensure_schema() -> None:
    s = get_session()
    try:
        s.execute(text("SET lock_timeout = '5s'"))
        from db.migrate import schema_managed
        if schema_managed():   # table comes from migrations; retention runs in CRM housekeeping
            return
        s.execute(text(SCHEMA))
        s.execute(text("DELETE FROM search_log WHERE created_at < now() - make_interval(days => :d)"),
                  {"d": LOG_RETENTION_DAYS})
        s.commit()
    except Exception as e:
        s.rollback()
        print(f"[search] search_log schema check failed: {str(e).splitlines()[0]}")
    finally:
        s.close()


def install(app) -> None:
    """Called from api.py: enable pg_trgm if possible (typo tolerance), mount routes."""
    global _TRGM
    s = get_session()
    try:
        from db.migrate import schema_managed
        if schema_managed():   # the extension comes from migrations; just see whether it's there
            _TRGM = bool(s.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).scalar())
            if not _TRGM:
                print("[search] pg_trgm not installed, typo matching off")
        else:
            s.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            s.commit()
            _TRGM = True
    except Exception as e:
        s.rollback()
        print(f"[search] pg_trgm unavailable, typo matching off: {str(e).splitlines()[0]}")
    finally:
        s.close()
    _ensure_schema()
    app.include_router(router)
