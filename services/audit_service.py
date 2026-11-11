"""Audit Service — Multi-Tenant Audit Logging and Event Tracking.

Records provable audit trails (§5.7) for client administrative and lifecycle operations:
- Workspace creation, suspension, and repairs
- Credit allocations, top-ups, and adjustments
- User membership grants and role changes
- Job dispatching and execution anomalies
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import text
from sqlalchemy.engine import Engine
from db.connection import engine as default_engine
from db.tenant import get_current_slug, validate_slug


class AuditService:
    """Enterprise audit logger for security, compliance, and billing events."""

    @classmethod
    def log_event(
        cls,
        action: str,
        client_slug: Optional[str] = None,
        actor_user_id: Optional[int] = None,
        target_user_id: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
        engine: Optional[Engine] = None,
    ) -> int:
        """Records an audit event in public.audit_log."""
        eng = engine or default_engine
        slug = client_slug or get_current_slug()
        client_id = None

        if slug:
            clean_slug = validate_slug(slug)
            with eng.connect() as conn:
                client_id = conn.execute(
                    text("SELECT id FROM public.clients WHERE slug = :s"),
                    {"s": clean_slug}
                ).scalar()

        details_json = json.dumps(details or {})

        with eng.begin() as conn:
            row = conn.execute(text("""
                INSERT INTO public.audit_log (client_id, actor_user_id, target_user_id, action, details, created_at)
                VALUES (:cid, :actor_id, :target_id, :action, :details, NOW())
                RETURNING id
            """), {
                "cid": client_id,
                "actor_id": actor_user_id,
                "target_id": target_user_id,
                "action": action,
                "details": details_json,
            }).fetchone()

            return row[0] if row else 0

    @classmethod
    def list_events(
        cls,
        client_slug: Optional[str] = None,
        limit: int = 50,
        engine: Optional[Engine] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieves audit trail events, optionally filtered by client tenant."""
        eng = engine or default_engine
        query = """
            SELECT a.id, a.client_id, c.slug AS client_slug, a.actor_user_id,
                   a.target_user_id, a.action, a.details, a.created_at
            FROM public.audit_log a
            LEFT JOIN public.clients c ON c.id = a.client_id
            WHERE 1=1
        """
        params: Dict[str, Any] = {"lim": limit}

        if client_slug:
            clean_slug = validate_slug(client_slug)
            query += " AND c.slug = :s"
            params["s"] = clean_slug

        query += " ORDER BY a.created_at DESC LIMIT :lim"

        with eng.connect() as conn:
            rows = conn.execute(text(query), params).mappings().fetchall()
            results = []
            for r in rows:
                ev = dict(r)
                if ev.get("created_at") is not None and hasattr(ev["created_at"], "isoformat"):
                    ev["created_at"] = ev["created_at"].isoformat()
                if isinstance(ev.get("details"), str):
                    try:
                        ev["details"] = json.loads(ev["details"])
                    except Exception:
                        pass
                results.append(ev)
            return results
