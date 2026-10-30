"""Scheduled jobs — a small in-app scheduler, configured and watched from the admin "Scheduled jobs" tab.

Jobs
  sources_refresh          scrape the chosen content-pipeline channels for every account (apps/content_pipeline
                           `main.py scrape --all --only …`). Default channel: sec (free). news / linkedin_jobs /
                           linkedin cost Apify credits, so an admin opts in. Needs the pipeline code next to this
                           app; where it isn't installed (the main Docker image leaves it out) the job reports
                           "unavailable" instead of failing.
  command_center_refresh   rebuild every Command Center user's snapshot from the data in the DB
                           (services/command_center_service.py — rule-based, no AI, free). Also runs right after a
                           successful sources_refresh, so new signals show up at once.

How it runs
  One background thread checks every few minutes which jobs are due: last run + interval, both read from the DB,
  so a restart or deploy catches up instead of skipping. A Postgres advisory lock lets only one server process run
  jobs. Every run (scheduled or "Run now") is logged in scheduled_job_runs (migrations/versions/0008).
  SCHEDULER_ENABLED=0 switches the thread off (tests, one-off scripts); "Run now" still works.
"""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from db.connection import engine, get_session
from db.models.user import User

CHECK_SECONDS = 300            # how often the thread looks for due jobs
STARTUP_DELAY_SECONDS = 90     # let the app finish starting first
STALE_RUN_HOURS = 4            # a "running" row older than this was interrupted (restart mid-run)
LOCK_KEY = 84301               # pg advisory lock: one scheduler across server processes
PIPELINE_DIR = Path(__file__).resolve().parents[1] / "apps" / "content_pipeline"

# Content-pipeline channels the source refresh may scrape, and what each costs (apps/content_pipeline/API.md)
CHANNELS = {
    "sec": "Free",
    "rss": "Free",
    "news": "Apify credits per article",
    "linkedin_jobs": "Apify, about $1 per 1,000 jobs",
    "linkedin": "Apify credits",
}


def _enabled_by_env() -> bool:
    return os.getenv("SCHEDULER_ENABLED", "1") not in ("0", "false", "False")


# ── Jobs ──────────────────────────────────────────────────────────────────────

def pipeline_available() -> Optional[str]:
    """None when the content pipeline can run here, else why not."""
    if not (PIPELINE_DIR / "main.py").exists():
        return "The content pipeline isn't installed next to this app (it runs in its own container)."
    return None


def run_sources_refresh(session, options: Dict[str, Any]) -> Dict[str, Any]:
    why = pipeline_available()
    if why:
        return {"skipped": why}
    channels = [c for c in options.get("channels") or ["sec"] if c in CHANNELS]
    if not channels:
        return {"skipped": "No channels selected."}
    limit = max(1, min(int(options.get("limit") or 20), 50))
    cmd = [sys.executable, "main.py", "scrape", "--all", "--only", ",".join(channels), "--limit", str(limit)]
    proc = subprocess.run(cmd, cwd=str(PIPELINE_DIR), capture_output=True, text=True, timeout=3 * 3600,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"}, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    failed = out.count("❌")
    scraped = out.count("SCRAPE ·")
    summary = {"channels": channels, "accounts": scraped, "accounts_failed": failed, "exit_code": proc.returncode,
               "output_tail": out[-1500:]}
    if proc.returncode != 0 and (not scraped or failed >= scraped):
        raise RuntimeError(f"Scrape failed for every account (exit {proc.returncode}): {out[-600:]}")
    return summary


def run_command_center_refresh(session, options: Dict[str, Any]) -> Dict[str, Any]:
    from services import command_center_service
    users = session.query(User).filter(User.is_active.is_(True)).all()
    done = signals = skipped = 0
    for u in users:
        if u.role == "partner" or (u.role != "super_admin" and u.has_command_center_access is False):
            skipped += 1
            continue
        scope = None if u.role == "super_admin" else auth.get_accessible_account_ids(session, u.id)
        snap = command_center_service.save_snapshot(session, u.id, scope)
        session.commit()
        done += 1
        signals += len(snap.signals or [])
    return {"users": done, "signals": signals, "users_skipped": skipped}


JOBS: Dict[str, Dict[str, Any]] = {   # run order within one check: sources first, then the feed
    "sources_refresh": {
        "label": "Refresh signal sources",
        "description": "Scrapes new news, SEC filings and jobs for every account, so the Command Center has new signals to show.",
        "run": run_sources_refresh, "interval": 6, "enabled": True, "options": {"channels": ["sec"], "limit": 20},
    },
    "command_center_refresh": {
        "label": "Refresh Command Center",
        "description": "Rebuilds every user's Priority Signal Feed, playbook and signal velocity from the latest data. Free — no AI.",
        "run": run_command_center_refresh, "interval": 6, "enabled": True, "options": {},
    },
}


# ── Settings and the run log ──────────────────────────────────────────────────

def _ensure_rows(session) -> None:
    for name, j in JOBS.items():
        session.execute(text("""INSERT INTO scheduled_jobs (name, enabled, interval_hours, options)
                                VALUES (:n, :e, :i, CAST(:o AS jsonb)) ON CONFLICT (name) DO NOTHING"""),
                        {"n": name, "e": j["enabled"], "i": j["interval"], "o": json.dumps(j["options"])})
    session.commit()


def job_settings(session) -> Dict[str, Dict[str, Any]]:
    _ensure_rows(session)
    rows = session.execute(text("SELECT name, enabled, interval_hours, options, updated_at FROM scheduled_jobs")).mappings()
    return {r["name"]: {"enabled": r["enabled"], "interval_hours": float(r["interval_hours"]), "options": r["options"] or {},
                        "updated_at": r["updated_at"]} for r in rows if r["name"] in JOBS}


def _last_run(session, name: str, finished_only: bool = True) -> Optional[Dict[str, Any]]:
    row = session.execute(text(f"""SELECT id, trigger, started_at, finished_at, status, summary, error FROM scheduled_job_runs
                                   WHERE job = :j {"AND status IN ('ok', 'failed', 'skipped')" if finished_only else ""}
                                   ORDER BY started_at DESC LIMIT 1"""), {"j": name}).mappings().fetchone()
    return dict(row) if row else None


def is_due(last_started: Optional[datetime], interval_hours: float, now: datetime) -> bool:
    return last_started is None or now - last_started >= timedelta(hours=interval_hours)


def next_run_at(last_started: Optional[datetime], interval_hours: float, enabled: bool, now: datetime) -> Optional[datetime]:
    if not enabled:
        return None
    return now if last_started is None else max(now, last_started + timedelta(hours=interval_hours))


def _running(session, name: str) -> bool:
    return bool(session.execute(text("""SELECT 1 FROM scheduled_job_runs WHERE job = :j AND status = 'running'
                                        AND started_at > now() - make_interval(hours => :h)"""),
                                {"j": name, "h": STALE_RUN_HOURS}).scalar())


def run_job(name: str, trigger: str = "schedule", user_id: Optional[int] = None) -> Dict[str, Any]:
    """Run one job now, logging it. Returns the finished run row."""
    session = get_session()
    try:
        opts = job_settings(session)[name]["options"]
        rid = session.execute(text("""INSERT INTO scheduled_job_runs (job, trigger, triggered_by) VALUES (:j, :t, :u)
                                      RETURNING id"""), {"j": name, "t": trigger, "u": user_id}).scalar()
        session.commit()
        status, summary, error = "ok", {}, None
        try:
            summary = JOBS[name]["run"](session, opts) or {}
            if summary.get("skipped"):
                status = "skipped"
        except Exception as e:      # the run log is how admins see this
            session.rollback()
            status, error = "failed", f"{type(e).__name__}: {e}"[:4000]
        session.execute(text("""UPDATE scheduled_job_runs SET finished_at = now(), status = :s, summary = CAST(:m AS jsonb),
                                error = :e WHERE id = :i"""),
                        {"s": status, "m": json.dumps(summary, default=str), "e": error, "i": rid})
        session.commit()
        print(f"[scheduler] {name} ({trigger}): {status} {summary if status != 'failed' else error}")
    finally:
        session.close()
    # New source data → rebuild the feeds now rather than at the next 6-hour mark
    if name == "sources_refresh" and status == "ok":
        s2 = get_session()
        try:
            go = job_settings(s2)["command_center_refresh"]["enabled"] and not _running(s2, "command_center_refresh")
        finally:
            s2.close()
        if go:
            run_job("command_center_refresh", trigger, user_id)
    return {"id": rid, "job": name, "status": status, "summary": summary, "error": error}


def tick(now: Optional[datetime] = None) -> List[str]:
    """Run every enabled job that is due. Returns the names run (empty if another process holds the lock)."""
    now = now or datetime.now(timezone.utc)
    ran: List[str] = []
    conn = engine.connect()
    try:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            return ran
        try:
            for name in JOBS:
                session = get_session()
                try:
                    cfg = job_settings(session)[name]
                    last = _last_run(session, name)
                    due = cfg["enabled"] and not _running(session, name) and \
                        is_due(last["started_at"] if last else None, cfg["interval_hours"], now)
                finally:
                    session.close()
                if due:
                    run_job(name, "schedule")
                    ran.append(name)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    finally:
        conn.close()
    return ran


def _mark_interrupted() -> None:
    session = get_session()
    try:
        session.execute(text("""UPDATE scheduled_job_runs SET status = 'failed', finished_at = now(),
                                error = 'Interrupted (the server restarted during the run)'
                                WHERE status = 'running' AND started_at < now() - make_interval(hours => :h)"""),
                        {"h": STALE_RUN_HOURS})
        session.commit()
    finally:
        session.close()


_thread: Optional[threading.Thread] = None


def start_background() -> None:
    global _thread

    def loop() -> None:
        time.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                tick()
            except Exception as e:
                print(f"[scheduler] check failed: {e}")
            time.sleep(CHECK_SECONDS)
    _thread = threading.Thread(target=loop, name="scheduler", daemon=True)
    _thread.start()


# ── API (super admins) ────────────────────────────────────────────────────────

router = APIRouter(tags=["10. Scheduled jobs"])
_admin = auth.require_role("super_admin")


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


def _run_view(r: Dict[str, Any]) -> Dict[str, Any]:
    dur = (r["finished_at"] - r["started_at"]).total_seconds() if r.get("finished_at") else None
    return {"id": r["id"], "job": r.get("job"), "trigger": r["trigger"], "status": r["status"], "started_at": _iso(r["started_at"]),
            "finished_at": _iso(r.get("finished_at")), "duration_s": round(dur, 1) if dur is not None else None,
            "summary": r.get("summary") or {}, "error": r.get("error"), "by": r.get("by")}


@router.get("/api/admin/jobs")
def list_jobs(user: User = Depends(_admin)):
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        settings = job_settings(session)
        jobs = []
        for name, j in JOBS.items():
            cfg = settings[name]
            last = _last_run(session, name)
            unavailable = pipeline_available() if name == "sources_refresh" else None
            jobs.append({"name": name, "label": j["label"], "description": j["description"], "enabled": cfg["enabled"],
                         "interval_hours": cfg["interval_hours"], "options": cfg["options"], "unavailable": unavailable,
                         "running": _running(session, name), "last_run": _run_view(last) if last else None,
                         "next_run_at": _iso(next_run_at(last["started_at"] if last else None, cfg["interval_hours"],
                                                         cfg["enabled"] and _enabled_by_env(), now))})
        runs = session.execute(text("""SELECT r.*, coalesce(u.full_name, u.email) AS by FROM scheduled_job_runs r
                                       LEFT JOIN users u ON u.id = r.triggered_by ORDER BY r.started_at DESC LIMIT 20""")).mappings()
        return {"scheduler_running": bool(_thread and _thread.is_alive()), "scheduler_enabled": _enabled_by_env(),
                "check_minutes": CHECK_SECONDS // 60, "channels": [{"key": k, "cost": v} for k, v in CHANNELS.items()],
                "jobs": jobs, "runs": [_run_view(dict(r)) for r in runs]}
    finally:
        session.close()


class JobPatch(BaseModel):
    enabled: Optional[bool] = None
    interval_hours: Optional[float] = Field(None, ge=1, le=168)
    channels: Optional[List[str]] = None


@router.patch("/api/admin/jobs/{name}")
def update_job(name: str, body: JobPatch, user: User = Depends(_admin)):
    if name not in JOBS:
        raise HTTPException(404, "Unknown job.")
    session = get_session()
    try:
        cfg = job_settings(session)[name]
        opts = dict(cfg["options"])
        if body.channels is not None:
            bad = [c for c in body.channels if c not in CHANNELS]
            if bad:
                raise HTTPException(400, "Unknown channel: " + ", ".join(bad))
            if name != "sources_refresh":
                raise HTTPException(400, "Only the source refresh has channels.")
            opts["channels"] = list(dict.fromkeys(body.channels))
        session.execute(text("""UPDATE scheduled_jobs SET enabled = coalesce(:e, enabled), interval_hours = coalesce(:i, interval_hours),
                                options = CAST(:o AS jsonb), updated_at = now(), updated_by = :u WHERE name = :n"""),
                        {"e": body.enabled, "i": body.interval_hours, "o": json.dumps(opts), "u": user.id, "n": name})
        session.commit()
        auth.log_audit(session, user.id, "scheduled_job_updated",
                       details={"job": name, **body.model_dump(exclude_none=True)})
    finally:
        session.close()
    return list_jobs(user)


@router.post("/api/admin/jobs/{name}/run")
def run_now(name: str, user: User = Depends(_admin)):
    """Start the job in the background; the admin tab polls GET /api/admin/jobs for the result."""
    if name not in JOBS:
        raise HTTPException(404, "Unknown job.")
    session = get_session()
    try:
        if _running(session, name):
            raise HTTPException(409, "This job is already running.")
        auth.log_audit(session, user.id, "scheduled_job_run_now", details={"job": name})
    finally:
        session.close()
    threading.Thread(target=run_job, args=(name, "manual", user.id), name=f"job-{name}", daemon=True).start()
    return {"started": True}


@router.get("/api/command-center/refresh-info")
def command_center_refresh_info(user: User = Depends(auth.get_current_user)):
    """For the line under the Command Center header: when the feeds were last rebuilt and when next."""
    session = get_session()
    try:
        cfg = job_settings(session)["command_center_refresh"]
        last = _last_run(session, "command_center_refresh")
        on = cfg["enabled"] and _enabled_by_env()
        return {"enabled": on, "interval_hours": cfg["interval_hours"],
                "last_refresh_at": _iso(last["finished_at"]) if last and last["status"] == "ok" else None,
                "next_refresh_at": _iso(next_run_at(last["started_at"] if last else None, cfg["interval_hours"], on,
                                                    datetime.now(timezone.utc)))}
    finally:
        session.close()


def install(app) -> None:
    app.include_router(router)
    if not _enabled_by_env():
        print("[scheduler] off (SCHEDULER_ENABLED=0)")
        return
    try:
        _mark_interrupted()
    except Exception as e:            # e.g. the 0008 migration hasn't run yet
        print(f"[scheduler] not started: {e}")
        return
    start_background()
