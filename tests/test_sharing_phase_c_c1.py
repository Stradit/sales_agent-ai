"""Tests for Phase C1: Catalog Preview Screen and Fetch-Fresh Flow."""

import json
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from api import app
import auth
from db.connection import get_session
from db.models.client import Client
from db.models.platform_catalog import Company, CompanyIdentifier, Snapshot, SnapshotBlob
from db.models.user import User
from db.provisioning import create_client_schema, drop_client_schema
from db.tenant import schema_for


@pytest.fixture(scope="module")
def c1_env():
    """Sets up super_admin, client_admin, target test client and catalog test snapshot."""
    slug_source = "test_c1_src"
    slug_target = "test_c1_tgt"

    create_client_schema(slug_source)
    create_client_schema(slug_target)

    with get_session() as db:
        # Create users
        super_admin = User(
            email="super_c1@test.com",
            hashed_password=auth.hash_password("password123"),
            full_name="Super Admin C1",
            role="super_admin",
            is_active=True,
        )
        client_admin = User(
            email="admin_c1@test.com",
            hashed_password=auth.hash_password("password123"),
            full_name="Client Admin C1",
            role="user",
            is_active=True,
        )
        db.add_all([super_admin, client_admin])
        db.flush()

        # Configure clients provisioned by create_client_schema
        c_source = db.query(Client).filter_by(slug=slug_source).first()
        c_source.status = "active"
        c_source.plan_tier = "enterprise"
        c_source.is_active = True
        c_source.catalog_publish = True
        c_source.catalog_consume = True
        c_source.api_credits_total = 1000
        c_source.api_credits_used = 0

        c_target = db.query(Client).filter_by(slug=slug_target).first()
        c_target.status = "active"
        c_target.plan_tier = "standard"
        c_target.is_active = True
        c_target.catalog_publish = False
        c_target.catalog_consume = True
        c_target.reuse_requires_approval = False
        c_target.api_credits_total = 500
        c_target.api_credits_used = 50
        db.flush()

        from db.models.user_client_access import UserClientAccess
        uca = UserClientAccess(
            user_id=client_admin.id,
            client_id=c_target.id,
            role="client_admin",
        )
        db.add(uca)

        # Create catalog company & ready snapshot
        company = Company(
            canonical_key="acmepreview.com",
            display_name="Acme Preview Corp",
            country="US",
        )
        db.add(company)
        db.flush()

        cid = CompanyIdentifier(
            company_id=company.id,
            id_type="domain",
            id_value="acmepreview.com",
        )
        db.add(cid)

        snap = Snapshot(
            company_id=company.id,
            version=1,
            status="ready",
            quality_score=94.5,
            quality_grade="A",
            checksum="test_checksum_c1",
            counts={"accounts": 1, "lobs": 4, "personas": 10, "posts": 3, "cxo_movements": 1, "jobs": 2},
            taken_at=datetime.now(timezone.utc),
        )
        db.add(snap)
        db.flush()

        blob_data = {
            "account": {
                "name": "Acme Preview Corp",
                "key": "acmepreview.com",
                "domain": "acmepreview.com",
                "website_url": "https://acmepreview.com",
                "industry": "Enterprise Software",
            },
            "lobs": [{"name": "Cloud Platform", "description": "Enterprise cloud services"}],
            "personas": [{"name": "Jane Doe", "title": "Chief Technology Officer"}],
        }
        blob = SnapshotBlob(
            snapshot_id=snap.id,
            payload=blob_data,
        )
        db.add(blob)

        db.commit()

        env_data = {
            "super_id": super_admin.id,
            "admin_id": client_admin.id,
            "target_client_id": c_target.id,
            "target_slug": slug_target,
            "source_slug": slug_source,
            "company_id": company.id,
            "snapshot_id": snap.id,
        }

    yield env_data

    # Teardown
    with get_session() as db:
        db.query(SnapshotBlob).filter_by(snapshot_id=env_data["snapshot_id"]).delete()
        db.query(Snapshot).filter_by(id=env_data["snapshot_id"]).delete()
        db.query(CompanyIdentifier).filter_by(company_id=env_data["company_id"]).delete()
        db.query(Company).filter_by(id=env_data["company_id"]).delete()
        from db.models.user_client_access import UserClientAccess
        db.query(UserClientAccess).filter_by(user_id=env_data["admin_id"]).delete()
        db.query(User).filter(User.id.in_([env_data["super_id"], env_data["admin_id"]])).delete()
        db.commit()

    drop_client_schema(slug_source)
    drop_client_schema(slug_target)


def test_preview_endpoint_match_and_anti_oracle(c1_env):
    """Verify GET /api/catalog/preview returns full PreviewView with zero source leak."""
    client = TestClient(app)
    with get_session() as db:
        admin_user = db.query(User).filter_by(id=c1_env["admin_id"]).first()
        token = auth.create_access_token(admin_user, client_slug=c1_env["target_slug"])
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Slug": c1_env["target_slug"],
    }

    # 1. Non-existent company
    res_none = client.get(
        "/api/catalog/preview?identifier=nonexistent9999.com",
        headers=headers,
    )
    assert res_none.status_code == 200
    data_none = res_none.json()
    assert data_none["match_found"] is False

    # 2. Existing company match
    res = client.get(
        "/api/catalog/preview?identifier=acmepreview.com",
        headers=headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["match_found"] is True
    assert data["available"] is True
    assert "preview_token" in data
    assert data["pricing"]["fee_credits"] >= 0
    assert data["pricing"]["balance_before"] == 450
    assert data["quality"]["score"] == 94.5

    # Anti-oracle assertion: ensure source slug, source name, or internal IDs do not appear
    raw_text = res.text
    assert c1_env["source_slug"] not in raw_text
    assert "Source Client" not in raw_text
    assert "holders" not in raw_text


def test_fetch_fresh_endpoint_and_audit(c1_env):
    """Verify POST /api/catalog/fetch-fresh enqueues pipeline and logs catalog.fetch_fresh_chosen audit."""
    client = TestClient(app)
    with get_session() as db:
        admin_user = db.query(User).filter_by(id=c1_env["admin_id"]).first()
        token = auth.create_access_token(admin_user, client_slug=c1_env["target_slug"])
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Slug": c1_env["target_slug"],
    }

    req_payload = {
        "company_name": "Fresh Harvest Corp",
        "domain": "freshharvest.com",
        "declined_snapshot_id": c1_env["snapshot_id"],
        "reason": "Need live market hiring signals",
    }

    res = client.post(
        "/api/catalog/fetch-fresh",
        headers=headers,
        json=req_payload,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "queued"
    assert body["company_name"] == "Fresh Harvest Corp"
    assert "job_id" in body

    # Verify audit log was recorded
    with get_session() as db:
        from db.models.audit_log import AuditLog
        audit = db.query(AuditLog).filter_by(
            action="catalog.fetch_fresh_chosen",
            client_id=c1_env["target_client_id"],
        ).order_by(AuditLog.id.desc()).first()
        assert audit is not None
        assert audit.details["declined_snapshot_id"] == c1_env["snapshot_id"]
        assert c1_env["source_slug"] not in json.dumps(audit.details)
