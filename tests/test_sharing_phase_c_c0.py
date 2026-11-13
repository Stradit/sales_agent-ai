"""Tests for Phase C0 Foundations: Catalog Presenter, Notification Outbox, and Anti-Oracle Isolation."""

import json
from datetime import datetime, timezone
import pytest

from db.connection import get_session
from db.models.client import Client
from db.models.user import User
from db.models.platform_catalog import ReuseRequestNotification, Snapshot, Company
from services.catalog_presenter import (
    build_preview_view,
    build_diff_view,
    build_request_view,
    build_provenance_view,
    build_ledger_entry_view,
)
from services.notification_service import NotificationService, sanitize_payload
from tasks.notifications import deliver_pending


def test_anti_oracle_payload_sanitizer():
    """Verify sanitize_payload strictly strips cross-tenant information for client_admin."""
    leaky_payload = {
        "company_name": "Acme Corp",
        "source_client_id": 42,
        "source_client_slug": "secret_tenant_inc",
        "source_client_name": "Secret Tenant Inc",
        "source_account_id": 999,
        "holders": ["tenant_a", "tenant_b"],
        "holder_count": 2,
        "fee_credits": 10,
    }
    
    clean = sanitize_payload(leaky_payload, recipient_scope="client_admin")
    assert "source_client_id" not in clean
    assert "source_client_slug" not in clean
    assert "source_client_name" not in clean
    assert "source_account_id" not in clean
    assert "holders" not in clean
    assert "holder_count" not in clean
    assert clean["company_name"] == "Acme Corp"
    assert clean["fee_credits"] == 10


def test_catalog_presenter_anti_oracle_serialization():
    """Verify presenter outputs never leak source client identity or unapproved metadata."""
    now = datetime.now(timezone.utc)
    mock_snap = type("SnapshotMock", (), {
        "id": 101,
        "version": 1,
        "canonical_key": "acme.com",
        "status": "active",
        "taken_at": now,
        "quality_score": 92.5,
    })()
    
    mock_company = type("CompanyMock", (), {
        "name": "Acme Global",
        "domain": "acme.com",
    })()

    # 1. Preview View
    preview = build_preview_view(
        snapshot=mock_snap,
        company=mock_company,
        quality_score=92.5,
        quality_grade="A",
        counts={"accounts": 1, "lobs": 3, "sub_lobs": 5, "personas": 12, "posts": 4, "cxo_movements": 2, "jobs": 1},
        already_exists=False,
        existing_account_id=None,
        refresh_available=False,
        fee_credits=15,
        estimated_credits_saved=85,
        fetch_fresh_estimate_credits=100,
        client_balance=500,
        requires_approval=True,
        preview_token="signed_token_xyz",
        expires_at=now,
    )

    preview_json = json.dumps(preview)
    assert "secret" not in preview_json
    assert "source_client" not in preview_json
    assert preview["pricing"]["reuse_fee_credits"] == 15
    assert preview["pricing"]["savings_credits"] == 85
    assert preview["rubric"]["total_score"] == 92.5

    # 2. Diff View
    diff = build_diff_view(
        account_id=1,
        company_name="Acme Global",
        snapshot_version_from=1,
        snapshot_version_to=2,
        summary={"personas": {"added": 2, "changed": 1}, "lobs": {"added": 1, "changed": 0}, "account_fields_changed": ["employees"]},
        details={"personas": [{"field": "title", "local": "VP", "remote": "SVP"}]},
        local_edits_present=True,
        protected_rows=[{"table": "personas", "id": 10}],
        fee_credits=5,
        plan_hash="hash123",
    )
    diff_json = json.dumps(diff)
    assert "source_client" not in diff_json
    assert diff["local_edits"]["present"] is True

    # 3. Request View
    mock_event = type("EventMock", (), {
        "id": 55,
        "status": "approved",
        "created_at": now,
        "completed_at": now,
        "error": None,
        "account_id": 10,
    })()
    req = build_request_view(
        event=mock_event,
        company_name="Acme Global",
        canonical_key="acme.com",
        snapshot_version=1,
        sla_hours=24,
    )
    req_json = json.dumps(req)
    assert "source_client" not in req_json
    assert req["status"] == "approved"

    # 4. Provenance View
    prov = build_provenance_view(
        origin_type="catalog",
        local_edits=False,
        snapshot_version=1,
        hydrated_at=now,
        hydrated_by_name="Alice",
        latest_snapshot_version=1,
        snapshot_taken_at=now,
    )
    prov_json = json.dumps(prov)
    assert "source_client" not in prov_json
    assert prov["kind"] == "catalog"

    # 5. Ledger Entry View
    ledger = build_ledger_entry_view(
        entry_id=777,
        created_at=now,
        entry_type="debit",
        amount=-15,
        balance_after=485,
        description="Hydration fee for Acme Global",
        ref_type="share_event",
        ref_id=55,
    )
    ledger_json = json.dumps(ledger)
    assert "source_client" not in ledger_json
    assert ledger["delta"] == -15


def test_notification_outbox_queue_and_delivery():
    """Verify NotificationService enqueues outbox entries and Celery delivery worker delivers them."""
    with get_session() as db:
        # Create test notification
        notifs = NotificationService.enqueue(
            db,
            event_id=None,
            recipient_scope="client_admin",
            channels=["in_app", "email"],
            kind="request_created",
            company_name="Test Enterprise Corp",
            payload={"fee_credits": 15},
        )
        assert len(notifs) == 2
        in_app_id = next(n.id for n in notifs if n.channel == "in_app")
        email_id = next(n.id for n in notifs if n.channel == "email")
        db.commit()

    # Run delivery worker
    stats = deliver_pending(batch_size=10)
    assert stats["processed"] >= 2
    assert stats["sent"] >= 2

    # Verify states updated
    with get_session() as db:
        item1 = db.query(ReuseRequestNotification).filter(ReuseRequestNotification.id == in_app_id).first()
        item2 = db.query(ReuseRequestNotification).filter(ReuseRequestNotification.id == email_id).first()
        assert item1.status == "sent"
        assert item1.sent_at is not None
        assert item2.status == "sent"
        assert item2.sent_at is not None

        # Clean up test rows
        db.delete(item1)
        db.delete(item2)
        db.commit()


def test_in_app_notification_read_workflow():
    """Verify mark_read, unread count, and listing for in-app notifications."""
    with get_session() as db:
        notifs = NotificationService.enqueue(
            db,
            recipient_user_id=1,
            recipient_scope="client_admin",
            channels=["in_app"],
            kind="approved",
            company_name="Workflow Inc",
            payload={"account_id": 42},
        )
        notif_id = notifs[0].id
        db.commit()

    with get_session() as db:
        unread_before = NotificationService.count_unread(db, user_id=1, is_super_admin=True)
        assert unread_before >= 1

        items = NotificationService.list_in_app_notifications(db, user_id=1, is_super_admin=True)
        match = next((item for item in items if item["id"] == notif_id), None)
        assert match is not None
        assert match["read"] is False

        # Mark as read
        ok = NotificationService.mark_read(db, notification_id=notif_id, user_id=1)
        assert ok is True
        db.commit()

        unread_after = NotificationService.count_unread(db, user_id=1, is_super_admin=True)
        assert unread_after == unread_before - 1

        # Clean up
        db.query(ReuseRequestNotification).filter(ReuseRequestNotification.id == notif_id).delete()
        db.commit()
