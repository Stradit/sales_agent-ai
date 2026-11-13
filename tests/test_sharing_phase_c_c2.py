"""Tests for Phase C2: Refresh Diff & Local-Edit Protection."""

import json
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from api import app
import auth
from db.connection import get_session
from db.models.client import Client
from db.models.platform_catalog import Company, CompanyIdentifier, Snapshot, SnapshotBlob
from db.models.user import User
from db.provisioning import create_client_schema, drop_client_schema
from db.tenant import schema_for


@pytest.fixture(scope="function")
def c2_env():
    """Sets up super_admin, client_admin, viewer, target client and catalog v1 and v2 snapshots."""
    slug_source = "test_c2_src"
    slug_target = "test_c2_tgt"

    for s in (slug_source, slug_target):
        try:
            drop_client_schema(s)
        except Exception:
            pass

    with get_session() as db:
        db.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key = 'acmeindustrial.com'"))
        db.commit()

    create_client_schema(slug_source)
    create_client_schema(slug_target)

    with get_session() as db:
        super_admin = db.query(User).filter_by(email="super_c2@test.com").first()
        if not super_admin:
            super_admin = User(
                email="super_c2@test.com",
                hashed_password=auth.hash_password("password123"),
                full_name="Super Admin C2",
                role="super_admin",
                is_active=True,
            )
            db.add(super_admin)

        client_admin = db.query(User).filter_by(email="admin_c2@test.com").first()
        if not client_admin:
            client_admin = User(
                email="admin_c2@test.com",
                hashed_password=auth.hash_password("password123"),
                full_name="Client Admin C2",
                role="user",
                is_active=True,
            )
            db.add(client_admin)

        viewer_user = db.query(User).filter_by(email="viewer_c2@test.com").first()
        if not viewer_user:
            viewer_user = User(
                email="viewer_c2@test.com",
                hashed_password=auth.hash_password("password123"),
                full_name="Viewer User C2",
                role="user",
                is_active=True,
            )
            db.add(viewer_user)
        db.flush()

        c_target = db.query(Client).filter_by(slug=slug_target).first()
        c_target.status = "active"
        c_target.is_active = True
        c_target.catalog_publish = False
        c_target.catalog_consume = True
        c_target.reuse_requires_approval = False
        c_target.api_credits_total = 500
        c_target.api_credits_used = 10
        db.flush()

        from db.models.user_client_access import UserClientAccess
        db.query(UserClientAccess).filter_by(client_id=c_target.id).delete()
        uca_admin = UserClientAccess(
            user_id=client_admin.id,
            client_id=c_target.id,
            role="client_admin",
        )
        uca_viewer = UserClientAccess(
            user_id=viewer_user.id,
            client_id=c_target.id,
            role="viewer",
        )
        db.add_all([uca_admin, uca_viewer])

        # Catalog Company
        company = db.query(Company).filter_by(canonical_key="acmeindustrial.com").first()
        if not company:
            company = Company(
                canonical_key="acmeindustrial.com",
                display_name="Acme Industrial Systems",
                country="US",
            )
            db.add(company)
            db.flush()

            cid = CompanyIdentifier(
                company_id=company.id,
                id_type="domain",
                id_value="acmeindustrial.com",
            )
            db.add(cid)
            db.flush()

        # Clean existing snapshots for this company
        existing_snaps = db.query(Snapshot).filter_by(company_id=company.id).all()
        for s in existing_snaps:
            db.query(SnapshotBlob).filter_by(snapshot_id=s.id).delete()
            db.delete(s)
        db.flush()

        # Snapshot V1
        snap_v1 = Snapshot(
            company_id=company.id,
            version=1,
            status="ready",
            quality_score=90.0,
            quality_grade="A",
            checksum="chk_c2_v1",
            counts={"accounts": 1, "lobs": 1, "personas": 2},
            taken_at=datetime.now(timezone.utc),
        )
        db.add(snap_v1)
        db.flush()

        # Snapshot V2 (Newer verified intelligence)
        snap_v2 = Snapshot(
            company_id=company.id,
            version=2,
            status="ready",
            quality_score=96.0,
            quality_grade="A",
            checksum="chk_c2_v2",
            counts={"accounts": 1, "lobs": 2, "personas": 3},
            taken_at=datetime.now(timezone.utc),
        )
        db.add(snap_v2)
        db.flush()

        blob_v2_data = {
            "account": {
                "display_name": "Acme Industrial Systems Worldwide",
                "employee_count": "5,000+",
                "revenue_range": "$500M - $1B",
                "domain": "acmeindustrial.com",
            },
            "lobs": [
                {
                    "key": "lob_mfg",
                    "lob_name": "Manufacturing Solutions",
                    "overview": "Advanced robotics manufacturing systems (v2 catalog updated)",
                },
                {
                    "key": "lob_supply_chain",
                    "lob_name": "Supply Chain Automation",
                    "overview": "Automated logistics solutions",
                },
            ],
            "personas": [
                {
                    "key": "per_alice",
                    "name": "Alice Smith",
                    "display_name": "Alice Smith",
                    "title": "SVP Manufacturing",
                    "email": "alice@acmeindustrial.com",
                },
                {
                    "key": "per_bob",
                    "name": "Bob Jones",
                    "display_name": "Bob Jones",
                    "title": "Director of Operations",
                    "email": "bob@acmeindustrial.com",
                },
                {
                    "key": "per_charlie",
                    "name": "Charlie Brown",
                    "display_name": "Charlie Brown",
                    "title": "Logistics Specialist",
                    "email": "charlie@acmeindustrial.com",
                },
            ],
        }
        blob_v2 = SnapshotBlob(
            snapshot_id=snap_v2.id,
            payload=blob_v2_data,
        )
        db.add(blob_v2)
        db.flush()

        # 3. Seed Target Tenant Schema (client_test_c2_tgt) with V1 hydrated state
        tgt_schema = schema_for(slug_target)

        # Account
        res_acct = db.execute(
            text(f"""
                INSERT INTO "{tgt_schema}"."accounts"
                (key, display_name, legal_name, domain, created_at)
                VALUES ('acmeindustrial.com', 'Acme Industrial Systems', 'Acme Industrial Inc', 'acmeindustrial.com', NOW())
                RETURNING id
            """)
        ).fetchone()
        account_id = res_acct[0]

        # Account Origin (pointing to V1)
        db.execute(
            text(f"""
                INSERT INTO "{tgt_schema}"."account_origin"
                (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, local_edits, hydrated_at)
                VALUES (:aid, 'catalog', 'acmeindustrial.com', :sid, 1, true, NOW())
            """),
            {"aid": account_id, "sid": snap_v1.id},
        )

        # LOB 1: Protected by manual verification
        res_lob = db.execute(
            text(f"""
                INSERT INTO "{tgt_schema}"."lobs"
                (account_id, key, lob_name, overview, is_manually_verified, manually_verified_at, local_edited_at)
                VALUES (:aid, 'lob_mfg', 'Manufacturing Solutions', 'Custom local tenant overview note', true, NOW(), NOW())
                RETURNING id
            """),
            {"aid": account_id},
        ).fetchone()
        lob1_id = res_lob[0]

        # Persona 1: Alice (Protected by manual verification)
        res_p1 = db.execute(
            text(f"""
                INSERT INTO "{tgt_schema}"."personas"
                (account_id, lob_id, key, display_name, full_name, title, email, is_manually_verified, manually_verified_at, local_edited_at)
                VALUES (:aid, :lid, 'per_alice', 'Alice Smith', 'Alice Smith', 'VP Manufacturing', 'alice@acmeindustrial.com', true, NOW(), NOW())
                RETURNING id
            """),
            {"aid": account_id, "lid": lob1_id},
        ).fetchone()
        persona1_id = res_p1[0]

        # Persona 2: Bob (Not manually verified)
        res_p2 = db.execute(
            text(f"""
                INSERT INTO "{tgt_schema}"."personas"
                (account_id, lob_id, key, display_name, full_name, title, email, is_manually_verified)
                VALUES (:aid, :lid, 'per_bob', 'Bob Jones', 'Bob Jones', 'Operations Lead', 'bob@acmeindustrial.com', false)
                RETURNING id
            """),
            {"aid": account_id, "lid": lob1_id},
        ).fetchone()
        persona2_id = res_p2[0]

        # Child: Action item linked to account and Persona 1 (must remain intact!)
        res_ai = db.execute(
            text(f"""
                INSERT INTO "{tgt_schema}"."action_items"
                (account_id, persona_id, title, status, priority, source, created_at)
                VALUES (:aid, :pid, 'Follow up on manufacturing automation roadmap', 'open', 'medium', 'manual', NOW())
                RETURNING id
            """),
            {"aid": account_id, "pid": persona1_id},
        ).fetchone()
        action_item_id = res_ai[0]

        db.commit()

        yield {
            "slug_source": slug_source,
            "slug_target": slug_target,
            "target_schema": tgt_schema,
            "super_user": super_admin,
            "client_admin": client_admin,
            "viewer_user": viewer_user,
            "account_id": account_id,
            "lob1_id": lob1_id,
            "persona1_id": persona1_id,
            "persona2_id": persona2_id,
            "action_item_id": action_item_id,
            "snap_v1_id": snap_v1.id,
            "snap_v2_id": snap_v2.id,
            "company_id": company.id,
        }

    drop_client_schema(slug_source)
    drop_client_schema(slug_target)


def test_refresh_preview_diff_and_anti_oracle(c2_env):
    """C2: Tests GET /api/accounts/{account_id}/reuse/refresh-preview with anti-oracle and diff counts."""
    client = TestClient(app)
    token = auth.create_access_token(c2_env["client_admin"])
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Slug": c2_env["slug_target"],
    }

    url = f"/api/accounts/{c2_env['account_id']}/reuse/refresh-preview"
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()

    # 1. Verify DiffView Structure
    assert data["account_id"] == c2_env["account_id"]
    assert data["company_name"] == "Acme Industrial Systems"
    assert data["refresh_policy"]["snapshot_version_from"] == 1
    assert data["refresh_policy"]["snapshot_version_to"] == 2
    assert data["refresh_policy"]["status"] == "blocked_local_edits"

    # 2. Verify summary counts
    summary = data["summary"]
    assert summary["personas"]["added"] == 1       # Charlie
    assert summary["personas"]["changed"] == 2     # Alice & Bob
    assert summary["lobs"]["added"] == 1           # Supply chain
    assert summary["lobs"]["changed"] == 1         # Manufacturing
    assert len(summary["account_fields_changed"]) > 0

    # 3. Verify local-edits & protected rows identification
    local_edits = data["local_edits"]
    assert local_edits["present"] is True
    protected_tables = [p["table"] for p in local_edits["protected_rows"]]
    assert "lobs" in protected_tables
    assert "personas" in protected_tables

    # 4. Verify cryptographic preview_token & deterministic plan_hash
    assert "preview_token" in data and len(data["preview_token"]) > 20
    assert "plan_hash" in data and len(data["plan_hash"]) == 64

    # 5. Anti-Oracle Check
    serialized = json.dumps(data)
    assert "test_c2_src" not in serialized
    assert "canonical_key" not in serialized


def test_apply_non_edited_preserves_protected_rows(c2_env):
    """C2: apply_non_edited updates unedited rows, inserts new rows, and never touches protected rows."""
    client = TestClient(app)
    token = auth.create_access_token(c2_env["client_admin"])
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Slug": c2_env["slug_target"],
    }

    # 1. Fetch preview to get preview_token and plan_hash
    prev_res = client.get(f"/api/accounts/{c2_env['account_id']}/reuse/refresh-preview", headers=headers)
    assert prev_res.status_code == 200
    preview_data = prev_res.json()

    # 2. Apply non-edited refresh
    payload = {
        "snapshot_id": c2_env["snap_v2_id"],
        "preview_token": preview_data["preview_token"],
        "strategy": "apply_non_edited",
    }
    apply_res = client.post(f"/api/accounts/{c2_env['account_id']}/reuse/refresh", json=payload, headers=headers)
    assert apply_res.status_code == 200, apply_res.text
    res_data = apply_res.json()

    assert res_data["ok"] is True
    assert res_data["snapshot_version"] == 2
    assert res_data["strategy"] == "apply_non_edited"
    assert res_data["skipped_protected"] == 2  # Alice and Lob 1 skipped!

    # 3. Verify in database
    with get_session() as db:
        tgt_schema = c2_env["target_schema"]

        # Alice must STILL have 'VP Manufacturing' and is_manually_verified = true
        alice = db.execute(
            text(f'SELECT title, is_manually_verified FROM "{tgt_schema}"."personas" WHERE id = :id'),
            {"id": c2_env["persona1_id"]},
        ).fetchone()
        assert alice[0] == "VP Manufacturing"
        assert alice[1] is True

        # Bob (not protected) must have updated to 'Director of Operations'
        bob = db.execute(
            text(f'SELECT title FROM "{tgt_schema}"."personas" WHERE id = :id'),
            {"id": c2_env["persona2_id"]},
        ).fetchone()
        assert bob[0] == "Director of Operations"

        # Charlie (new persona) must have been inserted
        charlie = db.execute(
            text(f'SELECT id, title FROM "{tgt_schema}"."personas" WHERE account_id = :aid AND display_name = :n'),
            {"aid": c2_env["account_id"], "n": "Charlie Brown"},
        ).fetchone()
        assert charlie is not None
        assert charlie[1] == "Logistics Specialist"

        # Lob 1 (protected) must retain custom local overview
        lob1 = db.execute(
            text(f'SELECT overview, is_manually_verified FROM "{tgt_schema}"."lobs" WHERE id = :id'),
            {"id": c2_env["lob1_id"]},
        ).fetchone()
        assert lob1[0] == "Custom local tenant overview note"
        assert lob1[1] is True

        # Lob 2 (Supply chain) must have been inserted
        lob2 = db.execute(
            text(f'SELECT id, lob_name FROM "{tgt_schema}"."lobs" WHERE account_id = :aid AND key = :k'),
            {"aid": c2_env["account_id"], "k": "lob_supply_chain"},
        ).fetchone()
        assert lob2 is not None

        # Child Action item must STILL exist and link to Alice!
        ai = db.execute(
            text(f'SELECT id, persona_id, status FROM "{tgt_schema}"."action_items" WHERE id = :id'),
            {"id": c2_env["action_item_id"]},
        ).fetchone()
        assert ai is not None
        assert ai[1] == c2_env["persona1_id"]

        # account_origin must show snapshot_version = 2 and local_edits = true (protected rows remain)
        origin = db.execute(
            text(f'SELECT snapshot_version, local_edits FROM "{tgt_schema}"."account_origin" WHERE account_id = :aid'),
            {"aid": c2_env["account_id"]},
        ).fetchone()
        assert origin[0] == 2
        assert origin[1] is True


def test_plan_hash_mismatch_returns_409(c2_env):
    """C2: Mismatched or tampered preview_token plan_hash triggers HTTP 409 plan_changed."""
    client = TestClient(app)
    token = auth.create_access_token(c2_env["client_admin"])
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Slug": c2_env["slug_target"],
    }

    # Generate a preview token with a fake plan_hash
    from services.account_reuse_service import AccountReuseService
    fake_token = AccountReuseService.generate_preview_token(
        snapshot_id=c2_env["snap_v2_id"],
        target_client_slug=c2_env["slug_target"],
        user_id=c2_env["client_admin"].id,
        plan_hash="0000000000000000000000000000000000000000000000000000000000000000",
    )

    payload = {
        "snapshot_id": c2_env["snap_v2_id"],
        "preview_token": fake_token,
        "strategy": "apply_non_edited",
    }
    res = client.post(f"/api/accounts/{c2_env['account_id']}/reuse/refresh", json=payload, headers=headers)
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "plan_changed"


def test_apply_all_overwrite_requires_admin_confirm_and_creates_backup(c2_env):
    """C2: apply_all_overwrite requires client_admin + confirm_overwrite + reason and stores pre-refresh backup."""
    client = TestClient(app)

    # 1. Viewer attempt -> 403 Forbidden
    viewer_token = auth.create_access_token(c2_env["viewer_user"])
    viewer_headers = {
        "Authorization": f"Bearer {viewer_token}",
        "X-Client-Slug": c2_env["slug_target"],
    }
    res_v = client.post(
        f"/api/accounts/{c2_env['account_id']}/reuse/refresh",
        json={
            "snapshot_id": c2_env["snap_v2_id"],
            "preview_token": "dummy",
            "strategy": "apply_all_overwrite",
            "confirm_overwrite": True,
            "reason": "Test overwrite",
        },
        headers=viewer_headers,
    )
    assert res_v.status_code == 403

    # 2. Client Admin attempt without confirm_overwrite -> 400 Bad Request
    admin_token = auth.create_access_token(c2_env["client_admin"])
    admin_headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Client-Slug": c2_env["slug_target"],
    }

    # Fetch valid preview token
    prev = client.get(f"/api/accounts/{c2_env['account_id']}/reuse/refresh-preview", headers=admin_headers).json()

    res_no_confirm = client.post(
        f"/api/accounts/{c2_env['account_id']}/reuse/refresh",
        json={
            "snapshot_id": c2_env["snap_v2_id"],
            "preview_token": prev["preview_token"],
            "strategy": "apply_all_overwrite",
            "confirm_overwrite": False,
            "reason": "Test overwrite",
        },
        headers=admin_headers,
    )
    assert res_no_confirm.status_code == 400

    # 3. Successful Overwrite by Admin with confirm_overwrite and reason
    res_ok = client.post(
        f"/api/accounts/{c2_env['account_id']}/reuse/refresh",
        json={
            "snapshot_id": c2_env["snap_v2_id"],
            "preview_token": prev["preview_token"],
            "strategy": "apply_all_overwrite",
            "confirm_overwrite": True,
            "reason": "Full corporate intelligence synchronization",
        },
        headers=admin_headers,
    )
    assert res_ok.status_code == 200, res_ok.text
    data = res_ok.json()
    assert data["ok"] is True
    assert data["strategy"] == "apply_all_overwrite"

    # 4. Verify database: Alice was overwritten, backup was created, local_edits = false
    with get_session() as db:
        tgt_schema = c2_env["target_schema"]

        # Alice's title overwritten to 'SVP Manufacturing', is_manually_verified = false
        alice = db.execute(
            text(f'SELECT title, is_manually_verified FROM "{tgt_schema}"."personas" WHERE id = :id'),
            {"id": c2_env["persona1_id"]},
        ).fetchone()
        assert alice[0] == "SVP Manufacturing"
        assert alice[1] is False

        # Lob 1 overview overwritten
        lob1 = db.execute(
            text(f'SELECT overview, is_manually_verified FROM "{tgt_schema}"."lobs" WHERE id = :id'),
            {"id": c2_env["lob1_id"]},
        ).fetchone()
        assert "Advanced robotics manufacturing systems" in lob1[0]
        assert lob1[1] is False

        # account_origin.local_edits = false
        origin = db.execute(
            text(f'SELECT local_edits FROM "{tgt_schema}"."account_origin" WHERE account_id = :aid'),
            {"aid": c2_env["account_id"]},
        ).fetchone()
        assert origin[0] is False

        # Verify Backup was recorded in account_origin_backups table
        backup = db.execute(
            text(f'SELECT strategy, snapshot_version_before, snapshot_version_after, backup_payload FROM "{tgt_schema}"."account_origin_backups" WHERE account_id = :aid'),
            {"aid": c2_env["account_id"]},
        ).fetchone()
        assert backup is not None
        assert backup[0] == "apply_all_overwrite"
        assert backup[1] == 1
        assert backup[2] == 2
        payload = backup[3]
        if isinstance(payload, str):
            payload = json.loads(payload)
        # Verify Alice's old state was saved in the backup payload
        old_personas = payload.get("personas", [])
        alice_old = [p for p in old_personas if p.get("id") == c2_env["persona1_id"]]
        assert len(alice_old) == 1
        assert alice_old[0]["title"] == "VP Manufacturing"
