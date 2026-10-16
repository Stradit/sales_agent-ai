"""
Command Center generator — builds the Priority Signal Feed, This Week's
Playbook and the "Signal velocity" KPI from data ALREADY in the DB:

  * cxo_movements        -> "Exec change" signals (one per movement)
  * linkedin_jobs        -> "Hiring" signals (one per account per week with 3+ new roles)
  * posts (channel=news) -> "News" signals (top 5 per account per week)
  * opportunity_signals  -> "Opportunity" signals (active growth/expansion themes)

Rule-based on purpose (no LLM call), so Generate is fast, free and repeatable.
Nothing here is invented: an account with no pipeline/content runs simply
contributes no signals. Triggered by POST /api/command-center/generate; the
result is stored as a CommandCenterSnapshot so the dashboard can reload it.

collect_raw_signals() is the only part that touches the DB; the scoring and
playbook builders work on plain dicts.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional

from db.models import Account, CxoMovement, LinkedInJob, OpportunitySignal, Post
from serializers.account_serializer import slugify

CATEGORIES = ["Exec change", "Hiring", "News", "Opportunity"]

FEED_WINDOW_DAYS = 7          # the feed only shows the last week
TREND_WEEKS = 8               # sparkline length for signal velocity
PLAYBOOK_SIZE = 5
HIRING_MIN_NEW_ROLES = 3
NEWS_PER_ACCOUNT_PER_WEEK = 5

_C_LEVEL = re.compile(r"\b(chief|ceo|cfo|cto|cio|coo|cmo|ciso|cdo|cro|president|chair(man|woman|person)?)\b", re.I)
_SENIOR = re.compile(r"\b(evp|svp|executive vice president|senior vice president|head of|managing director|vp|vice president|global head)\b", re.I)
_NEWS_KEYWORDS = re.compile(
    r"\b(acqui\w*|merger|partner\w*|launch\w*|funding|invest\w*|layoff\w*|restructur\w*|earnings|"
    r"ai|artificial intelligence|cloud|cyber\w*|digital asset\w*|blockchain|tokeni[sz]\w*|regulat\w*|"
    r"moderni[sz]\w*|transformation|outsourc\w*)\b",
    re.I,
)


# ── Date helpers ──────────────────────────────────────────────

def _parse_date(value: Any) -> Optional[datetime]:
    """Best-effort parse of the mixed date strings scraped sources store
    (ISO dates, ISO datetimes, RFC 2822). Returns an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _first_date(*values: Any) -> Optional[datetime]:
    for v in values:
        d = _parse_date(v)
        if d:
            return d
    return None


def _age_days(d: datetime, now: datetime) -> float:
    return max(0.0, (now - d).total_seconds() / 86400)


def _clamp(n: float, lo: float = 0, hi: float = 100) -> int:
    return int(round(max(lo, min(hi, n))))


# ── DB collection ─────────────────────────────────────────────

def _target_key_map(accounts: Iterable[Account]) -> Dict[str, Account]:
    """Same matching api.py's _build_target_key_to_account_map uses —
    scraped tables reference accounts by a loose target_key."""
    mapping: Dict[str, Account] = {}
    for a in accounts:
        for candidate in (
            a.key,
            (a.stock_symbol or "").lower() or None,
            slugify(a.display_name) if a.display_name else None,
            slugify(a.legal_name) if a.legal_name else None,
        ):
            if candidate:
                mapping[candidate] = a
    return mapping


def _account_name(a: Account) -> str:
    return a.display_name or a.legal_name or a.key


def collect_raw_signals(session, account_ids: Optional[List[int]], now: datetime) -> List[Dict[str, Any]]:
    """Reads every source table for the given accounts (None = all) and
    returns one raw dict per candidate signal, unscored."""
    q = session.query(Account)
    if account_ids is not None:
        if not account_ids:
            return []
        q = q.filter(Account.id.in_(account_ids))
    accounts = q.all()
    if not accounts:
        return []
    tmap = _target_key_map(accounts)
    keys = list(tmap.keys())
    since = now - timedelta(weeks=TREND_WEEKS)
    raw: List[Dict[str, Any]] = []

    for m in session.query(CxoMovement).filter(CxoMovement.target_key.in_(keys)).all():
        acct = tmap.get(m.target_key)
        d = _first_date(m.effective_date, m.published_at, m.first_seen)
        if not acct or not d or d < since or d > now + timedelta(days=1):
            continue
        raw.append({
            "kind": "exec", "account_id": acct.id, "account_name": _account_name(acct), "detected_at": d,
            "ref_id": m.id, "person": m.person_name, "designation": m.designation or "",
            "event_type": (m.event_type or "").lower().strip(), "context": (m.context or "").strip(),
            "url": m.article_url,
        })

    jobs_by_week: Dict[tuple, List[LinkedInJob]] = defaultdict(list)
    for j in session.query(LinkedInJob).filter(LinkedInJob.target_key.in_(keys)).all():
        acct = tmap.get(j.target_key)
        d = _first_date(j.posted_date, j.first_seen)
        if not acct or not d or d < since or d > now + timedelta(days=1):
            continue
        week = int(_age_days(d, now) // 7)
        jobs_by_week[(acct.id, week)].append(j)
    accounts_by_id = {a.id: a for a in accounts}
    for (acct_id, week), jobs in jobs_by_week.items():
        if len(jobs) < HIRING_MIN_NEW_ROLES:
            continue
        acct = accounts_by_id[acct_id]
        newest = max(_first_date(j.posted_date, j.first_seen) for j in jobs)
        raw.append({
            "kind": "hiring", "account_id": acct_id, "account_name": _account_name(acct), "detected_at": newest,
            "ref_id": None, "count": len(jobs), "top_titles": [j.title for j in jobs if j.title][:3],
            "url": None,
        })

    news = (
        session.query(Post)
        .filter(Post.channel == "news", Post.target_key.in_(keys), Post.first_seen >= since)
        .all()
    )
    for p in news:
        acct = tmap.get(p.target_key)
        r = p.raw or {}
        body = (p.body or "").strip()
        title = r.get("title") or (body.splitlines()[0] if body else None)
        d = _first_date(p.published_at, p.first_seen)
        if not acct or not title or not d or d < since or d > now + timedelta(days=1):
            continue
        summary_candidate = r.get("summary") or r.get("description") or (body if body and body != title else "")
        raw.append({
            "kind": "news", "account_id": acct.id, "account_name": _account_name(acct), "detected_at": d,
            "ref_id": p.id, "title": title, "source": p.author or r.get("source") or "", "url": p.post_url,
            "summary_text": summary_candidate,
        })

    opps = (
        session.query(OpportunitySignal)
        .filter(OpportunitySignal.account_id.in_([a.id for a in accounts]),
                OpportunitySignal.status == "active", OpportunitySignal.first_seen >= since)
        .all()
    )
    for o in opps:
        acct = accounts_by_id.get(o.account_id)
        details = o.details or {}
        raw.append({
            "kind": "opportunity", "account_id": o.account_id, "account_name": _account_name(acct),
            "detected_at": _parse_date(o.first_seen), "ref_id": o.id, "title": o.title,
            "theme": o.category,
            "summary": (details.get("rationale") or details.get("summary") or details.get("description") or "").strip(),
            "url": None,
        })
    return raw


# ── Scoring ───────────────────────────────────────────────────

def _recency_factor(age_days: float) -> float:
    return max(0.6, 1 - age_days / 20)


def score_signal(r: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Turns one raw signal into the feed shape the frontend renders:
    {id, account_id, account_name, category, title, summary, score, detected_at, url, ...}."""
    age = _age_days(r["detected_at"], now)
    kind = r["kind"]
    extra: Dict[str, Any] = {}

    if kind == "exec":
        designation = r["designation"]
        base = 60 + (25 if _C_LEVEL.search(designation) else 15 if _SENIOR.search(designation) else 0)
        base += 10 if r["event_type"] in ("joined", "promoted") else 5
        verb = {"joined": "joined", "promoted": "promoted to", "resigned": "left", "retired": "retired from"}.get(
            r["event_type"], r["event_type"] or "moved")
        role = f" as {designation}" if designation and r["event_type"] in ("joined",) else (
            f" {designation}" if designation and r["event_type"] == "promoted" else
            f" the {designation} role" if designation else "")
        title = f"{r['person']} {verb}{role}".strip()
        summary = (r["context"].strip() if r.get("context") else
                   "New leaders reset vendor priorities in their first 90 days — reach out early.")
        category = "Exec change"
        extra = {"person": r["person"], "designation": designation, "event_type": r["event_type"]}
    elif kind == "hiring":
        base = 40 + min(40, r["count"] * 2)
        title = f"Hiring push: {r['count']} new roles posted"
        summary = ("Top roles: " + "; ".join(r["top_titles"])) if r["top_titles"] else "Active hiring this week."
        category = "Hiring"
        extra = {"count": r["count"], "top_titles": r["top_titles"]}
    elif kind == "news":
        hits = {m.group(0).lower() for m in _NEWS_KEYWORDS.finditer(r["title"])}
        base = 35 + min(30, 10 * len(hits))
        title = r["title"]
        summary = (r.get("summary_text") or "").strip() or f"In the news{(' via ' + r['source']) if r['source'] else ''}."
        category = "News"
        extra = {"keywords": sorted(hits)}
    else:
        base = 55
        title = r["title"]
        summary = (r.get("summary") or "").strip() or "Recurring growth theme detected in this account's content."
        category = "Opportunity"
        extra = {"theme": r.get("theme")}

    return {
        "id": f"{kind}-{r['account_id']}-{r['ref_id'] if r['ref_id'] is not None else int(r['detected_at'].timestamp())}",
        "kind": kind,
        "account_id": r["account_id"],
        "account_name": r["account_name"],
        "category": category,
        "title": title,
        "summary": summary,
        "score": _clamp(base * _recency_factor(age)),
        "detected_at": r["detected_at"].isoformat(),
        "url": r.get("url"),
        **extra,
    }


def _cap_news(scored: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Keeps the top N news items per account per week so one noisy
    account can't flood the feed."""
    buckets: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    out = []
    for s in scored:
        if s["kind"] != "news":
            out.append(s)
            continue
        week = int(_age_days(_parse_date(s["detected_at"]), now) // 7)
        buckets[(s["account_id"], week)].append(s)
    for items in buckets.values():
        out.extend(sorted(items, key=lambda x: x["score"], reverse=True)[:NEWS_PER_ACCOUNT_PER_WEEK])
    return out


# ── Velocity & playbook ───────────────────────────────────────

def compute_velocity(signals: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """Signals detected per 7-day window. trend is oldest -> newest,
    so trend[-1] is this week."""
    counts = [0] * TREND_WEEKS
    for s in signals:
        week = int(_age_days(_parse_date(s["detected_at"]), now) // 7)
        if 0 <= week < TREND_WEEKS:
            counts[TREND_WEEKS - 1 - week] += 1
    this_week, last_week = counts[-1], counts[-2]
    if last_week:
        delta = round((this_week - last_week) / last_week * 100)
    else:
        delta = 100 if this_week else 0
    return {"this_week": this_week, "last_week": last_week, "delta_pct": delta, "trend": counts}


def _play_for(sig: Dict[str, Any], now: datetime) -> Dict[str, str]:
    acct = sig["account_name"]
    age = int(_age_days(_parse_date(sig["detected_at"]), now))
    when = "today" if age == 0 else f"{age} day{'s' if age != 1 else ''} ago"
    kind = sig["kind"]
    if kind == "exec":
        who = sig["person"] + (f" ({sig['designation']})" if sig.get("designation") else "")
        exec_ctx = f" {sig['summary']}" if sig.get("summary") and not sig["summary"].startswith("New leaders reset") else ""
        if sig["event_type"] in ("joined", "promoted"):
            return {
                "title": f"Send a congratulations note to {who} at {acct}",
                "rationale": f"Leadership change {when} — new leaders reset vendor priorities in their first 90 days.{exec_ctx}".strip(),
            }
        return {
            "title": f"Find out who replaces {who} at {acct}",
            "rationale": f"Departure {when} — the relationship and any open deal need a new owner.{exec_ctx}".strip(),
        }
    if kind == "hiring":
        return {
            "title": f"Reach the hiring leaders behind {acct}'s {sig['count']} new roles",
            "rationale": f"Hiring push detected {when}. {sig['summary']}".strip(),
        }
    if kind == "news":
        news_detail = f": {sig['summary']}" if sig.get("summary") and not sig["summary"].startswith("In the news") else ""
        return {
            "title": f'Reference "{sig["title"]}" in outreach to {acct}',
            "rationale": f'News signal {when} — {sig["title"]}{news_detail}. A timely, specific reason to initiate outreach and align with {acct}\'s active priorities.'.strip(),
        }
    return {
        "title": f'Build a point of view on "{sig["title"]}" for {acct}',
        "rationale": f"Active growth theme first seen {when}. {sig['summary']}".strip(),
    }


def build_playbook(signals: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Top-scoring signals from this week, at most one play per
    (account, category), ranked by score."""
    seen = set()
    plays = []
    for s in sorted(signals, key=lambda x: x["score"], reverse=True):
        if _age_days(_parse_date(s["detected_at"]), now) > FEED_WINDOW_DAYS:
            continue
        key = (s["account_id"], s["category"])
        if key in seen:
            continue
        seen.add(key)
        play = _play_for(s, now)
        plays.append({
            "rank": len(plays) + 1,
            "title": play["title"],
            "rationale": play["rationale"],
            "impact": "high" if s["score"] >= 75 else "medium",
            "account_id": s["account_id"],
            "account_name": s["account_name"],
            "signal_id": s["id"],
        })
        if len(plays) >= PLAYBOOK_SIZE:
            break
    return plays


def build_from_raw(raw: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    scored = _cap_news([score_signal(r, now) for r in raw if r.get("detected_at")], now)
    velocity = compute_velocity(scored, now)
    feed = sorted(
        (s for s in scored if _age_days(_parse_date(s["detected_at"]), now) <= FEED_WINDOW_DAYS),
        key=lambda x: x["score"], reverse=True,
    )
    return {
        "signals": feed,
        "playbook": build_playbook(feed, now),
        "velocity": velocity,
        "source_counts": {k: sum(1 for r in raw if r["kind"] == k) for k in ("exec", "hiring", "news", "opportunity")},
    }


def generate(session, account_ids: Optional[List[int]], now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    return build_from_raw(collect_raw_signals(session, account_ids, now), now)
