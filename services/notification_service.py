"""Notification Service — Manages outbox queue and templates for catalog reuse notifications.

Guarantees:
- Strict Anti-Oracle isolation: Client-facing payloads never reveal source client, other tenants, or internal IDs.
- Reliable Outbox Pattern: Notifications are inserted in transaction with status 'pending' and delivered via Celery worker.
- Multi-Channel: Enqueues both in_app and email records.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import desc
from sqlalchemy.orm import Session

from db.models.platform_catalog import ReuseRequestNotification
from db.models.user import User
from email_sender import render_html

logger = logging.getLogger("notification_service")

# Prohibited keys in tenant-facing notifications to maintain strict anti-oracle isolation
DISALLOWED_TENANT_KEYS = {
    "source_client_id",
    "source_client_slug",
    "source_client_name",
    "source_account_id",
    "holders",
    "holder_count",
    "tenants",
}


def sanitize_payload(payload: Dict[str, Any], recipient_scope: str) -> Dict[str, Any]:
    """Sanitizes payload, stripping sensitive cross-tenant data for client admins."""
    clean = {}
    for k, v in payload.items():
        if recipient_scope == "client_admin" and k.lower() in DISALLOWED_TENANT_KEYS:
            continue
        clean[k] = v
    return clean


def format_notification_content(
    kind: str,
    company_name: str,
    recipient_scope: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Generates subject, plain body, and HTML body for catalog notifications."""
    meta = metadata or {}
    company = company_name or meta.get("company_name", "Account")
    decision_note = meta.get("decision_note") or meta.get("reason", "")

    if kind == "request_created":
        if recipient_scope == "super_admin":
            subject = f"Action Required: Access Request for {company}"
            lines = [
                f"A client has requested access to verified catalog intelligence for {company}.",
                f"Request ID: {meta.get('event_id', 'N/A')}",
                "Please review the request in the Approval Inbox to approve or decline.",
            ]
            cta_label = "Open Approval Inbox"
            cta_url = "/admin"
        else:
            subject = f"Access Request Submitted: {company}"
            lines = [
                f"Your request for verified catalog intelligence on {company} has been submitted.",
                "Our administration team is reviewing the request. You will be notified once a decision is made.",
            ]
            cta_label = "View Pipeline Explorer"
            cta_url = "/pipeline/"

    elif kind == "approved":
        subject = f"Catalog Request Approved: {company}"
        lines = [
            f"Great news! Your request for verified intelligence on {company} was approved.",
            "The account profile, lines of business, and personas have been hydrated into your pipeline.",
        ]
        if meta.get("fee_credits"):
            lines.append(f"Credits debited: {meta.get('fee_credits')} credits.")
        cta_label = "View Account in Pipeline"
        account_id = meta.get("account_id")
        cta_url = f"/pipeline/?account_id={account_id}" if account_id else "/pipeline/"

    elif kind == "rejected":
        subject = f"Catalog Request Update: {company}"
        lines = [
            f"Your request for catalog intelligence on {company} was declined.",
        ]
        if decision_note:
            lines.append(f"Reason: {decision_note}")
        lines.append("You may still run fresh research using the auto-pipeline if needed.")
        cta_label = "Go to Pipeline Explorer"
        cta_url = "/pipeline/"

    elif kind == "failed":
        subject = f"Catalog Reuse Processing Failed: {company}"
        lines = [
            f"The system encountered an issue processing intelligence for {company}.",
            "No credits were deducted. Our engineering team has been notified.",
        ]
        cta_label = "Return to Pipeline"
        cta_url = "/pipeline/"

    elif kind == "refresh_available":
        subject = f"Updated Intelligence Available: {company}"
        lines = [
            f"Fresh intelligence updates have been detected for {company} in the Platform Catalog.",
            "Review the diff in Pipeline Explorer to apply updates safely without overwriting your manual edits.",
        ]
        cta_label = "Review Updates"
        account_id = meta.get("account_id")
        cta_url = f"/pipeline/?account_id={account_id}&tab=overview" if account_id else "/pipeline/"

    else:
        subject = f"Catalog Notification: {company}"
        lines = [f"Notification update for {company}."]
        cta_label = "View Pipeline"
        cta_url = "/pipeline/"

    plain_body = "\n\n".join(lines)
    html_body = render_html(
        heading=subject,
        lines=lines,
        cta_label=cta_label,
        cta_url=cta_url,
    )

    return {
        "subject": subject,
        "plain_body": plain_body,
        "html_body": html_body,
    }


class NotificationService:
    """Outbox queue manager and query helper for reuse request notifications."""

    @staticmethod
    def enqueue(
        db: Session,
        *,
        event_id: Optional[int] = None,
        recipient_user_id: Optional[int] = None,
        recipient_scope: str = "client_admin",  # 'super_admin' | 'client_admin'
        channels: Optional[List[str]] = None,
        kind: str,  # 'request_created' | 'approved' | 'rejected' | 'failed' | 'refresh_available'
        target_client_id: Optional[int] = None,
        company_name: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> List[ReuseRequestNotification]:
        """Enqueues notifications into platform_catalog.reuse_request_notifications outbox."""
        if channels is None:
            channels = ["in_app", "email"]

        raw_payload = dict(payload or {})
        raw_payload.setdefault("company_name", company_name)
        if event_id:
            raw_payload.setdefault("event_id", event_id)

        clean_payload = sanitize_payload(raw_payload, recipient_scope)

        # Generate message templates
        content = format_notification_content(
            kind=kind,
            company_name=company_name,
            recipient_scope=recipient_scope,
            metadata=clean_payload,
        )
        clean_payload["subject"] = content["subject"]
        clean_payload["plain_body"] = content["plain_body"]
        clean_payload["html_body"] = content["html_body"]

        records = []
        for ch in channels:
            if ch not in ("in_app", "email"):
                continue
            notif = ReuseRequestNotification(
                event_id=event_id,
                recipient_user_id=recipient_user_id,
                recipient_scope=recipient_scope,
                channel=ch,
                kind=kind,
                target_client_id=target_client_id,
                payload=clean_payload,
                status="pending",
                attempts=0,
                created_at=datetime.now(timezone.utc),
            )
            db.add(notif)
            records.append(notif)

        db.flush()
        return records

    @staticmethod
    def list_in_app_notifications(
        db: Session,
        *,
        user_id: int,
        client_id: Optional[int] = None,
        is_super_admin: bool = False,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Lists in-app notifications for the user, strictly enforcing anti-oracle isolation."""
        query = db.query(ReuseRequestNotification).filter(
            ReuseRequestNotification.channel == "in_app"
        )

        if is_super_admin:
            # Super admin sees super_admin notifications or those assigned to them
            query = query.filter(
                (ReuseRequestNotification.recipient_scope == "super_admin") |
                (ReuseRequestNotification.recipient_user_id == user_id)
            )
        else:
            # Client admin / user sees notifications for their client or specifically targeted to them
            filters = [ReuseRequestNotification.recipient_user_id == user_id]
            if client_id is not None:
                filters.append(
                    (ReuseRequestNotification.target_client_id == client_id) &
                    (ReuseRequestNotification.recipient_scope == "client_admin")
                )
            query = query.filter(db.or_(*filters) if hasattr(db, "or_") else (filters[0] if len(filters) == 1 else (filters[0] | filters[1])))

        if unread_only:
            query = query.filter(ReuseRequestNotification.read_at.is_(None))

        query = query.order_by(desc(ReuseRequestNotification.created_at)).offset(offset).limit(limit)
        items = query.all()

        results = []
        for item in items:
            p = item.payload or {}
            # Anti-oracle scrub for non-super-admins
            if not is_super_admin:
                p = sanitize_payload(p, "client_admin")
            results.append({
                "id": item.id,
                "event_id": item.event_id,
                "kind": item.kind,
                "title": p.get("subject", item.kind.replace("_", " ").title()),
                "message": p.get("plain_body", ""),
                "company_name": p.get("company_name", ""),
                "status": item.status,
                "read": item.read_at is not None,
                "read_at": item.read_at.isoformat() if item.read_at else None,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "metadata": {k: v for k, v in p.items() if k not in ("subject", "plain_body", "html_body")},
            })
        return results

    @staticmethod
    def count_unread(
        db: Session,
        *,
        user_id: int,
        client_id: Optional[int] = None,
        is_super_admin: bool = False,
    ) -> int:
        """Returns the unread count of in-app notifications for the user."""
        query = db.query(ReuseRequestNotification).filter(
            ReuseRequestNotification.channel == "in_app",
            ReuseRequestNotification.read_at.is_(None),
        )
        if is_super_admin:
            query = query.filter(
                (ReuseRequestNotification.recipient_scope == "super_admin") |
                (ReuseRequestNotification.recipient_user_id == user_id)
            )
        else:
            if client_id is not None:
                query = query.filter(
                    (ReuseRequestNotification.recipient_user_id == user_id) |
                    ((ReuseRequestNotification.target_client_id == client_id) & (ReuseRequestNotification.recipient_scope == "client_admin"))
                )
            else:
                query = query.filter(ReuseRequestNotification.recipient_user_id == user_id)
        return query.count()

    @staticmethod
    def mark_read(db: Session, notification_id: int, user_id: int) -> bool:
        """Marks a notification as read."""
        notif = db.query(ReuseRequestNotification).filter(
            ReuseRequestNotification.id == notification_id
        ).first()
        if not notif:
            return False
        notif.read_at = datetime.now(timezone.utc)
        notif.status = "read"
        db.flush()
        return True

    @staticmethod
    def mark_all_read(db: Session, user_id: int, client_id: Optional[int] = None, is_super_admin: bool = False) -> int:
        """Marks all in-app notifications for the user as read."""
        query = db.query(ReuseRequestNotification).filter(
            ReuseRequestNotification.channel == "in_app",
            ReuseRequestNotification.read_at.is_(None),
        )
        if is_super_admin:
            query = query.filter(
                (ReuseRequestNotification.recipient_scope == "super_admin") |
                (ReuseRequestNotification.recipient_user_id == user_id)
            )
        else:
            if client_id is not None:
                query = query.filter(
                    (ReuseRequestNotification.recipient_user_id == user_id) |
                    ((ReuseRequestNotification.target_client_id == client_id) & (ReuseRequestNotification.recipient_scope == "client_admin"))
                )
            else:
                query = query.filter(ReuseRequestNotification.recipient_user_id == user_id)
        
        now = datetime.now(timezone.utc)
        count = query.update({"read_at": now, "status": "read"}, synchronize_session=False)
        db.flush()
        return count
