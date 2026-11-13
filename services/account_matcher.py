"""Account matching and duplicate detection engine for cross-tenant intelligence reuse (B8: S-17/S-18).

Unifies duplicate detection across reuse-check and hydration:
1. Matches by SEC CIK: zero-padded 10-digit string normalization on both sides.
2. Matches by Domain: normalized hostname comparison against domain and primary_domain.
3. Matches by Provenance: account_origin.canonical_key = company.canonical_key.
4. Strictly guards against empty strings and NULL values.
5. Never matches on 'key' alone.
"""

from dataclasses import dataclass
import hashlib
import logging
import re
from typing import Any, Optional, Union
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.tenant_context import TenantRequestContext
from db.models.client import Client
from db.models.platform_catalog import Company, CompanyIdentifier
from db.tenant import schema_for, validate_slug
from services.catalog_service import CatalogService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExistingAccount:
    """Represents a matched existing account in a tenant workspace."""
    account_id: int
    key: str
    display_name: Optional[str] = None
    domain: Optional[str] = None
    sec_cik: Optional[str] = None
    canonical_key: Optional[str] = None
    origin_type: Optional[str] = None
    snapshot_id: Optional[int] = None
    snapshot_version: Optional[int] = None
    local_edits: Optional[bool] = False
    match_reason: Optional[str] = None


class AccountMatcher:
    """Unified Duplicate Detection & Key Collision Engine."""

    @classmethod
    def find_existing(
        cls,
        session: Session,
        ctx_or_slug: Union[TenantRequestContext, str],
        payload_or_company: Any,
    ) -> Optional[ExistingAccount]:
        """Finds existing account matching the given company or account payload.
        
        Evaluation Order:
        1. Normalized SEC CIK (zero-padded 10-digits in SQL).
        2. Normalized Domain (matched against domain & primary_domain).
        3. Provenance Canonical Key (account_origin.canonical_key).
        
        Guarantees:
        - Never matches on 'key' alone.
        - Never matches on empty string or NULL values.
        """
        if isinstance(ctx_or_slug, TenantRequestContext):
            target_schema = ctx_or_slug.schema
        else:
            clean_slug = validate_slug(str(ctx_or_slug))
            target_schema = schema_for(clean_slug)

        # 1. Extract candidate identifiers
        raw_cik = None
        raw_domain = None
        canonical_key = None
        display_name = None

        if isinstance(payload_or_company, Company):
            canonical_key = payload_or_company.canonical_key
            display_name = payload_or_company.display_name
            c_ids = session.query(CompanyIdentifier).filter_by(company_id=payload_or_company.id).all()
            for cid in c_ids:
                if cid.id_type == "sec_cik" and not raw_cik:
                    raw_cik = cid.id_value
                elif cid.id_type == "domain" and not raw_domain:
                    raw_domain = cid.id_value
        elif isinstance(payload_or_company, dict):
            raw_cik = payload_or_company.get("sec_cik")
            raw_domain = payload_or_company.get("domain") or payload_or_company.get("primary_domain")
            canonical_key = payload_or_company.get("canonical_key")
            display_name = payload_or_company.get("display_name") or payload_or_company.get("name")
        else:
            raw_cik = getattr(payload_or_company, "sec_cik", None)
            raw_domain = getattr(payload_or_company, "primary_domain", None) or getattr(payload_or_company, "domain", None)
            canonical_key = getattr(payload_or_company, "canonical_key", None)
            display_name = getattr(payload_or_company, "display_name", None)

        # Also extract from canonical_key if available
        if not raw_cik and canonical_key and canonical_key.startswith("cik:"):
            raw_cik = canonical_key.split(":", 1)[-1]
        if not raw_domain and canonical_key and canonical_key.startswith("domain:"):
            raw_domain = canonical_key.split(":", 1)[-1]

        # Normalize identifiers
        norm_cik = CatalogService.normalize_cik(raw_cik) if raw_cik else None
        if not norm_cik and canonical_key and canonical_key.startswith("cik:"):
            norm_cik = CatalogService.normalize_cik(canonical_key.split(":", 1)[-1])

        norm_dom = CatalogService.normalize_domain(raw_domain) if raw_domain else None
        if not norm_dom and canonical_key and canonical_key.startswith("domain:"):
            norm_dom = CatalogService.normalize_domain(canonical_key.split(":", 1)[-1])

        # Step 1: Match by zero-padded 10-digit CIK (both sides normalized in SQL)
        if norm_cik:
            row = session.execute(
                text(f"""
                    SELECT a.id, a.key, a.display_name, a.domain, a.sec_cik,
                           o.canonical_key, o.origin_type, o.snapshot_id, o.snapshot_version, o.local_edits
                    FROM "{target_schema}"."accounts" a
                    LEFT JOIN "{target_schema}"."account_origin" o ON o.account_id = a.id
                    WHERE a.sec_cik IS NOT NULL
                      AND trim(a.sec_cik) <> ''
                      AND lpad(regexp_replace(a.sec_cik, '\\D', '', 'g'), 10, '0') = :cik
                    ORDER BY a.id ASC
                    LIMIT 1
                """),
                {"cik": norm_cik},
            ).fetchone()

            if row:
                return ExistingAccount(
                    account_id=row[0],
                    key=row[1],
                    display_name=row[2],
                    domain=row[3],
                    sec_cik=row[4],
                    canonical_key=row[5],
                    origin_type=row[6],
                    snapshot_id=row[7],
                    snapshot_version=row[8],
                    local_edits=bool(row[9]),
                    match_reason="sec_cik",
                )

        # Step 2: Match by normalized Domain (against domain and primary_domain)
        if norm_dom:
            row = session.execute(
                text(f"""
                    SELECT a.id, a.key, a.display_name, a.domain, a.sec_cik,
                           o.canonical_key, o.origin_type, o.snapshot_id, o.snapshot_version, o.local_edits
                    FROM "{target_schema}"."accounts" a
                    LEFT JOIN "{target_schema}"."account_origin" o ON o.account_id = a.id
                    WHERE (
                        a.domain IS NOT NULL
                        AND trim(a.domain) <> ''
                        AND regexp_replace(lower(regexp_replace(regexp_replace(a.domain, '^https?://', '', 'i'), '^www\\.', '', 'i')), '/.*$', '') = :dom
                    ) OR (
                        a.primary_domain IS NOT NULL
                        AND trim(a.primary_domain) <> ''
                        AND regexp_replace(lower(regexp_replace(regexp_replace(a.primary_domain, '^https?://', '', 'i'), '^www\\.', '', 'i')), '/.*$', '') = :dom
                    )
                    ORDER BY a.id ASC
                    LIMIT 1
                """),
                {"dom": norm_dom},
            ).fetchone()

            if row:
                return ExistingAccount(
                    account_id=row[0],
                    key=row[1],
                    display_name=row[2],
                    domain=row[3],
                    sec_cik=row[4],
                    canonical_key=row[5],
                    origin_type=row[6],
                    snapshot_id=row[7],
                    snapshot_version=row[8],
                    local_edits=bool(row[9]),
                    match_reason="domain",
                )

        # Step 3: Match by Provenance Canonical Key
        if canonical_key and canonical_key.strip():
            clean_ck = canonical_key.strip()
            row = session.execute(
                text(f"""
                    SELECT a.id, a.key, a.display_name, a.domain, a.sec_cik,
                           o.canonical_key, o.origin_type, o.snapshot_id, o.snapshot_version, o.local_edits
                    FROM "{target_schema}"."accounts" a
                    JOIN "{target_schema}"."account_origin" o ON o.account_id = a.id
                    WHERE o.canonical_key IS NOT NULL
                      AND trim(o.canonical_key) <> ''
                      AND o.canonical_key = :ck
                    ORDER BY a.id ASC
                    LIMIT 1
                """),
                {"ck": clean_ck},
            ).fetchone()

            if row:
                return ExistingAccount(
                    account_id=row[0],
                    key=row[1],
                    display_name=row[2],
                    domain=row[3],
                    sec_cik=row[4],
                    canonical_key=row[5],
                    origin_type=row[6],
                    snapshot_id=row[7],
                    snapshot_version=row[8],
                    local_edits=bool(row[9]),
                    match_reason="canonical_key",
                )

        return None

    @classmethod
    def resolve_key_collision(
        cls,
        session: Session,
        target_schema: str,
        desired_key: str,
        canonical_key: str,
    ) -> str:
        """Resolves key collisions with a different company by generating <key>_<short hash>."""
        canonical_hash = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:6]
        candidate_key = desired_key
        counter = 0

        while True:
            exists = session.execute(
                text(f'SELECT 1 FROM "{target_schema}"."accounts" WHERE key = :k LIMIT 1'),
                {"k": candidate_key},
            ).scalar()

            if not exists:
                return candidate_key

            counter += 1
            if counter == 1:
                candidate_key = f"{desired_key}_{canonical_hash}"
            else:
                candidate_key = f"{desired_key}_{canonical_hash}_{counter}"
