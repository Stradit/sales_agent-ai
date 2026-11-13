"""Test Suite: S-20 / S-21 Publish Concurrency and Normalized Checksum.

Validates:
1. S-21: Volatile timestamp/analytics changes do not alter checksum (deduplicates).
2. Meaningful changes (e.g. persona title) generate a new version.
3. S-20: Concurrent publishes under coarse + per-company locks prevent version collisions.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import random
import uuid
import pytest
from sqlalchemy import text

from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.platform_catalog import Company, Snapshot
from db.provisioning import create_client_schema, drop_client_schema
from services.catalog_service import CatalogService

TEST_CONCURRENCY_SLUG = "test_src_concurr_001"
TEST_CONCURRENCY_SCHEMA = f"client_{TEST_CONCURRENCY_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_concurrency_env():
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=TEST_CONCURRENCY_SLUG).first()
        if not client:
            client = Client(
                name="Concurrency Test Corp",
                slug=TEST_CONCURRENCY_SLUG,
                schema_name=TEST_CONCURRENCY_SCHEMA,
                status="active",
                catalog_publish=True,
                catalog_consume=True,
            )
            session.add(client)
            session.commit()
        create_client_schema(TEST_CONCURRENCY_SLUG)
    finally:
        session.close()

    yield

    engine.dispose()
    drop_client_schema(TEST_CONCURRENCY_SLUG)
    clean_session = SessionLocal()
    try:
        clean_session.execute(text(f"DELETE FROM public.clients WHERE slug = '{TEST_CONCURRENCY_SLUG}'"))
        clean_session.commit()
    finally:
        clean_session.close()


def test_normalized_checksum_deduplication():
    """Volatile timestamp and engagement changes produce identical normalized checksums."""
    session = SessionLocal()
    try:
        uid = uuid.uuid4().hex[:6]
        cik = f"{random.randint(1000000, 9999999):010d}"
        acc_key = f"acc_dedup_{uid}"
        domain = f"dedup-{uid}.com"

        session.execute(text(f"""
            INSERT INTO "{TEST_CONCURRENCY_SCHEMA}"."accounts"
            (key, display_name, legal_name, domain, sec_cik, updated_at)
            VALUES ('{acc_key}', 'Dedup Corp', 'Dedup Corp Inc', '{domain}', '{cik}', now())
        """))
        session.commit()
        acc_id = session.execute(text(f'SELECT id FROM "{TEST_CONCURRENCY_SCHEMA}"."accounts" WHERE key = \'{acc_key}\'')).scalar()

        # Publish 1
        snap1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=TEST_CONCURRENCY_SLUG,
            account_id=acc_id,
            force=True,
        )
        assert snap1 is not None
        assert snap1.version == 1
        assert snap1.checksum_version == 2

        # Update only volatile fields on account (updated_at)
        session.execute(text(f"""
            UPDATE "{TEST_CONCURRENCY_SCHEMA}"."accounts"
            SET updated_at = now() + interval '1 hour'
            WHERE id = {acc_id}
        """))
        session.commit()

        # Publish 2: should deduplicate and return snap1 without creating a new version
        snap2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=TEST_CONCURRENCY_SLUG,
            account_id=acc_id,
            force=False,
        )
        assert snap2.id == snap1.id
        assert snap2.version == 1

        # Now change a meaningful field: legal_name
        session.execute(text(f"""
            UPDATE "{TEST_CONCURRENCY_SCHEMA}"."accounts"
            SET legal_name = 'Dedup Corp Renamed Global'
            WHERE id = {acc_id}
        """))
        session.commit()

        # Publish 3: should create version 2
        snap3 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=TEST_CONCURRENCY_SLUG,
            account_id=acc_id,
            force=False,
        )
        assert snap3.id != snap1.id
        assert snap3.version == 2
        assert snap3.checksum != snap1.checksum
    finally:
        session.close()


def test_concurrent_publishes_no_race():
    """Concurrent threads publishing on the same account/company never crash and maintain contiguous versions."""
    session = SessionLocal()
    try:
        uid = uuid.uuid4().hex[:6]
        cik = f"{random.randint(1000000, 9999999):010d}"
        acc_key = f"acc_concurr_{uid}"
        domain = f"concurr-{uid}.com"

        session.execute(text(f"""
            INSERT INTO "{TEST_CONCURRENCY_SCHEMA}"."accounts"
            (key, display_name, legal_name, domain, sec_cik)
            VALUES ('{acc_key}', 'Concurrent Corp', 'Concurrent Corp Inc', '{domain}', '{cik}')
        """))
        session.commit()
        acc_id = session.execute(text(f'SELECT id FROM "{TEST_CONCURRENCY_SCHEMA}"."accounts" WHERE key = \'{acc_key}\'')).scalar()
    finally:
        session.close()

    errors = []
    snapshot_ids = []

    def _publish_worker(idx: int):
        s = SessionLocal()
        try:
            # Force creates a new snapshot version each time
            snap = CatalogService.publish_account_snapshot(
                session=s,
                source_client_slug=TEST_CONCURRENCY_SLUG,
                account_id=acc_id,
                force=True,
            )
            return snap.id, snap.version
        except Exception as e:
            errors.append(str(e))
            raise
        finally:
            s.close()

    # 20 concurrent publishes across 5 worker threads
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_publish_worker, i) for i in range(20)]
        for f in as_completed(futures):
            try:
                sid, ver = f.result()
                snapshot_ids.append((sid, ver))
            except Exception:
                pass

    assert len(errors) == 0, f"Concurrent publish errors: {errors}"
    assert len(snapshot_ids) == 20

    # Verify versions are strictly 1..20 contiguous
    verify_session = SessionLocal()
    try:
        company = CatalogService.get_company_by_identifier(verify_session, cik)
        assert company is not None
        versions = [
            s.version for s in verify_session.query(Snapshot)
            .filter_by(company_id=company.id)
            .order_by(Snapshot.version)
            .all()
        ]
        assert versions == list(range(1, 21)), f"Versions were not contiguous: {versions}"
    finally:
        verify_session.close()
