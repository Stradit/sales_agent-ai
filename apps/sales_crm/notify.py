"""Email notifications over SMTP (email_sender.py). apps/sales_crm/README.md §B.

Events are written to the `crm_notifications` outbox (idempotent via `dedupe_key`) inside the same
transaction as the change that caused them, and a background worker sends them. Nothing leaves the
server until an admin turns on `email_notifications_enabled` (CRM settings); while it is off, new
notifications are kept as status 'held' so the log shows what *would* have been sent, and they are
never sent retroactively. Each user can switch any kind off (`crm_notification_prefs`).

Kinds: see KINDS. The weekly pipeline email goes out on Mondays 07:00 IST.
No AI requests.
"""

import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

import auth
from apps.sales_crm import permissions
from db.connection import engine, get_session
from db.models.user import User

IST = ZoneInfo("Asia/Kolkata")
KINDS: Dict[str, Dict[str, str]] = {
    "partner_intro_update": {"label": "My introductions changed (partners)", "who": "partner"},
    "intro_assigned": {"label": "An introduction is assigned to me", "who": "internal"},
    "partner_note": {"label": "A partner added an update to my introduction", "who": "internal"},
    "triage_needed": {"label": "A partner submission needs linking to an account", "who": "internal"},
    "intro_stale": {"label": "One of my introductions went stale", "who": "internal"},
    "account_assigned": {"label": "An account is assigned to me", "who": "internal"},
    "capture_reauth": {"label": "My Microsoft 365 connection needs reconnecting", "who": "internal"},
    "weekly_pipeline": {"label": "Weekly pipeline email (Mondays)", "who": "internal"},
}
MAX_ATTEMPTS = 3
UNDELIVERABLE = (".invalid", "@example.com", "@example.org", "@example.net", ".test", ".local", "-selftest.co")
_sender = None          # tests replace this; default is email_sender.send_email
# Test safety: when set, only these user ids can ever get a queued email; everyone else is recorded as
# 'skipped (test run)'. Test scripts call suppress() first — a test must never email a real person, even
# when the org switch is on (2026-09-24 incident: test runs emailed 4 internal addresses).
_restrict_to: Optional[set] = None
if os.getenv("CRM_NOTIFY_SUPPRESS") == "1":
    _restrict_to = set()


def suppress(allow_user_ids=()) -> None:
    """Call at the start of any test / seed script. Only `allow_user_ids` may receive (test-sender) email."""
    global _restrict_to
    _restrict_to = set(allow_user_ids)


def base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def enabled(s) -> bool:
    return bool(s.execute(text("SELECT value FROM crm_settings WHERE key = 'email_notifications_enabled'")).scalar())


def _prefs(s, user_id: int) -> Dict[str, bool]:
    v = s.execute(text("SELECT settings FROM crm_notification_prefs WHERE user_id = :u"), {"u": user_id}).scalar()
    return v or {}


def enqueue(s, user_id: Optional[int], kind: str, subject: str, lines: List[str], link: Optional[str] = None,
            dedupe_key: Optional[str] = None, cta_label: str = "Open in StradIT") -> Optional[int]:
    """Queue one email to a user. Doesn't commit — it rides on the caller's transaction."""
    if kind not in KINDS or not user_id:
        return None
    u = s.execute(text("SELECT email, full_name, is_active FROM users WHERE id = :u"), {"u": user_id}).fetchone()
    if not u or not u[2] or not u[0]:
        return None
    status, reason = ("queued", None) if enabled(s) else ("held", None)
    if _restrict_to is not None and user_id not in _restrict_to:
        status, reason = "skipped", "Suppressed: created by a test run"
    elif _prefs(s, user_id).get(kind) is False:
        status, reason = "skipped", "Turned off in the user's notification settings"
    elif any(u[0].lower().endswith(x) for x in UNDELIVERABLE) and _restrict_to is None:
        status, reason = "skipped", "Test / reserved address"
    url = f"{base_url()}{link}" if link and link.startswith("/") else link
    greeting = f"Hi {(u[1] or '').split(' ')[0] or 'there'},"
    body = "\n".join([greeting, ""] + lines + ([f"\n{cta_label}: {url}"] if url else []) +
                     ["", "— Doshin by StradIT", "You can turn these emails off under Email & Calendar Sync → Notifications."])
    try:
        import email_sender
        html = email_sender.render_html(subject, [greeting] + lines, cta_label=cta_label if url else None, cta_url=url,
                                        footnote="You can turn these emails off under Email & Calendar Sync → Notifications.")
    except Exception:
        html = None
    return s.execute(text("""
        INSERT INTO crm_notifications (user_id, to_email, kind, subject, body, html, link, dedupe_key, status, error)
        VALUES (:u, :to, :k, :su, :b, :h, :l, :d, :st, :er)
        ON CONFLICT (dedupe_key) DO NOTHING RETURNING id"""),
        {"u": user_id, "to": u[0], "k": kind, "su": subject[:250], "b": body, "h": html, "l": url,
         "d": f"test:{dedupe_key}" if (dedupe_key and _restrict_to is not None) else dedupe_key, "st": status, "er": reason}).scalar()


def internal_triagers(s, account_id: Optional[int]) -> List[int]:
    """Who hears about unassigned partner submissions: the account owner if known, else managers + admins."""
    if account_id:
        owner = s.execute(text("SELECT owner_user_id FROM accounts WHERE id = :a"), {"a": account_id}).scalar()
        if owner:
            return [owner]
    return [r[0] for r in s.execute(text("""SELECT id FROM users WHERE is_active AND role IN ('sales_manager','super_admin')
                                            ORDER BY id"""))]


# ── Sending ───────────────────────────────────────────────────────────────────


def send_due(limit: int = 50) -> Dict[str, int]:
    import email_sender
    sender = _sender or email_sender.send_email
    s = get_session()
    stats: Dict[str, int] = {}
    try:
        if not enabled(s):
            n = s.execute(text("UPDATE crm_notifications SET status = 'held' WHERE status = 'queued'")).rowcount
            s.commit()
            return {"held": n} if n else {}
        rows = s.execute(text("""SELECT id, to_email, subject, body, html, attempts FROM crm_notifications
                                 WHERE status = 'queued' ORDER BY id LIMIT :l FOR UPDATE SKIP LOCKED"""), {"l": limit}).fetchall()
        configured = email_sender.is_configured() or _sender is not None
        for nid, to, subject, body, html, attempts in rows:
            if _sender is None and any(to.lower().endswith(x) for x in UNDELIVERABLE):
                s.execute(text("UPDATE crm_notifications SET status = 'skipped', error = 'Test / reserved address — never sent' WHERE id = :i"),
                          {"i": nid})
                stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            ok = sender(to, subject, body, html_body=html)
            if ok:
                status, err = "sent", None
            elif not configured:
                status, err = "logged", "SMTP not configured — written to the server log"
            elif attempts + 1 >= MAX_ATTEMPTS:
                status, err = "failed", "SMTP send failed 3 times (see server log)"
            else:
                status, err = "queued", "SMTP send failed — will retry"
            s.execute(text("""UPDATE crm_notifications SET status = :st, error = :e, attempts = attempts + 1,
                              sent_at = CASE WHEN :st IN ('sent','logged') THEN now() ELSE sent_at END WHERE id = :i"""),
                      {"st": status, "e": err, "i": nid})
            stats[status] = stats.get(status, 0) + 1
        s.commit()
        return stats
    finally:
        s.close()


# ── Weekly pipeline email ─────────────────────────────────────────────────────


def queue_weekly(now: Optional[datetime] = None, only_user_ids: Optional[set] = None) -> int:
    """One email per internal user with open deals in their scope; managers' covers their team.
    `only_user_ids` limits the run (tests)."""
    from apps.sales_deals.api import digest_data
    now = now or datetime.now(IST)
    week = now.strftime("%G-W%V")
    s = get_session()
    n = 0
    try:
        users = s.execute(text("""SELECT id, role FROM users WHERE is_active AND role IN ('user','sales_manager')""")).fetchall()
        for uid, role in users:
            if only_user_ids is not None and uid not in only_user_ids:
                continue
            acl = auth.get_accessible_account_ids(s, uid)
            if not acl:
                continue
            g = digest_data(s, acl, None if role == "sales_manager" else uid, days=7)
            t = g["totals"]
            if not t["open"] and not g["closed"]:
                continue

            def section(title, items):
                return [f"{title} ({len(items)}):"] + [f"  • {x['name']} — {x['account_name']} · {x['why'] or x['stage_label']}"
                                                       for x in items[:6]] if items else []
            lines = [f"{'Your team' if role == 'sales_manager' else 'You'} have {t['open']} open deal(s) worth "
                     f"{t['value']:,.0f} USD ({t['weighted']:,.0f} weighted)."]
            for title, key in (("At risk", "at_risk"), ("Overdue next steps", "overdue"), ("Stuck more than 30 days", "stuck"),
                               ("Closing in the next 30 days", "closing_soon"), ("Closed last week", "closed")):
                lines += section(title, g[key])
            if enqueue(s, uid, "weekly_pipeline", f"Your pipeline this week — {t['open']} open deals", lines,
                       link="/deals", dedupe_key=f"weekly:{uid}:{week}", cta_label="Open the pipeline"):
                n += 1
        s.commit()
        return n
    finally:
        s.close()


def _worker() -> None:
    while True:
        try:
            with engine.connect() as conn:
                if conn.execute(text("SELECT pg_try_advisory_lock(84214)")).scalar():
                    try:
                        now = datetime.now(IST)
                        if now.weekday() == 0 and now.hour >= 7:
                            queue_weekly(now)          # idempotent per ISO week (dedupe_key)
                        r = send_due()
                        if r.get("sent") or r.get("failed"):
                            print(f"[crm] notifications: {r}")
                    finally:
                        conn.execute(text("SELECT pg_advisory_unlock(84214)"))
        except Exception as e:
            print(f"[crm] notification worker failed: {e}")
        time.sleep(60)


def start_background() -> None:
    threading.Thread(target=_worker, name="crm-notify", daemon=True).start()


# ── API ───────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/crm/notifications", tags=["CRM notifications"])


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _kinds_for(role: str) -> List[str]:
    if role == "partner":
        return ["partner_intro_update"]
    return [k for k, v in KINDS.items() if v["who"] == "internal"]


@router.get("/prefs")
def get_prefs(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    p = _prefs(s, user.id)
    role = user.role if auth.AUTH_ENFORCED else "super_admin"
    return {"enabled_org_wide": enabled(s), "email": user.email,
            "kinds": [{"key": k, "label": KINDS[k]["label"], "on": p.get(k, True)} for k in _kinds_for(role)]}


class PrefsIn(BaseModel):
    settings: Dict[str, bool]


@router.put("/prefs")
def put_prefs(body: PrefsIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    import json
    allowed = set(_kinds_for(user.role if auth.AUTH_ENFORCED else "super_admin"))
    bad = [k for k in body.settings if k not in allowed]
    if bad:
        raise HTTPException(400, f"Unknown notification type(s): {bad}")
    s.execute(text("""INSERT INTO crm_notification_prefs (user_id, settings) VALUES (:u, CAST(:v AS jsonb))
                      ON CONFLICT (user_id) DO UPDATE SET settings = crm_notification_prefs.settings || EXCLUDED.settings,
                      updated_at = now()"""), {"u": user.id, "v": json.dumps(body.settings)})
    s.commit()
    return get_prefs(user, s)


@router.get("/mine")
def mine(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    return [dict(r) for r in s.execute(text("""SELECT id, kind, subject, status, created_at, sent_at, link FROM crm_notifications
                                               WHERE user_id = :u ORDER BY id DESC LIMIT 50"""), {"u": user.id}).mappings()]


@router.get("/admin")
def admin_log(user: User = Depends(auth.require_role("super_admin")), s=Depends(_session)):
    import email_sender
    rows = [dict(r) for r in s.execute(text("""
        SELECT n.id, n.kind, n.subject, n.to_email, n.status, n.error, n.attempts, n.created_at, n.sent_at,
               coalesce(u.full_name, u.email) AS user_name
        FROM crm_notifications n LEFT JOIN users u ON u.id = n.user_id ORDER BY n.id DESC LIMIT 200""")).mappings()]
    counts = {r[0]: r[1] for r in s.execute(text("SELECT status, count(*) FROM crm_notifications GROUP BY 1"))}
    return {"enabled": enabled(s), "smtp_configured": email_sender.is_configured(), "counts": counts, "recent": rows}


@router.post("/test")
def send_test(user: User = Depends(permissions.require_roles("sales_manager", "user")), s=Depends(_session)):
    """Sends one test email to yourself right now (even while notifications are off) to check SMTP."""
    import email_sender
    ok = (_sender or email_sender.send_email)(
        user.email, "StradIT notifications — test email",
        "This is a test from Doshin by StradIT. If you received it, email notifications can reach you.")
    status = "sent" if ok else ("logged" if not email_sender.is_configured() else "failed")
    s.execute(text("""INSERT INTO crm_notifications (user_id, to_email, kind, subject, body, status, sent_at)
                      VALUES (:u, :to, 'test', 'Test email', 'Test email', :st, now())"""), {"u": user.id, "to": user.email, "st": status})
    s.commit()
    return {"status": status, "to": user.email}
