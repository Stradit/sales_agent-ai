"""Catalog Audit Logging and Rate Limiting Service (S-14).

Ensures all cross-tenant catalog interactions are auditable, tamper-evident,
and zero-knowledge compliant. Writes to public.audit_log inside caller transactions.
"""

import hashlib
import logging
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models.audit_log import AuditLog

logger = logging.getLogger(__name__)

# The 11 exact cross-tenant catalog audit actions (Phase A S-14)
CATALOG_ACTIONS = {
    "catalog.publish",
    "catalog.publish_forced",
    "catalog.reuse_check_available",
    "catalog.hydrate",
    "catalog.assign",
    "catalog.request_created",
    "catalog.request_approved",
    "catalog.request_rejected",
    "catalog.rollback",
    "catalog.rollback_blocked",
    "catalog.consume_bypassed_by_admin",
    "catalog.identifier_ambiguous",
    "catalog.companies_merged",
    "catalog.merge_candidate_dismissed",
}

FORBIDDEN_DETAIL_KEYS = {
    "payload",
    "records",
    "data",
    "raw",
    "email",
    "emails",
    "phone",
    "phones",
    "person",
    "personas",
    "founder",
    "founders",
}

SOURCE_CLIENT_KEYS = {
    "source_client_slug",
    "source_client_id",
    "source_slug",
    "source_id",
    "source_client",
}


def sanitize_audit_details(
    details: Optional[Dict[str, Any]],
    is_actor_super_admin: bool = False,
) -> Dict[str, Any]:
    """Sanitizes catalog audit details:
    - Never includes payload content or PII.
    - Strips source client identifiers unless the actor is a platform super-admin.
    """
    if not details:
        return {}
    clean: Dict[str, Any] = {}
    for k, v in details.items():
        if k in FORBIDDEN_DETAIL_KEYS:
            continue
        if not is_actor_super_admin and k in SOURCE_CLIENT_KEYS:
            continue
        clean[k] = v
    return clean


def log_catalog_audit(
    session: Session,
    actor_user_id: Optional[int],
    action: str,
    client_id: Optional[int] = None,
    target_user_id: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
    is_actor_super_admin: bool = False,
) -> AuditLog:
    """Logs a cross-tenant catalog action to public.audit_log inside the caller's transaction.

    Invariants:
    - Runs inside the caller's transaction without issuing a commit of its own.
    - Uses SAVEPOINTs (session.begin_nested) to self-heal stale sequences without poisoning
      the outer transaction.
    - Sanitizes details to preserve zero-knowledge invariants (strips source client for non-super-admins).
    """
    if action not in CATALOG_ACTIONS:
        logger.warning(f"[Audit] Unrecognized catalog audit action '{action}'")

    clean_details = sanitize_audit_details(details, is_actor_super_admin=is_actor_super_admin)

    entry = AuditLog(
        client_id=client_id,
        actor_user_id=actor_user_id,
        action=action,
        target_user_id=target_user_id,
        details=clean_details,
    )

    try:
        with session.begin_nested():
            session.add(entry)
            session.flush()
    except IntegrityError:
        # Sequence out of sync (psycopg2.errors.UniqueViolation on audit_log_pkey)
        session.execute(
            text("SELECT setval('audit_log_id_seq', (SELECT MAX(id) FROM audit_log), true)")
        )
        with session.begin_nested():
            session.add(entry)
            session.flush()

    return entry


class InMemoryRateLimiter:
    """In-process sliding window rate limiter.
    Counts every call (hit or miss) to prevent anti-oracle timing and enumeration attacks.
    TODO: Move to Redis in production for multi-worker / multi-instance deployments.
    """

    def __init__(self, max_calls: int = 30, window_seconds: int = 300):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: Dict[str, list] = defaultdict(list)
        self._lock = Lock()

    def check_and_record(self, key: str) -> bool:
        """Returns True if within limit, False if limit exceeded."""
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            timestamps = [t for t in self._calls[key] if t > cutoff]
            if len(timestamps) >= self.max_calls:
                self._calls[key] = timestamps
                return False
            timestamps.append(now)
            self._calls[key] = timestamps
            return True

    def reset(self):
        """Reset rate limiter state (used for testing)."""
        with self._lock:
            self._calls.clear()


# Global sliding window rate limiter for reuse-check (30 calls per 5 minutes per (user_id, client_slug))
reuse_check_limiter = InMemoryRateLimiter(max_calls=30, window_seconds=300)
