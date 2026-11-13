"""Phase C4 Tests: Provenance Badge on Each Account, Batch Retrieval, Backfill Script, Creation Hook, and Anti-Oracle Guards.

Tests:
1. Badge computation for all 4 states (verified, fetched, edited, update_available).
2. GET /api/accounts list and GET /api/accounts/{id} detail return correct provenance payload.
3. Constant query count on accounts list (O(1) queries for batch provenance).
4. Creation hook automatically inserts account_origin row with origin_type='fetched'.
5. Backfill script works in dry-run and live modes idempotently.
6. Anti-Oracle isolation: no source client or foreign tenant data leaked in provenance.
"""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from api import app
import auth
from db.connection import get_session, SessionLocal
from db.models.account import Account
from db.models.client import Client
from db.models.user_client_access import UserClientAccess
from db.models.user import User
from db.models.platform_catalog import Company, Snapshot, SnapshotBlob
from db.provisioning import create_client_schema, drop_client_schema
from db.tenant import schema_for, validate_slug
from db.repositories.account_repo import AccountRepository
from db.schemas.account_schema import AccountSchema
from services.catalog_presenter import build_provenance_view, batch_get_account_provenances
from scripts.backfill_account_origin import backfill_client


@pytest.fixture(scope="module")
def c4_env():
    """Provisions isolated tenant schema and catalog company with multiple snapshots for C4 testing."""
    slug_c4 = "test_c4_tenant"
    schema_c4 = schema_for(validate_slug(slug_c4))

    # Clean previous run
    drop_client_schema(slug_c4)
    with get_session() as db:
        old_comp = db.query(Company).filter_by(canonical_key="acmepower.com").first()
        if old_comp:
            db.delete(old_comp)
            db.commit()
    create_client_schema(slug_c4)

    with get_session() as db:
        # Client Admin
        user = db.query(User).filter_by(email="admin_c4@test.com").first()
        if not user:
            user = User(
                email="admin_c4@test.com",
                full_name="Admin C4",
                hashed_password=auth.hash_password("testpass123"),
                role="client_admin",
                is_active=True,
            )
            db.add(user)
            db.flush()
        else:
            user.role = "client_admin"
            db.flush()

        client = db.query(Client).filter_by(slug=slug_c4).first()
        client.catalog_consume = True
        client.reuse_requires_approval = True
        db.query(UserClientAccess).filter_by(user_id=user.id, client_id=client.id).delete()
        db.add(UserClientAccess(user_id=user.id, client_id=client.id, role="client_admin"))
        db.flush()

        # Clean up any leftover company from previous runs
        old_comp = db.query(Company).filter_by(canonical_key="acmepower.com").first()
        if old_comp:
            db.delete(old_comp)
            db.flush()

        # Company in catalog
        company = Company(
            canonical_key="acmepower.com",
            display_name="Acme Power Corp",
            legal_name="Acme Power Corporation",
        )
        db.add(company)
        db.flush()

        # Snapshot v1
        snap_v1 = Snapshot(
            company_id=company.id,
            version=1,
            status="ready",
            quality_score=90.0,
            quality_grade="A",
            counts={"accounts": 1, "lobs": 2, "personas": 2},
            taken_at=datetime.now(timezone.utc),
            checksum="chk_c4_v1",
        )
        db.add(snap_v1)
        db.flush()

        # Snapshot v2 (newer)
        snap_v2 = Snapshot(
            company_id=company.id,
            version=2,
            status="ready",
            quality_score=95.0,
            quality_grade="A",
            counts={"accounts": 1, "lobs": 2, "personas": 4},
            taken_at=datetime.now(timezone.utc),
            checksum="chk_c4_v2",
        )
        db.add(snap_v2)
        db.flush()

        def insert_tenant_acct(k, n, d):
            return db.execute(
                text(f"""
                    INSERT INTO "{schema_c4}"."accounts" (key, display_name, legal_name, domain, primary_domain)
                    VALUES (:k, :n, :n, :d, :d)
                    RETURNING id
                """),
                {"k": k, "n": n, "d": d},
            ).scalar()

        # Accounts in tenant schema
        # Account 1: Catalog v1 (has update available -> v2)
        aid1 = insert_tenant_acct("acmepower.com", "Acme Power Corp", "acmepower.com")
        db.execute(
            text(f"""
                INSERT INTO "{schema_c4}"."account_origin"
                    (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, hydrated_at, local_edits)
                VALUES
                    (:aid, 'catalog', 'acmepower.com', :sid, 1, NOW(), false)
            """),
            {"aid": aid1, "sid": snap_v1.id},
        )

        # Account 2: Catalog v2 (up to date -> verified)
        aid2 = insert_tenant_acct("acmepower_v2.com", "Acme Power V2", "acmepower.com")
        db.execute(
            text(f"""
                INSERT INTO "{schema_c4}"."account_origin"
                    (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, hydrated_at, local_edits)
                VALUES
                    (:aid, 'catalog', 'acmepower.com', :sid, 2, NOW(), false)
            """),
            {"aid": aid2, "sid": snap_v2.id},
        )

        # Account 3: Catalog v2 with local edits -> edited
        aid3 = insert_tenant_acct("acmepower_edited.com", "Acme Power Edited", "acmepower.com")
        db.execute(
            text(f"""
                INSERT INTO "{schema_c4}"."account_origin"
                    (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, hydrated_at, local_edits)
                VALUES
                    (:aid, 'catalog', 'acmepower.com', :sid, 2, NOW(), true)
            """),
            {"aid": aid3, "sid": snap_v2.id},
        )

        # Account 4: Normal fetched account
        aid4 = insert_tenant_acct("local_fetched.com", "Local Fetched Corp", "local_fetched.com")
        db.execute(
            text(f"""
                INSERT INTO "{schema_c4}"."account_origin"
                    (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, hydrated_at, local_edits)
                VALUES
                    (:aid, 'fetched', 'local_fetched.com', NULL, NULL, NULL, false)
            """),
            {"aid": aid4},
        )

        # Account 5: Account with NO account_origin row (for backfill testing)
        aid5 = insert_tenant_acct("legacy_orphan.com", "Legacy Orphan Corp", "legacy_orphan.com")

        db.commit()

        yield {
            "slug": slug_c4,
            "schema": schema_c4,
            "user": user,
            "company_id": company.id,
            "snap_v1_id": snap_v1.id,
            "snap_v2_id": snap_v2.id,
            "acct1_id": aid1,
            "acct2_id": aid2,
            "acct3_id": aid3,
            "acct4_id": aid4,
            "acct5_id": aid5,
        }

    drop_client_schema(slug_c4)
    with get_session() as db:
        old_comp = db.query(Company).filter_by(canonical_key="acmepower.com").first()
        if old_comp:
            db.delete(old_comp)
            db.commit()


def test_provenance_badge_states(c4_env):
    """C4: Unit tests for badge computation across all 4 states."""
    # 1. Update available
    p1 = build_provenance_view(
        origin_type="catalog",
        local_edits=False,
        snapshot_version=1,
        hydrated_at=datetime.now(timezone.utc),
        latest_snapshot_version=2,
    )
    assert p1["badge"] == "update_available"
    assert p1["has_newer_version"] is True
    assert p1["latest_catalog_version"] == 2
    assert "Update available" in p1["label"]

    # 2. Verified
    p2 = build_provenance_view(
        origin_type="catalog",
        local_edits=False,
        snapshot_version=2,
        hydrated_at=datetime.now(timezone.utc),
        latest_snapshot_version=2,
    )
    assert p2["badge"] == "verified"
    assert p2["has_newer_version"] is False
    assert "Verified catalog" in p2["label"]

    # 3. Edited locally
    p3 = build_provenance_view(
        origin_type="catalog",
        local_edits=True,
        snapshot_version=2,
        hydrated_at=datetime.now(timezone.utc),
        latest_snapshot_version=2,
    )
    assert p3["badge"] == "edited"
    assert p3["local_edits"] is True
    assert "edited" in p3["label"]

    # 4. Fetched
    p4 = build_provenance_view(
        origin_type="fetched",
        local_edits=False,
        snapshot_version=None,
        hydrated_at=None,
    )
    assert p4["badge"] == "fetched"
    assert p4["origin_type"] == "fetched"
    assert p4["label"] == "Fetched"


def test_accounts_api_provenance_payloads(c4_env):
    """C4: Verifies GET /api/accounts and GET /api/accounts/{id} include rich provenance."""
    client = TestClient(app)
    token = auth.create_access_token(c4_env["user"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c4_env["slug"]}

    # 1. Accounts list endpoint
    res = client.get("/api/accounts", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert "accounts" in data
    accounts_by_id = {a["id"]: a for a in data["accounts"]}

    # Account 1: update available
    a1 = accounts_by_id[c4_env["acct1_id"]]
    assert "provenance" in a1
    assert a1["provenance"]["badge"] == "update_available"
    assert a1["provenance"]["has_newer_version"] is True
    assert a1["provenance"]["latest_catalog_version"] == 2

    # Account 2: verified
    a2 = accounts_by_id[c4_env["acct2_id"]]
    assert a2["provenance"]["badge"] == "verified"
    assert a2["provenance"]["snapshot_version"] == 2

    # Account 3: edited
    a3 = accounts_by_id[c4_env["acct3_id"]]
    assert a3["provenance"]["badge"] == "edited"
    assert a3["provenance"]["local_edits"] is True

    # Account 4: fetched
    a4 = accounts_by_id[c4_env["acct4_id"]]
    assert a4["provenance"]["badge"] == "fetched"

    # Account 5: orphan defaults to fetched
    a5 = accounts_by_id[c4_env["acct5_id"]]
    assert a5["provenance"]["badge"] == "fetched"

    # 2. Account detail endpoint
    res_detail = client.get(f"/api/accounts/{c4_env['acct1_id']}", headers=headers)
    assert res_detail.status_code == 200, res_detail.text
    detail_data = res_detail.json()
    assert "provenance" in detail_data
    assert detail_data["provenance"]["badge"] == "update_available"


def test_batch_provenance_query_count(c4_env):
    """C4: Verifies batch provenance retrieval issues constant O(1) queries."""
    with get_session() as session:
        # Set search path to tenant
        session.execute(text(f'SET LOCAL search_path = "{c4_env["schema"]}", public'))

        query_count = 0
        def count_queries(conn, cursor, statement, parameters, context, executemany):
            nonlocal query_count
            query_count += 1

        from sqlalchemy import event
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", count_queries)

        try:
            account_ids = [c4_env["acct1_id"], c4_env["acct2_id"], c4_env["acct3_id"], c4_env["acct4_id"], c4_env["acct5_id"]]
            prov_map = batch_get_account_provenances(session, account_ids)
            assert len(prov_map) == 5

            # Must be exactly 2 queries: 1 for account_origin, 1 for platform_catalog.snapshots
            assert query_count <= 2
        finally:
            event.remove(engine, "before_cursor_execute", count_queries)


def test_creation_hook(c4_env):
    """C4: Creation-time hook automatically writes account_origin row with origin_type='fetched'."""
    with get_session() as session:
        session.execute(text(f'SET LOCAL search_path = "{c4_env["schema"]}", public'))
        repo = AccountRepository(session)

        schema_input = AccountSchema(
            key="hook_created_corp.com",
            display_name="Hook Created Corp",
            domain="hook_created_corp.com",
        )
        created_acct = repo.upsert(schema_input)
        aid = created_acct.id
        session.commit()

        # Check account_origin in tenant schema
        row = session.execute(
            text(f'SELECT origin_type, canonical_key, local_edits FROM "{c4_env["schema"]}"."account_origin" WHERE account_id = :aid'),
            {"aid": aid},
        ).fetchone()

        assert row is not None
        assert row[0] == "fetched"
        assert row[1] == "hook_created_corp.com"
        assert row[2] is False


def test_backfill_account_origin_script(c4_env):
    """C4: Backfill script finds missing accounts and inserts them idempotently."""
    with get_session() as session:
        # Verify acct5 is currently missing in account_origin
        existing = session.execute(
            text(f'SELECT 1 FROM "{c4_env["schema"]}"."account_origin" WHERE account_id = :aid'),
            {"aid": c4_env["acct5_id"]},
        ).scalar()
        assert existing is None

        # 1. Dry run should report 1 missing without inserting
        dry_res = backfill_client(session, c4_env["slug"], dry_run=True)
        assert dry_res >= 1

        check_dry = session.execute(
            text(f'SELECT 1 FROM "{c4_env["schema"]}"."account_origin" WHERE account_id = :aid'),
            {"aid": c4_env["acct5_id"]},
        ).scalar()
        assert check_dry is None

        # 2. Live run should insert missing row
        live_res = backfill_client(session, c4_env["slug"], dry_run=False)
        assert live_res >= 1

        check_live = session.execute(
            text(f'SELECT origin_type, local_edits FROM "{c4_env["schema"]}"."account_origin" WHERE account_id = :aid'),
            {"aid": c4_env["acct5_id"]},
        ).fetchone()
        assert check_live is not None
        assert check_live[0] == "fetched"

        # 3. Running again should report 0 missing (idempotent)
        second_run = backfill_client(session, c4_env["slug"], dry_run=False)
        assert second_run == 0


def test_anti_oracle_provenance_isolation(c4_env):
    """C4: Anti-Oracle verification ensures no foreign tenant metadata leaks into provenance."""
    client = TestClient(app)
    token = auth.create_access_token(c4_env["user"])
    headers = {"Authorization": f"Bearer {token}", "X-Client-Slug": c4_env["slug"]}

    res = client.get("/api/accounts", headers=headers)
    assert res.status_code == 200
    res_str = res.text

    # Anti-Oracle checks: no source client references
    assert "source_client_id" not in res_str
    assert "source_client_slug" not in res_str
    assert "target_client_id" not in res_str
    assert "client_test_" not in res_str
