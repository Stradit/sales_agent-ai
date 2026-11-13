"""Test Suite: S-12 Share Policy Enforcer.

Validates:
1. Unknown column in tenant table is dropped and logged with warning.
2. Post limiting (age cutoff + row count limit) works as configured in share_policy.yaml.
3. Deeply nested JSON PII (nested dicts/lists with sensitive keys and email/phone strings) is recursively scrubbed.
4. Policy hash is stable and stored on published snapshot row.
5. Contact email and phone switchboard vs personal domain filtering (B6.5).
"""

from datetime import datetime, timezone, timedelta
import logging
from pathlib import Path
import pytest
from sqlalchemy import text
from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.platform_catalog import Snapshot
from db.models.user import User
from db.provisioning import create_client_schema, drop_client_schema
import auth
from services.catalog_service import CatalogService, get_share_policy
from services.share_policy_enforcer import (
    compute_policy_hash,
    get_manifest_skeleton_version,
    sanitize_contact_email,
    sanitize_phone_number,
    scrub_json_pii,
)

TEST_ENFORCE_SLUG = "test_policy_enforce_001"
TEST_ENFORCE_SCHEMA = f"client_{TEST_ENFORCE_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_policy_enforce_env():
    session = SessionLocal()
    try:
        session.execute(text(f"DELETE FROM public.clients WHERE slug = '{TEST_ENFORCE_SLUG}'"))
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'policy_enforce_%'"))
        session.commit()

        create_client_schema(TEST_ENFORCE_SLUG)

        client = session.query(Client).filter_by(slug=TEST_ENFORCE_SLUG).first()
        client.status = "active"
        client.catalog_publish = True
        client.catalog_consume = True

        pwd = auth.hash_password("testpass123")
        u_super = session.query(User).filter_by(role="super_admin").first()
        if not u_super:
            u_super = User(email="policy_enforce_super@corp.com", hashed_password=pwd, role="super_admin", full_name="Super Admin")
            session.add(u_super)
            session.flush()

        session.commit()
    finally:
        session.close()

    yield

    engine.dispose()
    drop_client_schema(TEST_ENFORCE_SLUG)
    clean_session = SessionLocal()
    try:
        clean_session.execute(text(f"DELETE FROM public.clients WHERE slug = '{TEST_ENFORCE_SLUG}'"))
        clean_session.execute(text("DELETE FROM public.users WHERE email LIKE 'policy_enforce_%'"))
        clean_session.commit()
    finally:
        clean_session.close()


def test_recursive_json_pii_scrubber():
    """Validates deeply nested JSON PII stripping and redaction."""
    input_payload = {
        "config": {
            "user": {
                "email": "ceo@personal.com",
                "phone_number": "123-456-7890",
                "ssn": "000-11-2222",
                "secret_key": "topsecret123",
                "bearer_token": "xyz987",
                "nested_notes": [
                    "Please contact ceo@acme.com or call +1-555-0199 directly.",
                    {"personal_email": "hidden@yahoo.com", "safe_note": "Quarterly review"},
                ],
            },
            "allowed_setting": True,
            "max_connections": 50,
        },
        "salary_info": {"amount": 150000},
    }

    scrubbed = scrub_json_pii(input_payload)

    # 1. Blacklisted keys dropped entirely
    assert "salary_info" not in scrubbed
    user_data = scrubbed["config"]["user"]
    assert "email" not in user_data
    assert "phone_number" not in user_data
    assert "ssn" not in user_data
    assert "secret_key" not in user_data
    assert "bearer_token" not in user_data

    # 2. Nested dict in list has sensitive key dropped
    nested_list = user_data["nested_notes"]
    assert "personal_email" not in nested_list[1]
    assert nested_list[1]["safe_note"] == "Quarterly review"

    # 3. String values have regex email and phone redacted
    redacted_note = nested_list[0]
    assert "ceo@acme.com" not in redacted_note
    assert "[REDACTED_EMAIL]" in redacted_note
    assert "+1-555-0199" not in redacted_note
    assert "[REDACTED_PHONE]" in redacted_note

    # 4. Safe non-PII preserved
    assert scrubbed["config"]["allowed_setting"] is True
    assert scrubbed["config"]["max_connections"] == 50


def test_contact_email_and_phone_sanitization():
    """Validates B6.5 switchboard vs personal domain evaluation."""
    # Switchboard at company domain -> KEEP
    assert sanitize_contact_email("info@acme.com", "acme.com") == "info@acme.com"
    assert sanitize_contact_email("contact@acme.com", "acme.com") == "contact@acme.com"
    assert sanitize_contact_email("support@sub.acme.com", "acme.com") == "support@sub.acme.com"
    assert sanitize_contact_email("press@acme.com", "acme.com") == "press@acme.com"

    # Personal email domains -> DROP
    assert sanitize_contact_email("founder@gmail.com", "acme.com") is None
    assert sanitize_contact_email("ceo@yahoo.com", "acme.com") is None
    assert sanitize_contact_email("info@hotmail.com", "acme.com") is None

    # Individual personal name at company domain -> DROP
    assert sanitize_contact_email("john.doe@acme.com", "acme.com") is None
    assert sanitize_contact_email("sarah_connor@acme.com", "acme.com") is None

    # Phone sanitization
    assert sanitize_phone_number("+1-800-555-0100", is_switchboard=True) == "+1-800-555-0100"
    assert sanitize_phone_number(None) is None


def test_policy_hash_stability():
    """Validates that compute_policy_hash returns a stable SHA-256 string."""
    hash1 = compute_policy_hash()
    hash2 = compute_policy_hash()
    assert hash1 == hash2
    assert len(hash1) == 64
    int(hash1, 16)  # Must be valid hex


def test_unknown_column_dropped_and_logged(caplog):
    """Adds a temporary unknown column to tenant table, verifies it is dropped and logged."""
    session = SessionLocal()
    u_super = session.query(User).filter_by(role="super_admin").first()
    try:
        # Add a custom untracked column in the tenant accounts table
        session.execute(text(f'ALTER TABLE "{TEST_ENFORCE_SCHEMA}"."accounts" ADD COLUMN IF NOT EXISTS "unapproved_secret_metric" text'))
        session.commit()

        # Seed account with value in the unapproved column
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{TEST_ENFORCE_SCHEMA}"."accounts"
                (key, display_name, domain, sec_cik, unapproved_secret_metric, owner_user_id)
                VALUES
                ('unapproved_test_co', 'Unapproved Metric Corp', 'unapproved-co.com', '0007788991', 'SECRET_12345', :uid)
                RETURNING id
            """),
            {"uid": u_super.id},
        ).scalar()
        session.commit()

        # Extract payload with logging captured at WARNING level
        with caplog.at_level(logging.WARNING):
            payload = CatalogService.extract_and_sanitize_payload(session, TEST_ENFORCE_SLUG, acct_id)

        # Unapproved column must NOT be in extracted account
        assert "unapproved_secret_metric" not in payload["account"]

        # Warning must be logged
        assert "unapproved_secret_metric" in caplog.text
        assert "accounts" in caplog.text
    finally:
        session.execute(text(f'ALTER TABLE "{TEST_ENFORCE_SCHEMA}"."accounts" DROP COLUMN IF EXISTS "unapproved_secret_metric"'))
        session.commit()
        session.close()


def test_post_limiting_age_and_rows():
    """Verifies that posts older than max_age_days are omitted and posts are capped at max_rows."""
    session = SessionLocal()
    u_super = session.query(User).filter_by(role="super_admin").first()
    try:
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{TEST_ENFORCE_SCHEMA}"."accounts"
                (key, display_name, domain, sec_cik, owner_user_id)
                VALUES
                ('post_test_co', 'Post Test Corp', 'post-test.com', '0007788992', :uid)
                RETURNING id
            """),
            {"uid": u_super.id},
        ).scalar()

        policy = get_share_policy()
        max_age_days = int(policy["rules"].get("posts_max_age_days", 90))
        max_rows = int(policy["rules"].get("posts_max_rows", 100))

        now = datetime.now(timezone.utc)
        # Insert 150 fresh posts (< 90 days) and 20 old posts (> 90 days)
        for i in range(120):
            session.execute(
                text(f"""
                    INSERT INTO "{TEST_ENFORCE_SCHEMA}"."posts"
                    (target_key, post_key, kind, channel, published_at, body, raw)
                    VALUES
                    ('post_test_co', :pid, 'post', 'linkedin', :pub, 'Fresh post text', '{{}}'::jsonb)
                """),
                {"pid": f"fresh_post_{i}", "pub": (now - timedelta(days=i % 30)).isoformat()},
            )
        for j in range(15):
            session.execute(
                text(f"""
                    INSERT INTO "{TEST_ENFORCE_SCHEMA}"."posts"
                    (target_key, post_key, kind, channel, published_at, body, raw)
                    VALUES
                    ('post_test_co', :pid, 'post', 'linkedin', :pub, 'Stale post text', '{{}}'::jsonb)
                """),
                {"pid": f"stale_post_{j}", "pub": (now - timedelta(days=max_age_days + 10 + j)).isoformat()},
            )
        session.commit()

        payload = CatalogService.extract_and_sanitize_payload(session, TEST_ENFORCE_SLUG, acct_id)
        extracted_posts = payload["posts"]

        # Must be capped at max_rows (100)
        assert len(extracted_posts) <= max_rows

        # No stale post must be included
        stale_ids = {p.get("post_key") for p in extracted_posts}
        for j in range(15):
            assert f"stale_post_{j}" not in stale_ids

        # Publish snapshot and verify policy_hash and skeleton_version are stored
        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=TEST_ENFORCE_SLUG,
            account_id=acct_id,
            published_by_user_id=u_super.id,
            force=True,
        )
        assert snap.policy_hash == compute_policy_hash()
        assert snap.skeleton_version == get_manifest_skeleton_version()
    finally:
        session.close()
