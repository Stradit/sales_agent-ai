"""Tests for Phase C3: Request Access, Status Tracking, Notifications, and Approval Inbox."""

import json
from datetime import datetime, timezone
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from api import app
import auth
from db.connection import get_session
from db.models.client import Client
from db.models.platform_catalog import (
    Company,
    CompanyIdentifier,
    ReuseRequestNotification,
    ShareEvent,
    Snapshot,
    SnapshotBlob,
)
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from db.tenant import schema_for
from services.account_reuse_service import AccountReuseService
from tasks.notifications import deliver_pending


@pytest.fixture(scope="function")
def c3_env():
    """Sets up super_admin, client_admin, viewer, target client and catalog snapshots."""
    slug_source = "test_c3_src"
    slug_target = "test_c3_tgt"
    slug_other = "test_c3_oth"

    for s in (slug_source, slug_target, slug_other):
        try:
            drop_client_schema(s)
        except Exception:
            pass

    with get_session() as db:
        db.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key = 'solarsystems.com'"))
        db.commit()

    create_client_schema(slug_source)
    create_client_schema(slug_target)
    create_client_schema(slug_other)

    with get_session() as db:
        # 1. Users
        super_admin = db.query(User).filter_by(email="super_c3@test.com").first()
        if not super_admin:
            super_admin = User(
                email="super_c3@test.com",
                hashed_password=auth.hash_password("password123"),
                full_name="Super Admin C3",
                role="super_admin",
                is_active=True,
            )
            db.add(super_admin)

        client_admin = db.query(User).filter_by(email="admin_c3@test.com").first()
        if not client_admin:
            client_admin = User(
                email="admin_c3@test.com",
                hashed_password=auth.hash_password("password123"),
                full_name="Target Client Admin C3",
                role="user",
                is_active=True,
            )
            db.add(client_admin)

        viewer_user = db.query(User).filter_by(email="viewer_c3@test.com").first()
        if not viewer_user:
            viewer_user = User(
                email="viewer_c3@test.com",
                hashed_password=auth.hash_password("password123"),
                full_name="Target Viewer C3",
                role="user",
                is_active=True,
            )
            db.add(viewer_user)

        other_admin = db.query(User).filter_by(email="other_c3@test.com").first()
        if not other_admin:
            other_admin = User(
                email="other_c3@test.com",
                hashed_password=auth.hash_password("password123"),
                full_name="Other Client Admin C3",
                role="user",
                is_active=True,
            )
            db.add(other_admin)

        db.flush()

        # Clients
        target_client = db.query(Client).filter_by(slug=slug_target).first()
        target_client.catalog_consume = True
        target_client.reuse_requires_approval = True
        target_client.api_credits_total = 500
        target_client.api_credits_used = 0
        target_client.max_accounts = 50

        other_client = db.query(Client).filter_by(slug=slug_other).first()
        other_client.catalog_consume = True
        other_client.reuse_requires_approval = True
        other_client.api_credits_total = 500
        other_client.api_credits_used = 0
        other_client.max_accounts = 50

        # Grants
        db.query(UserClientAccess).filter(UserClientAccess.user_id.in_([client_admin.id, viewer_user.id, other_admin.id])).delete()
        db.add(UserClientAccess(user_id=client_admin.id, client_id=target_client.id, role="client_admin"))
        db.add(UserClientAccess(user_id=viewer_user.id, client_id=target_client.id, role="viewer"))
        db.add(UserClientAccess(user_id=other_admin.id, client_id=other_client.id, role="client_admin"))
        db.flush()

        # 2. Company & Identifiers
        company = Company(
            canonical_key="solarsystems.com",
            display_name="Solar Systems Energy",
            legal_name="Solar Systems Energy Inc",
        )
        db.add(company)
        db.flush()

        db.add(CompanyIdentifier(company_id=company.id, id_type="domain", id_value="solarsystems.com"))
        db.add(CompanyIdentifier(company_id=company.id, id_type="sec_cik", id_value="0001889900"))
        db.flush()

        # 3. Snapshot V1
        snap_v1 = Snapshot(
            company_id=company.id,
            version=1,
            status="ready",
            quality_score=92.5,
            quality_grade="A",
            counts={"accounts": 1, "lobs": 2, "personas": 3, "posts": 2, "jobs": 1},
            taken_at=datetime.now(timezone.utc),
            checksum="dummy_checksum_v1",
        )
        db.add(snap_v1)
        db.flush()

        blob_v1_data = {
            "account": {
                "key": "solarsystems.com",
                "display_name": "Solar Systems Energy",
                "legal_name": "Solar Systems Energy Inc",
                "domain": "solarsystems.com",
                "industry": "Renewables & Environment",
            },
            "lobs": [
                {"key": "lob_res", "lob_name": "Residential Solar", "overview": "Rooftop installations"},
                {"key": "lob_com", "lob_name": "Commercial Solar", "overview": "Grid scale installations"},
            ],
            "personas": [
                {"key": "per_sam", "name": "Sam Taylor", "display_name": "Sam Taylor", "title": "VP Commercial Solar"},
                {"key": "per_jordan", "name": "Jordan Lee", "display_name": "Jordan Lee", "title": "Head of Operations"},
            ],
        }
        db.add(SnapshotBlob(snapshot_id=snap_v1.id, payload=blob_v1_data))

        # 4. Snapshot V2 (superseded candidate)
        snap_v2 = Snapshot(
            company_id=company.id,
            version=2,
            status="ready",
            quality_score=96.0,
            quality_grade="A",
            counts={"accounts": 1, "lobs": 3, "personas": 4, "posts": 4, "jobs": 2},
            taken_at=datetime.now(timezone.utc),
            checksum="dummy_checksum_v2",
        )
        db.add(snap_v2)
        db.flush()

        blob_v2_data = dict(blob_v1_data)
        blob_v2_data["personas"].append({"key": "per_alex", "name": "Alex Morgan", "display_name": "Alex Morgan", "title": "Chief Technology Officer"})
        db.add(SnapshotBlob(snapshot_id=snap_v2.id, payload=blob_v2_data))

        db.commit()

        yield {
            "slug_source": slug_source,
            "slug_target": slug_target,
            "slug_other": slug_other,
            "target_schema": schema_for(slug_target),
            "super_user": super_admin,
            "client_admin": client_admin,
            "viewer_user": viewer_user,
            "other_admin": other_admin,
            "snap_v1_id": snap_v1.id,
            "snap_v2_id": snap_v2.id,
            "company_id": company.id,
            "company_name": company.display_name,
        }

    for s in (slug_source, slug_target, slug_other):
        try:
            drop_client_schema(s)
        except Exception:
            pass


def test_full_approval_lifecycle_and_notifications(c3_env):
    """C3: Full request -> pending -> approve -> hydrate -> notifications delivered lifecycle."""
    client = TestClient(app)

    # 1. Requester generates preview token and submits request
    token = auth.create_access_token(c3_env["client_admin"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c3_env["slug_target"]}

    prev_token = AccountReuseService.generate_preview_token(
        snapshot_id=c3_env["snap_v1_id"],
        target_client_slug=c3_env["slug_target"],
        user_id=c3_env["client_admin"].id,
    )

    req_payload = {
        "snapshot_id": c3_env["snap_v1_id"],
        "preview_token": prev_token,
        "mode": "instant",
        "reason": "Q4 Commercial expansion target",
    }
    submit_res = client.post("/api/catalog/requests", json=req_payload, headers=headers)
    assert submit_res.status_code == 200, submit_res.text
    req_view = submit_res.json()

    assert req_view["status"] == "pending_approval"
    assert req_view["already_pending"] is False
    assert len(req_view["timeline"]) >= 2
    assert "24 hours" in req_view["sla_hint"]
    request_id = req_view["request_id"]

    # 2. Verify single request view endpoint GET /api/catalog/requests/{id}
    get_res = client.get(f"/api/catalog/requests/{request_id}", headers=headers)
    assert get_res.status_code == 200
    assert get_res.json()["request_id"] == request_id
    assert "canonical_key" not in get_res.json()  # Anti-Oracle check for tenant

    # 3. Super Admin visits Inbox: GET /api/catalog/inbox
    super_token = auth.create_access_token(c3_env["super_user"])
    super_headers = {"Authorization": f"Bearer {super_token}"}

    inbox_res = client.get("/api/catalog/inbox?status=pending_approval", headers=super_headers)
    assert inbox_res.status_code == 200, inbox_res.text
    inbox_data = inbox_res.json()

    item = next((i for i in inbox_data["items"] if i["id"] == request_id), None)
    assert item is not None
    assert item["company"]["display_name"] == "Solar Systems Energy"
    assert item["client"]["slug"] == c3_env["slug_target"]
    assert item["quality_report"]["grade"] == "A"
    assert len(item["quality_report"]["breakdown"]) == 4
    assert item["quality_report"]["identifier_coverage"]["domain"] is True
    assert item["quality_report"]["identifier_coverage"]["cik"] is True
    assert item["client_credit_status"]["balance"] == 500
    assert item["client_credit_status"]["allowed"] is True

    # 4. Super Admin decides to approve: POST /api/catalog/inbox/{id}/decision
    decision_payload = {
        "decision": "approve",
        "note": "Approved for commercial expansion",
        "use_latest_version": True,  # Uses latest snapshot
    }
    decide_res = client.post(f"/api/catalog/inbox/{request_id}/decision", json=decision_payload, headers=super_headers)
    assert decide_res.status_code == 200, decide_res.text
    decide_data = decide_res.json()
    assert decide_data["ok"] is True
    assert decide_data["status"] == "approved"
    assert decide_data["account_id"] is not None

    # 5. Verify database: Target schema contains hydrated account and personas
    with get_session() as db:
        tgt_schema = c3_env["target_schema"]
        acct = db.execute(
            text(f'SELECT id, display_name FROM "{tgt_schema}"."accounts" WHERE id = :id'),
            {"id": decide_data["account_id"]},
        ).fetchone()
        assert acct is not None
        assert acct[1] == "Solar Systems Energy"

        personas_cnt = db.execute(
            text(f'SELECT COUNT(*) FROM "{tgt_schema}"."personas" WHERE account_id = :id'),
            {"id": decide_data["account_id"]},
        ).scalar()
        assert personas_cnt == 3

    # 6. Verify in-app notifications API for requester
    notif_res = client.get("/api/notifications/catalog", headers=headers)
    assert notif_res.status_code == 200
    notifs = notif_res.json()["notifications"]
    assert len(notifs) >= 1
    # Check anti-oracle: no source client info
    serialized_notifs = json.dumps(notifs)
    assert "test_c3_src" not in serialized_notifs

    unread_res = client.get("/api/notifications/catalog/unread-count", headers=headers)
    assert unread_res.status_code == 200
    assert unread_res.json()["unread_count"] >= 1

    first_notif_id = notifs[0]["id"]
    read_res = client.post(f"/api/notifications/catalog/{first_notif_id}/read", headers=headers)
    assert read_res.status_code == 200

    # 7. Celery Outbox Delivery Worker
    with patch("tasks.notifications.email_sender.send_email") as mock_email:
        mock_email.return_value = {"status": "sent"}
        stats = deliver_pending(batch_size=50)
        assert stats["sent"] >= 1
        # Assert each email recipient receives message without leaking cross-tenant data
        for call_args in mock_email.call_args_list:
            body = str(call_args)
            assert "test_c3_src" not in body


def test_reject_path(c3_env):
    """C3: Super Admin rejects a pending request; records note and enqueues notification."""
    client = TestClient(app)

    token = auth.create_access_token(c3_env["client_admin"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c3_env["slug_target"]}

    prev_token = AccountReuseService.generate_preview_token(
        snapshot_id=c3_env["snap_v1_id"],
        target_client_slug=c3_env["slug_target"],
        user_id=c3_env["client_admin"].id,
    )

    req_res = client.post(
        "/api/catalog/requests",
        json={"snapshot_id": c3_env["snap_v1_id"], "preview_token": prev_token, "mode": "instant"},
        headers=headers,
    )
    request_id = req_res.json()["request_id"]

    super_token = auth.create_access_token(c3_env["super_user"])
    super_headers = {"Authorization": f"Bearer {super_token}"}

    rej_res = client.post(
        f"/api/catalog/inbox/{request_id}/decision",
        json={"decision": "reject", "note": "Budget exceeded for Q4"},
        headers=super_headers,
    )
    assert rej_res.status_code == 200
    rej_data = rej_res.json()
    assert rej_data["ok"] is True
    assert rej_data["status"] == "rejected"
    assert rej_data["decision_note"] == "Budget exceeded for Q4"

    # Verify ShareEvent status in DB
    with get_session() as db:
        ev = db.query(ShareEvent).filter_by(id=request_id).first()
        assert ev.status == "rejected"
        assert ev.decision_note == "Budget exceeded for Q4"

        # Verify no account created in target schema
        cnt = db.execute(text(f'SELECT COUNT(*) FROM "{c3_env["target_schema"]}"."accounts"')).scalar()
        assert cnt == 0


def test_cancel_path_and_permissions(c3_env):
    """C3: Requester/client_admin can cancel a pending request; other roles/tenants cannot."""
    client = TestClient(app)

    token = auth.create_access_token(c3_env["client_admin"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c3_env["slug_target"]}

    prev_token = AccountReuseService.generate_preview_token(
        snapshot_id=c3_env["snap_v1_id"],
        target_client_slug=c3_env["slug_target"],
        user_id=c3_env["client_admin"].id,
    )

    req_res = client.post(
        "/api/catalog/requests",
        json={"snapshot_id": c3_env["snap_v1_id"], "preview_token": prev_token, "mode": "instant"},
        headers=headers,
    )
    request_id = req_res.json()["request_id"]

    # 1. Other client trying to cancel -> 404 Not Found (tenant isolation)
    other_token = auth.create_access_token(c3_env["other_admin"])
    other_headers = {"Authorization": f"Bearer {other_token}", "X-Client-Slug": c3_env["slug_other"]}
    res_other = client.post(f"/api/catalog/requests/{request_id}/cancel", headers=other_headers)
    assert res_other.status_code == 404

    # 2. Requester / Client Admin cancels request -> 200 OK
    res_cancel = client.post(f"/api/catalog/requests/{request_id}/cancel", headers=headers)
    assert res_cancel.status_code == 200
    assert res_cancel.json()["status"] == "cancelled"

    # 3. Cancelling already cancelled request -> 400 Bad Request
    res_cancel_again = client.post(f"/api/catalog/requests/{request_id}/cancel", headers=headers)
    assert res_cancel_again.status_code == 400


def test_duplicate_pending_request(c3_env):
    """C3: Duplicate request for same snapshot returns existing one with already_pending=True."""
    client = TestClient(app)

    token = auth.create_access_token(c3_env["client_admin"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c3_env["slug_target"]}

    prev_token = AccountReuseService.generate_preview_token(
        snapshot_id=c3_env["snap_v1_id"],
        target_client_slug=c3_env["slug_target"],
        user_id=c3_env["client_admin"].id,
    )

    res1 = client.post(
        "/api/catalog/requests",
        json={"snapshot_id": c3_env["snap_v1_id"], "preview_token": prev_token, "mode": "instant"},
        headers=headers,
    )
    assert res1.status_code == 200
    assert res1.json()["already_pending"] is False
    req_id = res1.json()["request_id"]

    res2 = client.post(
        "/api/catalog/requests",
        json={"snapshot_id": c3_env["snap_v1_id"], "preview_token": prev_token, "mode": "instant"},
        headers=headers,
    )
    assert res2.status_code == 200
    assert res2.json()["already_pending"] is True
    assert res2.json()["request_id"] == req_id


def test_permission_matrix(c3_env):
    """C3: Viewer/analyst cannot execute or call inbox; super admin can."""
    client = TestClient(app)

    viewer_token = auth.create_access_token(c3_env["viewer_user"])
    viewer_headers = {"Authorization": f"Bearer {viewer_token}", "X-Client-Slug": c3_env["slug_target"]}

    # Non-super admin cannot access Inbox
    inbox_res = client.get("/api/catalog/inbox", headers=viewer_headers)
    assert inbox_res.status_code == 403

    # Non-super admin cannot call decision
    dec_res = client.post("/api/catalog/inbox/999/decision", json={"decision": "approve"}, headers=viewer_headers)
    assert dec_res.status_code == 403


def test_snapshot_superseded_handling(c3_env):
    """C3: 409 snapshot_superseded returned when newer version exists and use_latest_version is False."""
    client = TestClient(app)

    token = auth.create_access_token(c3_env["client_admin"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c3_env["slug_target"]}

    # Request Snapshot V1
    prev_token = AccountReuseService.generate_preview_token(
        snapshot_id=c3_env["snap_v1_id"],
        target_client_slug=c3_env["slug_target"],
        user_id=c3_env["client_admin"].id,
    )
    req_res = client.post(
        "/api/catalog/requests",
        json={"snapshot_id": c3_env["snap_v1_id"], "preview_token": prev_token, "mode": "instant"},
        headers=headers,
    )
    request_id = req_res.json()["request_id"]

    super_token = auth.create_access_token(c3_env["super_user"])
    super_headers = {"Authorization": f"Bearer {super_token}"}

    # Attempt approve without use_latest_version -> 409 snapshot_superseded (v2 is ready!)
    res_conflict = client.post(
        f"/api/catalog/inbox/{request_id}/decision",
        json={"decision": "approve", "use_latest_version": False},
        headers=super_headers,
    )
    assert res_conflict.status_code == 409
    err = res_conflict.json()["detail"]
    assert err["error"] == "snapshot_superseded"
    assert err["new_version"] == 2

    # Attempt approve WITH use_latest_version -> 200 OK
    res_ok = client.post(
        f"/api/catalog/inbox/{request_id}/decision",
        json={"decision": "approve", "use_latest_version": True},
        headers=super_headers,
    )
    assert res_ok.status_code == 200, res_ok.text
    assert res_ok.json()["ok"] is True


def test_bulk_decision_with_partial_failure(c3_env):
    """C3: Bulk decisions process each item independently and return itemized results."""
    client = TestClient(app)

    token = auth.create_access_token(c3_env["client_admin"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c3_env["slug_target"]}

    prev_token = AccountReuseService.generate_preview_token(
        snapshot_id=c3_env["snap_v2_id"],
        target_client_slug=c3_env["slug_target"],
        user_id=c3_env["client_admin"].id,
    )
    req_res = client.post(
        "/api/catalog/requests",
        json={"snapshot_id": c3_env["snap_v2_id"], "preview_token": prev_token, "mode": "instant"},
        headers=headers,
    )
    request_id = req_res.json()["request_id"]

    super_token = auth.create_access_token(c3_env["super_user"])
    super_headers = {"Authorization": f"Bearer {super_token}"}

    bulk_payload = {
        "decisions": [
            {"id": request_id, "decision": "approve", "note": "Bulk approved"},
            {"id": 999999, "decision": "approve", "note": "Non-existent request"},
        ],
        "use_latest_version": True,
    }
    bulk_res = client.post("/api/catalog/inbox/bulk-decision", json=bulk_payload, headers=super_headers)
    assert bulk_res.status_code == 200
    data = bulk_res.json()
    assert data["total"] == 2
    assert data["succeeded"] == 1
    assert data["failed"] == 1
    assert data["results"][0]["ok"] is True
    assert data["results"][1]["ok"] is False


def test_tenant_isolation_on_requests(c3_env):
    """C3: Client A never sees Client B's requests; single request by wrong client returns 404."""
    client = TestClient(app)

    token = auth.create_access_token(c3_env["client_admin"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c3_env["slug_target"]}

    prev_token = AccountReuseService.generate_preview_token(
        snapshot_id=c3_env["snap_v1_id"],
        target_client_slug=c3_env["slug_target"],
        user_id=c3_env["client_admin"].id,
    )
    req_res = client.post(
        "/api/catalog/requests",
        json={"snapshot_id": c3_env["snap_v1_id"], "preview_token": prev_token, "mode": "instant"},
        headers=headers,
    )
    req_id = req_res.json()["request_id"]

    # Other client requests list
    other_token = auth.create_access_token(c3_env["other_admin"])
    other_headers = {"Authorization": f"Bearer {other_token}", "X-Client-Slug": c3_env["slug_other"]}

    list_res = client.get("/api/catalog/requests", headers=other_headers)
    assert list_res.status_code == 200
    req_ids = [r["request_id"] for r in list_res.json()["requests"]]
    assert req_id not in req_ids

    # Other client tries to fetch single request
    single_res = client.get(f"/api/catalog/requests/{req_id}", headers=other_headers)
    assert single_res.status_code == 404
