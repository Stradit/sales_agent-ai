"""
JobRegistry — Thread-safe in-memory background job tracker.

Used by background pipeline endpoints (/fetch-background) so enrichment
threads can run independently of the HTTP connection lifecycle.

Jobs are retained for JOB_RETENTION_HOURS (default 24h) then purged.
Running/queued jobs are NEVER purged, only finished ones.

All state is in-process memory (same uvicorn process). This is intentional:
- Zero new infrastructure (no Redis, no DB table needed)
- Survives browser close / refresh / tab switch
- Survives as long as the uvicorn server stays up
- For cross-restart persistence, upgrade to Celery + Redis later
"""

import threading
import uuid
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# ── Module-level state ────────────────────────────────────────────────────────
_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}

# How long to retain finished jobs (queued/running are never purged)
JOB_RETENTION_HOURS: int = 24

# Valid status values
STATUS_QUEUED  = "queued"
STATUS_RUNNING = "running"
STATUS_DONE    = "done"
STATUS_FAILED  = "failed"

# Valid job_type values (for filtering in list_jobs)
TYPE_ACCOUNT = "account_enrich"
TYPE_LOB     = "lob_enrich"
TYPE_PERSONA = "persona_enrich"


# ── Public API ────────────────────────────────────────────────────────────────

def create_job(job_type: str, meta: Optional[Dict[str, Any]] = None, client_slug: Optional[str] = None) -> str:
    """
    Register a new background job.

    Args:
        job_type:    One of TYPE_ACCOUNT, TYPE_LOB, TYPE_PERSONA (or any string).
        meta:        Arbitrary dict for display purposes (name, company, account_id etc.).
        client_slug: Optional client slug (defaults to current tenant or stradit_india).

    Returns:
        A unique job_id string like "pjob_a1b2c3d4e5f6".
    """
    job_id = f"pjob_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    resolved_slug = client_slug or (meta.get("client_slug") if meta else None)
    if not resolved_slug:
        try:
            from db.tenant import get_current_slug
            resolved_slug = get_current_slug()
        except Exception:
            resolved_slug = None
    if not resolved_slug:
        resolved_slug = "stradit_india"

    with _lock:
        _jobs[job_id] = {
            "job_id":       job_id,
            "job_type":     job_type,
            "type":         job_type,
            "entity_id":    (meta.get("entity_id") or meta.get("account_id") or meta.get("lob_id") or meta.get("persona_id")) if meta else None,
            "client_slug":  resolved_slug,
            "status":       STATUS_QUEUED,
            "progress_pct": 0,
            "message":      "Queued — waiting to start...",
            "started_at":   now,
            "result":       None,
            "error":        None,
            "created_at":   now,
            "updated_at":   now,
            "meta":         meta or {},
        }
    _purge_old_finished_jobs()

    # Persist to database in public.pipeline_jobs for cross-restart survival
    try:
        from services.job_store import JobStore
        JobStore.create_job(job_type=job_type, meta=meta, client_slug=resolved_slug, job_id=job_id)
    except Exception:
        pass

    return job_id


def update_job(job_id: str, **kwargs: Any) -> None:
    """
    Thread-safe update of any fields on a job in both memory and persistent storage.
    """
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)
            _jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        from services.job_store import JobStore
        JobStore.update_job(job_id, **kwargs)
    except Exception:
        pass


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Return a snapshot copy of the job dict, or None if not found / expired.
    Falls back to public.pipeline_jobs if uvicorn restarted.
    Safe to call from any thread.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job:
            return dict(job)

    # Cross-restart persistence fallback
    try:
        from services.job_store import JobStore
        db_job = JobStore.get_job(job_id)
        if db_job:
            with _lock:
                _jobs[job_id] = db_job
            return dict(db_job)
    except Exception:
        pass

    return None


def list_jobs(job_type: Optional[str] = None, client_slug: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return all jobs sorted newest-first, optionally filtered by job_type and client_slug.
    Queries persistent DB store with in-memory fallback.
    """
    try:
        from services.job_store import JobStore
        db_jobs = JobStore.list_jobs(job_type=job_type, client_slug=client_slug)
        if db_jobs:
            return db_jobs
    except Exception:
        pass

    with _lock:
        jobs = [dict(j) for j in _jobs.values()]
    if job_type:
        jobs = [j for j in jobs if j.get("job_type") == job_type]
    if client_slug:
        jobs = [j for j in jobs if j.get("client_slug") == client_slug]
    return sorted(jobs, key=lambda j: j.get("created_at", ""), reverse=True)


def record_exception(job_id: str, exc: Exception) -> None:
    """
    Convenience helper: mark a job as failed with full traceback as error string.
    Call from within the except block of a background thread.
    """
    update_job(
        job_id,
        status=STATUS_FAILED,
        progress_pct=0,
        message=f"Failed: {exc}",
        error=traceback.format_exc(),
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _purge_old_finished_jobs() -> None:
    """
    Remove DONE/FAILED jobs older than JOB_RETENTION_HOURS.
    Queued/running jobs are never removed.
    Called automatically on every create_job().
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=JOB_RETENTION_HOURS)
    to_remove = []
    with _lock:
        for jid, j in _jobs.items():
            if j.get("status") not in (STATUS_DONE, STATUS_FAILED):
                continue
            try:
                created = datetime.fromisoformat(j["created_at"])
                if created < cutoff:
                    to_remove.append(jid)
            except (ValueError, KeyError):
                to_remove.append(jid)
        for jid in to_remove:
            del _jobs[jid]
