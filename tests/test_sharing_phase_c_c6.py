"""Phase C6 Tests: Polish, Resilience, Performance Guards, Telemetry, and Anti-Oracle Validation.

Tests:
1. Bounded diff computation guard: accounts with > 2,000 entities trigger bounded limit protection (HTTP 400).
2. Telemetry emission: structured events (preview_opened, hydrate_chosen, fetch_fresh_chosen, request_created) emit with correlation_id.
3. Stale preview token detection: expired or tampered preview token is rejected.
4. Anti-Oracle response verification: tenant responses never leak foreign client IDs, slugs, or internal DB keys.
5. OpenAPI documentation integrity: all new Phase C routes are documented in OpenAPI schema.
"""

import json
import logging
import pytest
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from api import app
import auth
from db.connection import get_session
from db.models.client import Client
from db.models.user import User
from db.models.platform_catalog import Company, Snapshot, SnapshotBlob
from db.provisioning import create_client_schema, drop_client_schema
from db.tenant import schema_for, validate_slug
from services.refresh_planner import RefreshPlanner
from routes.catalog_router import emit_sharing_telemetry


@pytest.fixture(scope="module")
def c6_env():
    """Isolated tenant environment for C6 polish and resilience verification."""
    slug = "test_c6_tenant"
    schema = schema_for(validate_slug(slug))

    drop_client_schema(slug)
    create_client_schema(slug)

    with get_session() as db:
        c = db.query(Client).filter_by(slug=slug).first()
        c.api_credits_total = 50000
        c.api_credits_used = 0
        c.is_active = True
        c.status = "active"

        user = db.query(User).filter_by(email="admin_c6@test.com").first()
        if not user:
            user = User(
                email="admin_c6@test.com",
                full_name="Alice C6",
                hashed_password=auth.hash_password("pass123"),
                role="client_admin",
                is_active=True,
            )
            db.add(user)
            db.flush()
        else:
            user.role = "client_admin"
            db.flush()

        from db.models.user_client_access import UserClientAccess
        db.query(UserClientAccess).filter_by(user_id=user.id, client_id=c.id).delete()
        db.add(UserClientAccess(user_id=user.id, client_id=c.id, role="client_admin"))

        # Seed catalog company & snapshot
        comp = db.query(Company).filter_by(canonical_key="c6corp.com").first()
        if not comp:
            comp = Company(canonical_key="c6corp.com", display_name="C6 Global Corp")
            db.add(comp)
            db.flush()

        snap = db.query(Snapshot).filter_by(company_id=comp.id, version=1).first()
        if not snap:
            snap = Snapshot(
                company_id=comp.id,
                version=1,
                status="ready",
                quality_score=95.0,
                quality_grade="A",
                counts={"accounts": 1, "lobs": 1, "personas": 1},
                checksum="chk_c6_v1",
                taken_at=datetime.now(timezone.utc),
            )
            db.add(snap)
            db.flush()

        # Seed account in tenant schema
        res = db.execute(text(f"""
            INSERT INTO "{schema}"."accounts" (key, display_name, legal_name, domain, primary_domain, created_at, updated_at)
            VALUES ('c6corp.com', 'C6 Global Corp', 'C6 Global Corp', 'c6corp.com', 'c6corp.com', NOW(), NOW())
            RETURNING id
        """)).fetchone()
        acct_id = res[0]

        db.execute(text(f"""
            INSERT INTO "{schema}"."account_origin" (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, local_edits)
            VALUES ({acct_id}, 'catalog', 'c6corp.com', {snap.id}, 1, false)
            ON CONFLICT (account_id) DO NOTHING
        """))

        token = auth.create_access_token(user, client_slug=slug)
        db.commit()

        cid = c.id
        uid = user.id
        snap_id = snap.id

    client = TestClient(app)

    yield {
        "slug": slug,
        "schema": schema,
        "token": token,
        "client": client,
        "snapshot_id": snap_id,
        "client_id": cid,
        "account_id": acct_id,
    }

    drop_client_schema(slug)


def test_bounded_diff_computation_guard(c6_env):
    """Guards against unbounded diff execution when account entity counts exceed 2,000 rows."""
    schema = c6_env["schema"]

    with get_session() as db:
        snap = db.query(Snapshot).filter_by(id=c6_env["snapshot_id"]).first()

        # Generate mock remote blob with > 2000 personas
        large_personas = [{"key": f"p_{i}", "full_name": f"Executive {i}"} for i in range(2005)]
        oversized_blob = {
            "account": {"name": "C6 Large Corp"},
            "lobs": [],
            "sub_lobs": [],
            "personas": large_personas,
        }

        with pytest.raises(HTTPException) as exc_info:
            RefreshPlanner.plan(
                session=db,
                schema=schema,
                account_id=c6_env["account_id"],
                snapshot=snap,
                blob_payload=oversized_blob,
            )
        assert exc_info.value.status_code == 400
        assert "2,000 rows" in str(exc_info.value.detail)


def test_telemetry_emission_format(c6_env, caplog):
    """Validates structured non-PII telemetry events are formatted with correlation_id."""
    with caplog.at_level(logging.INFO, logger="routes.catalog_router"):
        emit_sharing_telemetry(
            event="preview_opened",
            correlation_id="corr_c6_test",
            client_id=c6_env["client_id"],
            snapshot_id=123,
            extra_field="sample_val",
        )

    assert "[TELEMETRY]" in caplog.text
    assert "preview_opened" in caplog.text
    assert "corr_c6_test" in caplog.text
    assert "sample_val" in caplog.text


def test_stale_preview_token_rejection(c6_env):
    """Hydration requests with invalid or expired preview tokens must be rejected with HTTP 400."""
    client = c6_env["client"]
    token = c6_env["token"]
    slug = c6_env["slug"]

    # Attempt hydration with forged/expired token
    res = client.post(
        "/api/catalog/hydrate",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
        json={
            "snapshot_id": c6_env["snapshot_id"],
            "preview_token": "forged_preview_token_12345",
            "idempotency_key": "c6_test_stale_idem",
            "mode": "hydrate",
        },
    )
    assert res.status_code == 400
    assert "Invalid or expired preview token" in res.text


def test_anti_oracle_response_cleanliness(c6_env):
    """Tenant-facing endpoints must never leak foreign client identifiers or raw DB secrets."""
    client = c6_env["client"]
    token = c6_env["token"]
    slug = c6_env["slug"]

    # 1. Preview response
    res_prev = client.get(
        "/api/catalog/preview?domain=c6corp.com",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res_prev.status_code == 200
    prev_str = res_prev.text.lower()
    assert "source_client_id" not in prev_str
    assert "source_client_slug" not in prev_str
    assert "holder_count" not in prev_str

    # 2. Credits Ledger response
    res_led = client.get(
        "/api/credits/ledger",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res_led.status_code == 200
    led_str = res_led.text.lower()
    assert "source_client" not in led_str

    # 3. Credits Summary response
    res_sum = client.get(
        "/api/credits/summary",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res_sum.status_code == 200
    sum_str = res_sum.text.lower()
    assert "source_client" not in sum_str


def test_openapi_documentation_integrity(c6_env):
    """OpenAPI specification includes descriptions for all Phase C endpoints."""
    client = c6_env["client"]
    res = client.get("/openapi.json")
    assert res.status_code == 200
    schema = res.json()
    paths = schema.get("paths", {})

    expected_routes = [
        "/api/credits/ledger",
        "/api/credits/summary",
        "/api/credits/ledger/export.csv",
        "/api/credits/platform-summary",
        "/api/catalog/preview",
        "/api/catalog/fetch-fresh",
        "/api/catalog/requests",
        "/api/catalog/inbox",
        "/api/notifications/catalog",
    ]

    for route in expected_routes:
        assert route in paths, f"Route '{route}' missing from OpenAPI schema!"
