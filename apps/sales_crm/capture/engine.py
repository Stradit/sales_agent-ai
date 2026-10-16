"""Capture engine: provider records → matched, de-duplicated CRM activities (README §4.2, §4.4).

Matching (in this order):
  1. every participant email exactly equal to a contact's work email → contact + its account
  2. otherwise the email's domain equals an account's domain / website → account only
  3. open deals at those accounts where a matched contact is on the buying committee → deal
  4. introductions for matched contacts are updated by activities._after_write (meeting held)
Never stored: items with no external match, internal-only items (setting), items whose only matches are
accounts the user can't access, excluded domains, and free-mail / personal addresses (never matched).
Idempotent: (source, owner, external_id) upsert; the same message / meeting captured from two
colleagues' mailboxes is stored once (first one wins).
"""

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy import text

import auth
from apps.sales_copilot.privacy import FREEMAIL
from apps.sales_crm import activities as acts
from apps.sales_crm.capture import crypto, microsoft
from db.connection import engine, get_session

SYNC_EVERY = timedelta(minutes=10)
SOURCE = {"email": "outlook", "meeting": "outlook_calendar"}


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower().strip() if "@" in (email or "") else ""


def _host(url_or_domain: Optional[str]) -> str:
    v = (url_or_domain or "").strip().lower()
    if not v:
        return ""
    if "://" in v:
        v = urlparse(v).netloc
    return re.sub(r"^www\.", "", v.split("/")[0].split(":")[0])


class Directory:
    """Lookups loaded once per sync run."""

    def __init__(self, s, user_id: int, mailbox: str):
        role = s.execute(text("SELECT role FROM users WHERE id = :u"), {"u": user_id}).scalar()
        self.scope = set(r[0] for r in s.execute(text("SELECT id FROM accounts"))) if role == "super_admin" \
            else set(auth.get_accessible_account_ids(s, user_id))
        self.internal = {_domain(r[0]) for r in s.execute(text("SELECT email FROM users WHERE role <> 'partner'")) if r[0]}
        self.internal.add(_domain(mailbox))
        self.internal -= set(FREEMAIL)
        self.persona_by_email: Dict[str, Tuple[int, int, str]] = {}
        for pid, aid, email, name in s.execute(text("""SELECT id, account_id, lower(email), coalesce(full_name, display_name)
                                                      FROM personas WHERE email IS NOT NULL AND email LIKE '%@%'""")):
            if _domain(email) not in FREEMAIL:
                self.persona_by_email.setdefault(email, (pid, aid, name))
        self.account_by_domain: Dict[str, int] = {}
        for aid, dom, pdom, web in s.execute(text("SELECT id, domain, primary_domain, website_url FROM accounts")):
            for d in (_host(dom), _host(pdom), _host(web)):
                if d and d not in FREEMAIL:
                    self.account_by_domain.setdefault(d, aid)

    def account_for_domain(self, dom: str) -> Optional[int]:
        parts = dom.split(".")
        for i in range(len(parts) - 1):              # sub.bny.com → bny.com
            aid = self.account_by_domain.get(".".join(parts[i:]))
            if aid:
                return aid
        return None


def match(rec: Dict[str, Any], d: Directory, settings: Dict[str, Any]) -> Tuple[Optional[List[Tuple[str, int, str]]], List[Dict[str, Any]], str]:
    """→ (links or None, annotated participants, reason when None)."""
    excluded = {x.lower().lstrip("@") for x in settings.get("exclude_domains") or []}
    links: List[Tuple[str, int, str]] = []
    people = []
    external = 0
    for p in rec["participants"]:
        email, dom = p["email"], _domain(p["email"])
        entry = {"name": p.get("name") or email, "email": email, "is_internal": dom in d.internal}
        if dom in excluded:
            return None, [], "excluded_domain"
        if entry["is_internal"]:
            people.append(entry)
            continue
        external += 1
        if dom in FREEMAIL:
            entry["email"] = None                 # personal address: never stored, never matched
            people.append(entry)
            continue
        hit = d.persona_by_email.get(email)
        if hit and hit[1] in d.scope:
            entry.update(persona_id=hit[0], name=hit[2])
            links.append(("persona", hit[0], "email_exact"))
        else:
            aid = d.account_for_domain(dom)
            if aid and aid in d.scope:
                links.append(("account", aid, "domain"))
        people.append(entry)
    if not external:
        return None, people, "internal_only" if settings.get("exclude_internal_only", True) else "no_external"
    if not links:
        return None, people, "unmatched"
    return list(dict.fromkeys(links)), people, ""


def _deal_links(s, links: List[Tuple[str, int, str]]) -> List[Tuple[str, int, str]]:
    pids = [oid for t, oid, _ in links if t == "persona"]
    if not pids:
        return []
    rows = s.execute(text("""SELECT DISTINCT ON (d.account_id) d.id FROM deals d
                             JOIN deal_stakeholders ds ON ds.deal_id = d.id AND ds.persona_id = ANY(:p)
                             WHERE d.stage NOT IN ('won','lost') ORDER BY d.account_id, d.updated_at DESC"""), {"p": pids}).fetchall()
    return [("deal", r[0], "rule") for r in rows]


def store(s, user_id: int, rec: Dict[str, Any], d: Directory, settings: Dict[str, Any], allow_bodies: bool) -> str:
    """Upsert one normalised record. Returns an outcome key for the run stats."""
    source = SOURCE[rec["kind"]]
    if rec.get("removed"):
        n = s.execute(text("""DELETE FROM activities WHERE owner_user_id = :u AND source = :src
                              AND (external_id = :e OR metadata ->> 'graph_id' = :e)"""),
                      {"u": user_id, "src": source, "e": rec["external_id"]}).rowcount
        return "removed" if n else "ignored"
    links, people, reason = match(rec, d, settings)
    if links is None:
        return f"skipped_{reason}"
    existing = s.execute(text("SELECT id, owner_user_id FROM activities WHERE source = :src AND external_id = :e"),
                         {"src": source, "e": rec["external_id"]}).fetchall()
    mine = next((r[0] for r in existing if r[1] == user_id), None)
    if existing and mine is None:
        return "duplicate"                           # already captured from a colleague's mailbox
    body = rec.get("body") if (allow_bodies and settings.get("store_bodies")) else None
    meta = {"graph_id": rec.get("graph_id"), "online": rec.get("online"), "captured": True}
    values = {"t": "email" if rec["kind"] == "email" else "meeting", "d": rec["direction"], "su": rec["subject"],
              "sm": rec.get("snippet"), "b": body, "o": rec["occurred_at"], "du": rec.get("duration_min"), "u": user_id,
              "src": source, "e": rec["external_id"], "th": rec.get("thread_id"), "p": json.dumps(people), "m": json.dumps(meta)}
    if mine:
        s.execute(text("""UPDATE activities SET direction = :d, subject = :su, summary = :sm, body = coalesce(:b, body),
                          occurred_at = :o, duration_min = :du, participants = CAST(:p AS jsonb),
                          metadata = metadata || CAST(:m AS jsonb), updated_at = now() WHERE id = :id"""), {**values, "id": mine})
        s.execute(text("DELETE FROM activity_links WHERE activity_id = :a AND matched_by NOT IN ('manual')"), {"a": mine})
        aid, outcome = mine, "updated"
    else:
        aid = s.execute(text("""INSERT INTO activities (type, direction, subject, summary, body, occurred_at, duration_min, owner_user_id,
                                                        source, external_id, thread_id, visibility, participants, metadata)
                                VALUES (:t, :d, :su, :sm, :b, :o, :du, :u, :src, :e, :th, 'team', CAST(:p AS jsonb), CAST(:m AS jsonb))
                                RETURNING id"""), values).scalar()
        outcome = "stored"
    acts._add_links(s, aid, links + _deal_links(s, links))
    acts._after_write(s, aid, user_id)
    return outcome


# ── Per-connection sync ───────────────────────────────────────────────────────


def _allow_bodies(s) -> bool:
    return bool(s.execute(text("SELECT value FROM crm_settings WHERE key = 'capture_allow_bodies'")).scalar())


def sync_connection(conn_id: int, transport=None) -> Dict[str, Any]:
    s = get_session()
    try:
        c = s.execute(text("SELECT * FROM capture_connections WHERE id = :i"), {"i": conn_id}).mappings().fetchone()
        if not c:
            return {"error": "connection not found"}
        c = dict(c)
        stats: Dict[str, int] = {}
        settings = c["settings"] or {}
        allow_bodies = _allow_bodies(s)
        try:
            tokens = crypto.unseal(c["token_encrypted"])
        except ValueError as e:
            s.execute(text("UPDATE capture_connections SET status = 'needs_reauth', error = :e WHERE id = :i"), {"e": str(e), "i": conn_id})
            s.commit()
            return {"error": str(e)}

        def save_tokens(t):
            s.execute(text("UPDATE capture_connections SET token_encrypted = :t WHERE id = :i"), {"t": crypto.seal(t), "i": conn_id})
            s.commit()

        g = microsoft.Graph(tokens, on_refresh=save_tokens, transport=transport)
        cursors = dict(c["cursors"] or {})
        directory = Directory(s, c["user_id"], c["account_email"])
        streams = ([("inbox", "email"), ("sent", "email")] if settings.get("capture_email", True) else []) + \
                  ([("calendar", "meeting")] if settings.get("capture_calendar", True) else [])
        try:
            for stream, _kind in streams:
                try:
                    items, new_cursor = g.delta(stream, cursors.get(stream), bodies=allow_bodies and settings.get("store_bodies", False))
                except LookupError:
                    items, new_cursor = g.delta(stream, None, bodies=allow_bodies and settings.get("store_bodies", False))
                stats[f"fetched_{stream}"] = len(items)
                for it in items:
                    rec = microsoft.normalize_event(it, c["account_email"]) if stream == "calendar" \
                        else microsoft.normalize_message(it, c["account_email"], stream)
                    if rec is None:
                        continue
                    out = store(s, c["user_id"], rec, directory, settings, allow_bodies)
                    stats[out] = stats.get(out, 0) + 1
                cursors[stream] = new_cursor
                s.execute(text("UPDATE capture_connections SET cursors = CAST(:c AS jsonb) WHERE id = :i"),
                          {"c": json.dumps(cursors), "i": conn_id})
                s.commit()
            status, error, nxt = "active", None, datetime.now(timezone.utc) + SYNC_EVERY
        except microsoft.AuthError as e:
            s.rollback()
            status, error, nxt = "needs_reauth", f"Microsoft sign-in expired or was revoked — reconnect ({e})"[:500], datetime.now(timezone.utc) + timedelta(days=365)
            from apps.sales_crm import notify
            notify.enqueue(s, c["user_id"], "capture_reauth", "Reconnect your Microsoft 365 mailbox",
                           ["Email & calendar capture stopped because the Microsoft sign-in expired or was revoked.",
                            "Reconnect to keep meetings and emails logging automatically."],
                           link="/email-sync", dedupe_key=f"reauth:{conn_id}:{datetime.now(timezone.utc):%Y-%m-%d}",
                           cta_label="Reconnect")
        except microsoft.RateLimited as e:
            s.rollback()
            status, error, nxt = "active", str(e), datetime.now(timezone.utc) + timedelta(seconds=max(60, e.retry_after))
        except Exception as e:  # network / Graph error: keep cursors, back off
            s.rollback()
            status, error, nxt = "error", f"{type(e).__name__}: {e}"[:500], datetime.now(timezone.utc) + timedelta(minutes=30)
        finally:
            g.close()
        s.execute(text("""UPDATE capture_connections SET status = :st, error = :er, last_sync_at = now(), next_sync_at = :n,
                          last_stats = CAST(:stats AS jsonb) WHERE id = :i"""),
                  {"st": status, "er": error, "n": nxt, "stats": json.dumps(stats), "i": conn_id})
        s.commit()
        return {"status": status, "error": error, **stats}
    finally:
        s.close()


def _worker() -> None:
    while True:
        try:
            if microsoft.configured():
                with engine.connect() as conn:
                    if conn.execute(text("SELECT pg_try_advisory_lock(84213)")).scalar():
                        try:
                            due = conn.execute(text("""SELECT id FROM capture_connections
                                                       WHERE status IN ('active','error') AND next_sync_at <= now()
                                                       ORDER BY next_sync_at LIMIT 20""")).fetchall()
                            for (cid,) in due:
                                r = sync_connection(cid)
                                if r.get("stored") or r.get("error"):
                                    print(f"[crm] capture #{cid}: {r}")
                        finally:
                            conn.execute(text("SELECT pg_advisory_unlock(84213)"))
        except Exception as e:
            print(f"[crm] capture worker failed: {e}")
        time.sleep(60)


def start_background() -> None:
    threading.Thread(target=_worker, name="crm-capture", daemon=True).start()
