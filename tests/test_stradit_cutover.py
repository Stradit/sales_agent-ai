"""Tests for Phase 6 Stradit Cutover & Rehearsal Tooling."""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine, text

import config
from db.connection import SessionLocal, engine as default_engine
from scripts.stradit_cutover import StraditCutoverManager
from services.audit_service import AuditService


@pytest.fixture(scope="module")
def cutover_mgr():
    return StraditCutoverManager(slug="stradit_india")


def test_stradit_cutover_schema_exists(cutover_mgr):
    assert cutover_mgr.check_schema_exists() is True


def test_stradit_cutover_manifest_compliance(cutover_mgr):
    report = cutover_mgr.verify_manifest()
    assert report["is_compliant"] is True
    assert report["missing_tables"] == []
    assert report["fk_mismatches"] == []
    assert report["skeleton_version"] == "T0002"


def test_stradit_cutover_data_parity(cutover_mgr):
    parity = cutover_mgr.verify_data_parity()
    assert len(parity) >= 58
    for item in parity:
        assert item["status"].startswith("MATCH") or item["status"] == "TENANT_ONLY", (
            f"Table {item['table']} has parity mismatch: {item}"
        )


def test_stradit_cutover_foreign_key_orphans(cutover_mgr):
    orphans = cutover_mgr.verify_foreign_key_orphans()
    assert len(orphans) > 0
    for o in orphans:
        assert o["clean"] is True, f"Found orphan records: {o}"


def test_stradit_cutover_sequence_sync(cutover_mgr):
    seqs = cutover_mgr.verify_sequence_sync()
    assert len(seqs) > 0
    for s in seqs:
        assert s["synced"] is True, f"Sequence out of sync: {s}"


def test_stradit_cutover_full_verification(cutover_mgr):
    report = cutover_mgr.run_full_verification()
    assert report["ok"] is True
    assert report["manifest"]["is_compliant"] is True
    assert report["parity"]["clean"] is True
    assert report["orphans"]["clean"] is True
    assert report["sequences"]["clean"] is True


def test_stradit_cutover_dry_run_archive(cutover_mgr):
    result = cutover_mgr.archive_public_tenant_tables(dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["tables_count"] >= 58
    assert "archive_schema" in result
    assert "accounts" in result["tables_to_archive"]
    assert "users" not in result["tables_to_archive"]  # Platform tables must NEVER be archived


def test_stradit_cutover_archive_and_restore_rehearsal():
    """Hermetic test of archive & rollback mechanism using a dedicated scratch probe table."""
    with default_engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS public.tmp_cutover_probe (id serial primary key, note text)"))
        conn.execute(text("INSERT INTO public.tmp_cutover_probe (note) VALUES ('rehearsal_payload')"))

    mgr = StraditCutoverManager(slug="stradit_india")
    archive_schema = "test_rehearsal_archive_schema"

    try:
        # Move probe table into test archive schema
        with default_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{archive_schema}"'))
            conn.execute(text(f'ALTER TABLE public.tmp_cutover_probe SET SCHEMA "{archive_schema}"'))

        # Check table moved
        with default_engine.connect() as conn:
            in_pub = conn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'tmp_cutover_probe'")).scalar()
            in_arch = conn.execute(text(f"SELECT 1 FROM information_schema.tables WHERE table_schema = '{archive_schema}' AND table_name = 'tmp_cutover_probe'")).scalar()
            assert not in_pub
            assert in_arch

        # Test restore
        restore_result = mgr.restore_public_tenant_tables(archive_schema)
        assert restore_result["ok"] is True
        assert "tmp_cutover_probe" in restore_result["restored_tables"]

        # Check table restored in public
        with default_engine.connect() as conn:
            in_pub_restored = conn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'tmp_cutover_probe'")).scalar()
            assert in_pub_restored

    finally:
        with default_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS public.tmp_cutover_probe CASCADE"))
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{archive_schema}" CASCADE'))
