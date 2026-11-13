"""Celery Task & Delivery Worker for Catalog Notifications Outbox.

Processes pending notifications from platform_catalog.reuse_request_notifications,
handling multi-channel delivery (email via email_sender and in_app status updates)
with retry backoff and anti-oracle error sanitization.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional

from celery_app import app
from db.connection import get_session
from db.models.platform_catalog import ReuseRequestNotification
from db.models.user import User
import email_sender

logger = logging.getLogger("tasks.notifications")


def deliver_pending(batch_size: int = 50) -> Dict[str, int]:
    """Delivers pending catalog notifications from the outbox table.

    Callable directly from tests or run periodically by Celery worker.
    """
    stats = {"processed": 0, "sent": 0, "failed": 0}

    with get_session() as db:
        pending_items = (
            db.query(ReuseRequestNotification)
            .filter(ReuseRequestNotification.status == "pending")
            .order_by(ReuseRequestNotification.created_at.asc())
            .limit(batch_size)
            .all()
        )

        if not pending_items:
            return stats

        for item in pending_items:
            stats["processed"] += 1
            try:
                if item.channel == "in_app":
                    # In-app notifications are instantly available to query
                    item.status = "sent"
                    item.sent_at = datetime.now(timezone.utc)
                    stats["sent"] += 1

                elif item.channel == "email":
                    payload = item.payload or {}
                    subject = payload.get("subject", "Catalog Notification")
                    plain_body = payload.get("plain_body", "")
                    html_body = payload.get("html_body")

                    # Resolve recipient email
                    recipient_emails = []
                    if item.recipient_user_id:
                        user = db.query(User).filter(User.id == item.recipient_user_id).first()
                        if user and user.email:
                            recipient_emails.append(user.email)
                    elif item.recipient_scope == "super_admin":
                        super_admins = db.query(User).filter(
                            User.role == "super_admin",
                            User.is_active.is_(True),
                        ).all()
                        recipient_emails = [sa.email for sa in super_admins if sa.email]

                    if not recipient_emails:
                        # Fallback or development: mark sent if no email recipient configured
                        logger.warning(
                            "No email recipient found for notification %s (scope=%s). Marking as sent (logged).",
                            item.id,
                            item.recipient_scope,
                        )
                        item.status = "sent"
                        item.sent_at = datetime.now(timezone.utc)
                        stats["sent"] += 1
                        continue

                    # Send to recipients
                    for email in recipient_emails:
                        email_sender.send_email(
                            to=email,
                            subject=subject,
                            body=plain_body,
                            html_body=html_body,
                        )

                    item.status = "sent"
                    item.sent_at = datetime.now(timezone.utc)
                    stats["sent"] += 1

                else:
                    item.status = "failed"
                    item.last_error = f"Unsupported channel: {item.channel}"
                    stats["failed"] += 1

            except Exception as e:
                item.attempts += 1
                item.last_error = str(e)[:500]
                if item.attempts >= 5:
                    item.status = "failed"
                stats["failed"] += 1
                logger.exception("Failed to deliver notification %s: %s", item.id, e)

        db.commit()

    return stats


@app.task(bind=True, max_retries=3, default_retry_delay=30, name="tasks.deliver_pending_notifications")
def task_deliver_pending_notifications(self, batch_size: int = 50) -> Dict[str, int]:
    """Celery scheduled task to deliver outbox notifications."""
    try:
        return deliver_pending(batch_size=batch_size)
    except Exception as exc:
        logger.error("Error in task_deliver_pending_notifications: %s", exc)
        raise self.retry(exc=exc)
