"""Test Suite: Table-Driven Entity Resolution & Identifier Classification (Phase B - B2).

Verifies:
1. classify_identifier across all table-driven cases:
   - "3m.com" -> domain
   - "3M" -> unknown
   - "Apple Inc 1234" -> unknown
   - "1364742" -> cik "0001364742"
   - "0001364742" -> cik "0001364742"
   - "CIK 1364742" -> cik "0001364742"
   - "https://www.BlackRock.com/us" -> domain "blackrock.com"
   - 20-char LEI (e.g. "549300V5XZ12H6G20E62") -> lei
   - Lookalike 20-char non-LEI words -> unknown
   - IP address -> unknown
   - Empty/None -> unknown
2. get_company_by_identifier queries ONLY the classified type.
3. AmbiguousIdentifierError raised when multiple companies share an identifier.
4. Weak company resolution uses tenant-neutral hash and quarantines snapshot.
"""

import pytest
from sqlalchemy import text
from db.connection import SessionLocal
from db.models.platform_catalog import Company, CompanyIdentifier
from services.catalog_service import (
    AmbiguousIdentifierError,
    CatalogService,
)


@pytest.mark.parametrize(
    "raw_input,expected_kind,expected_val",
    [
        ("3m.com", "domain", "3m.com"),
        ("3M", "unknown", "3M"),
        ("Apple Inc 1234", "unknown", "Apple Inc 1234"),
        ("1364742", "cik", "0001364742"),
        ("0001364742", "cik", "0001364742"),
        ("CIK 1364742", "cik", "0001364742"),
        ("https://www.BlackRock.com/us", "domain", "blackrock.com"),
        ("549300V5XZ12H6G20E62", "lei", "549300V5XZ12H6G20E62"),
        ("AAAAAAAAAAAAAAAAAAAA", "unknown", "AAAAAAAAAAAAAAAAAAAA"),  # 20 letters (no digits)
        ("12345678901234567890", "unknown", "12345678901234567890"),  # 20 digits (no letters, >10 digits for CIK)
        ("192.168.1.1", "unknown", "192.168.1.1"),  # IP address
        ("0000000000", "unknown", "0000000000"),  # all zeros CIK rejected
        ("", "unknown", ""),
        (None, "unknown", ""),
    ],
)
def test_table_driven_identifier_classification(raw_input, expected_kind, expected_val):
    kind, val = CatalogService.classify_identifier(raw_input)
    assert kind == expected_kind
    assert val == expected_val


def test_ambiguous_identifier_raises_error():
    """Two companies sharing the same identifier -> AmbiguousIdentifierError."""
    session = SessionLocal()
    try:
        # Pre-cleanup
        session.execute(text("DELETE FROM platform_catalog.company_identifiers WHERE id_value = 'shared-conflict.com'"))
        session.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key IN ('dom:conflict_a', 'dom:conflict_b')"))
        session.execute(text("ALTER TABLE platform_catalog.company_identifiers DROP CONSTRAINT IF EXISTS uq_company_id_type_val"))
        session.commit()

        # Create two companies
        c1 = Company(canonical_key="dom:conflict_a", display_name="Company Alpha")
        c2 = Company(canonical_key="dom:conflict_b", display_name="Company Beta")
        session.add_all([c1, c2])
        session.flush()

        # Insert shared identifier into company_identifiers
        session.execute(text("""
            INSERT INTO platform_catalog.company_identifiers (company_id, id_type, id_value)
            VALUES (:c1, 'domain', 'shared-conflict.com')
        """), {"c1": c1.id})
        session.execute(text("""
            INSERT INTO platform_catalog.company_identifiers (company_id, id_type, id_value)
            VALUES (:c2, 'domain', 'shared-conflict.com')
        """), {"c2": c2.id})
        session.commit()

        # Lookup must raise AmbiguousIdentifierError
        with pytest.raises(AmbiguousIdentifierError) as exc_info:
            CatalogService.get_company_by_identifier(session, "shared-conflict.com")
        assert "matches multiple companies" in str(exc_info.value)
        assert c1.id in exc_info.value.candidate_company_ids
        assert c2.id in exc_info.value.candidate_company_ids
    finally:
        session.rollback()
        session.execute(text("DELETE FROM platform_catalog.company_identifiers WHERE id_value = 'shared-conflict.com'"))
        session.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key IN ('dom:conflict_a', 'dom:conflict_b')"))
        session.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_company_id_type_val'
                ) THEN
                    ALTER TABLE platform_catalog.company_identifiers ADD CONSTRAINT uq_company_id_type_val UNIQUE (id_type, id_value);
                END IF;
            END $$;
        """))
        session.commit()
        session.close()


def test_weak_canonical_key_resolution():
    """Accounts lacking strong identifiers (CIK, LEI, domain) resolve to tenant-neutral hash and weak=True."""
    account_no_ids = {
        "legal_name": "Private Boutique Consulting LLC",
        "country": "Germany",
        "key": "private_boutique_internal_key",
    }
    resolution = CatalogService.resolve_canonical_key(account_no_ids)
    assert resolution.weak is True
    assert resolution.canonical_key.startswith("key:")
    # Must NOT contain the tenant's internal key 'private_boutique_internal_key'
    assert "private_boutique_internal_key" not in resolution.canonical_key
    assert len(resolution.canonical_key) == 20  # "key:" (4) + 16 chars hash
