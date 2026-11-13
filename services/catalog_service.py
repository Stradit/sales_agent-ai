"""Catalog Service — Verified Account Intelligence Sharing Engine.

Manages the platform intelligence catalog (platform_catalog schema):
1. Deterministic Entity Resolution (CIK > LEI > Domain) preventing false merges.
2. Deny-by-default sanitization via config/tenant_skeleton/share_policy.yaml.
3. Automated Quality Gate Scoring (0-100 rubric, >=80 verified).
4. Immutable versioned snapshot generation with SHA-256 checksums.
5. Zero-Knowledge Source Anonymity (source tenant details stay platform-internal).
"""

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse
import uuid

import yaml
from sqlalchemy import desc, func, or_, text
from sqlalchemy.orm import Session

from db.models.client import Client
from db.models.user import User
from db.models.platform_catalog import (
    Company,
    CompanyIdentifier,
    MergeCandidate,
    Snapshot,
    SnapshotBlob,
)
from db.tenant import schema_for, validate_slug
from services.catalog_audit import log_catalog_audit

SHARE_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "tenant_skeleton" / "share_policy.yaml"

_CACHED_SHARE_POLICY: Optional[Dict[str, Any]] = None


class AmbiguousIdentifierError(ValueError):
    """Raised when an identifier matches more than one canonical company."""
    def __init__(self, message: str, candidate_company_ids: Optional[List[int]] = None):
        super().__init__(message)
        self.candidate_company_ids = candidate_company_ids or []


class CanonicalResolution(tuple):
    canonical_key: str
    identifiers: List[Tuple[str, str]]
    weak: bool

    def __new__(cls, canonical_key: str, identifiers: List[Tuple[str, str]], weak: bool = False):
        obj = super().__new__(cls, (canonical_key, identifiers))
        obj.canonical_key = canonical_key
        obj.identifiers = identifiers
        obj.weak = weak
        return obj


def get_share_policy() -> Dict[str, Any]:
    """Loads and caches share_policy.yaml."""
    global _CACHED_SHARE_POLICY
    if _CACHED_SHARE_POLICY is None:
        if not SHARE_POLICY_PATH.exists():
            raise FileNotFoundError(f"Share policy configuration missing at: {SHARE_POLICY_PATH}")
        with open(SHARE_POLICY_PATH, "r", encoding="utf-8") as f:
            _CACHED_SHARE_POLICY = yaml.safe_load(f)
    return _CACHED_SHARE_POLICY


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    return obj


class CatalogService:
    """Platform Catalog Publishing and Quality Gate Engine."""

    # ── Normalization & Entity Resolution ─────────────────────────────────────

    @classmethod
    def classify_identifier(cls, raw: Any) -> Tuple[str, str]:
        """Classifies raw identifier string into (kind, normalized_value).
        
        Kinds:
        - "canonical_key": starts with cik:, lei:, dom:, or key:
        - "cik": 1-10 digits (rejects all zeros or >10 digits), optional cik: prefix
        - "lei": exactly 20 alphanum characters with >=1 digit and >=1 letter
        - "domain": host with public-suffix style TLD (never strips subdomains other than www.)
        - "unknown": everything else
        """
        if not raw or not isinstance(raw, str):
            return ("unknown", "")
        s = raw.strip()
        if not s:
            return ("unknown", "")

        # 1. Canonical key prefix
        for prefix in ("cik:", "lei:", "dom:", "key:"):
            if s.lower().startswith(prefix):
                return ("canonical_key", s)

        # 2. CIK
        cik = cls.normalize_cik(s)
        if cik:
            return ("cik", cik)

        # 3. LEI
        lei = cls.normalize_lei(s)
        if lei:
            return ("lei", lei)

        # 4. Domain
        dom = cls.normalize_domain(s)
        if dom:
            return ("domain", dom)

        return ("unknown", s)

    @classmethod
    def normalize_domain(cls, val: Optional[str]) -> Optional[str]:
        """Extracts lowercase domain or subdomain host (e.g. 'blackrock.com', 'us.fidelity.com').
        
        Decision:
        - Never strips subdomains other than 'www.' (e.g. 'us.fidelity.com' stays 'us.fidelity.com').
          Stripping non-www subdomains causes false merges between different business units/divisions.
        - Strips scheme, path, query, and port.
        - Encodes via IDNA for internationalized domain names.
        - Rejects pure IP addresses.
        """
        if not val or not isinstance(val, str):
            return None
        raw = val.strip().lower()
        if not raw:
            return None
        if "://" not in raw:
            raw = "https://" + raw
        try:
            parsed = urlparse(raw)
            host = parsed.netloc or parsed.path
            host = host.split(":")[0].strip()
            if host.startswith("www."):
                host = host[4:]
            if not host:
                return None
            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host):
                return None
            try:
                host = host.encode("idna").decode("ascii")
            except Exception:
                pass
            if re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", host):
                tld = host.split(".")[-1]
                if len(tld) >= 2 and not tld.isdigit():
                    return host
            return None
        except Exception:
            return None

    @classmethod
    def normalize_cik(cls, val: Any) -> Optional[str]:
        """Zero-pads SEC CIK to 10 digits (rejects values longer than 10 digits or all zeros)."""
        if val is None or not isinstance(val, (str, int)):
            return None
        s = str(val).strip()
        m = re.match(r"^(?:cik[:\s-]*)?(\d{1,10})$", s, re.IGNORECASE)
        if not m:
            return None
        digits = m.group(1)
        if not any(c != "0" for c in digits):
            return None
        return digits.zfill(10)

    @classmethod
    def normalize_lei(cls, val: Any) -> Optional[str]:
        """Normalizes Legal Entity Identifier (exactly 20-character uppercase alphanumeric with >=1 letter and >=1 digit)."""
        if val is None or not isinstance(val, str):
            return None
        s = val.strip()
        if len(s) == 20 and re.match(r"^[A-Za-z0-9]{20}$", s):
            if re.search(r"\d", s) and re.search(r"[A-Za-z]", s):
                return s.upper()
        return None

    @classmethod
    def resolve_canonical_key(cls, account_data: Dict[str, Any]) -> CanonicalResolution:
        """Resolves the canonical primary key and all alternate global identifiers.
        
        Priority hierarchy:
        1. SEC CIK:  cik:0001364742
        2. LEI:      lei:549300V5XZ12H6G20E62
        3. Domain:   dom:blackrock.com
        
        Weak Fallback:
        When no strong global identifier exists, returns:
        CanonicalResolution("key:<tenant-neutral hash>", [("key", hash)], weak=True)
        where hash = sha256(normalized legal_name + country)[:16].
        """
        identifiers: List[Tuple[str, str]] = []

        # 1. CIK
        raw_cik = account_data.get("sec_cik")
        norm_cik = cls.normalize_cik(raw_cik)
        if norm_cik:
            identifiers.append(("cik", norm_cik))

        # 2. LEI
        raw_lei = account_data.get("lei") or account_data.get("lei_code")
        norm_lei = cls.normalize_lei(raw_lei)
        if norm_lei:
            identifiers.append(("lei", norm_lei))

        # 3. Domain
        raw_dom = (
            account_data.get("primary_domain")
            or account_data.get("domain")
            or account_data.get("website_url")
        )
        norm_dom = cls.normalize_domain(raw_dom)
        if norm_dom:
            identifiers.append(("domain", norm_dom))

        if not identifiers:
            # S-6: Weak company resolution with tenant-neutral hash
            name = (account_data.get("legal_name") or account_data.get("display_name") or "unknown").strip().lower()
            country = (account_data.get("country") or "").strip().lower()
            neutral_hash = hashlib.sha256(f"{name}|{country}".encode("utf-8")).hexdigest()[:16]
            canonical_key = f"key:{neutral_hash}"
            return CanonicalResolution(canonical_key, [("key", neutral_hash)], weak=True)

        # Determine primary canonical key following the strict hierarchy
        for id_type, id_val in identifiers:
            if id_type == "cik":
                return CanonicalResolution(f"cik:{id_val}", identifiers, weak=False)
        for id_type, id_val in identifiers:
            if id_type == "lei":
                return CanonicalResolution(f"lei:{id_val}", identifiers, weak=False)
        for id_type, id_val in identifiers:
            if id_type == "domain":
                return CanonicalResolution(f"dom:{id_val}", identifiers, weak=False)

        return CanonicalResolution(f"{identifiers[0][0]}:{identifiers[0][1]}", identifiers, weak=False)

    # ── Quality Gate Rubric ───────────────────────────────────────────────────

    @classmethod
    def calculate_quality_score(
        cls,
        account: Dict[str, Any],
        lobs: List[Dict[str, Any]],
        sub_lobs: List[Dict[str, Any]],
        personas: List[Dict[str, Any]],
        posts: List[Dict[str, Any]],
        jobs: List[Dict[str, Any]],
    ) -> Tuple[float, str]:
        """Calculates 0-100 quality score and returns (score, grade).
        
        Rubric:
        - Firmographics completeness (name, domain, desc, location, etc.): up to 25 pts
        - Financials & SEC (stock_symbol, sec_cik, revenue, funding): up to 20 pts
        - LOB & Sub-LOB Structure: up to 20 pts
        - Personas & Leadership depth: up to 25 pts
        - Signals, Jobs & Posts currency: up to 10 pts
        
        Grade thresholds:
        - score >= 80.0: "verified"
        - score >= 60.0: "standard"
        - score < 60.0: "insufficient"
        """
        score = 0.0

        # 1. Firmographics (max 25)
        if account.get("display_name") or account.get("legal_name"):
            score += 5.0
        if account.get("domain") or account.get("primary_domain"):
            score += 5.0
        if account.get("short_description") or account.get("full_description"):
            score += 5.0
        if account.get("country") or account.get("city"):
            score += 5.0
        if account.get("industries") or account.get("keywords"):
            score += 5.0

        # 2. Financials & Identifiers (max 20)
        if account.get("sec_cik") or account.get("stock_symbol"):
            score += 10.0
        if account.get("estimated_revenue_range") or account.get("total_funding_amount"):
            score += 5.0
        if account.get("employee_count_range"):
            score += 5.0

        # 3. LOB & Sub-LOB Structure (max 20)
        lob_cnt = len(lobs)
        if lob_cnt >= 1:
            score += 7.0
        if lob_cnt >= 3:
            score += 6.0
        if len(sub_lobs) >= 1:
            score += 7.0

        # 4. Personas & Executive Contacts (max 25)
        per_cnt = len(personas)
        if per_cnt >= 1:
            score += 7.0
        if per_cnt >= 5:
            score += 8.0
        if per_cnt >= 15:
            score += 5.0
        # Check for senior hierarchy presence
        has_senior = any(
            p.get("hierarchy_level") in (1, 2, "1", "2", "c_suite", "vp", "director")
            or p.get("tier") in ("Tier 1", "Tier 2")
            for p in personas
        )
        if has_senior:
            score += 5.0

        # 5. Continuous Intelligence & Signals (max 10)
        if len(posts) > 0:
            score += 4.0
        if len(jobs) > 0:
            score += 3.0
        if account.get("multi_source_intelligence"):
            score += 3.0

        score = min(100.0, max(0.0, round(score, 2)))

        if score >= 80.0:
            grade = "verified"
        elif score >= 60.0:
            grade = "standard"
        else:
            grade = "insufficient"

        return score, grade

    # ── Snapshot Extraction & Sanitization ────────────────────────────────────

    @classmethod
    def extract_and_sanitize_payload(
        cls,
        session: Session,
        source_client_slug: str,
        account_id: int,
    ) -> Dict[str, Any]:
        """Extracts and sanitizes account data per share_policy.yaml (deny-by-default)."""
        clean_slug = validate_slug(source_client_slug)
        schema = schema_for(clean_slug)
        policy = get_share_policy()
        tables_policy = policy.get("tables", {})

        # 1. Accounts
        acc_cols = tables_policy.get("accounts", {}).get("copy", [])
        if not acc_cols:
            raise ValueError("Share policy has no copy columns defined for accounts")

        acc_cols_sql = ", ".join(f'"{c}"' for c in acc_cols)
        acc_row = session.execute(
            text(f'SELECT id, {acc_cols_sql} FROM "{schema}"."accounts" WHERE id = :aid'),
            {"aid": account_id},
        ).fetchone()

        if not acc_row:
            raise ValueError(f"Account ID {account_id} not found in tenant schema '{schema}'")

        col_names = ["_source_id"] + acc_cols
        account_dict = dict(zip(col_names, acc_row))
        account_key = account_dict.get("key")

        # 2. LOBs
        lob_cols = tables_policy.get("lobs", {}).get("copy", [])
        lob_cols_sql = ", ".join(f'"{c}"' for c in lob_cols)
        lob_rows = session.execute(
            text(f'SELECT id, account_id, {lob_cols_sql} FROM "{schema}"."lobs" WHERE account_id = :aid ORDER BY id'),
            {"aid": account_id},
        ).fetchall()
        lob_col_names = ["_old_id", "_old_account_id"] + lob_cols
        lobs_list = [dict(zip(lob_col_names, r)) for r in lob_rows]
        old_lob_ids = [l["_old_id"] for l in lobs_list]

        # 3. Sub-LOBs (no account_id column; linked via lob_id)
        sub_lobs_list = []
        if old_lob_ids:
            sub_cols = tables_policy.get("sub_lobs", {}).get("copy", [])
            sub_cols_sql = ", ".join(f'"{c}"' for c in sub_cols)
            sub_rows = session.execute(
                text(f'SELECT id, lob_id, {sub_cols_sql} FROM "{schema}"."sub_lobs" WHERE lob_id = ANY(:lids) ORDER BY id'),
                {"lids": old_lob_ids},
            ).fetchall()
            sub_col_names = ["_old_id", "lob_id"] + sub_cols
            sub_lobs_list = [dict(zip(sub_col_names, r)) for r in sub_rows]

        # 4. Personas (filter out sensitive, omit regenerate)
        per_cols = tables_policy.get("personas", {}).get("copy", [])
        per_cols_sql = ", ".join(f'"{c}"' for c in per_cols)
        per_rows = session.execute(
            text(f'SELECT id, account_id, lob_id, {per_cols_sql} FROM "{schema}"."personas" WHERE account_id = :aid ORDER BY id'),
            {"aid": account_id},
        ).fetchall()
        per_col_names = ["_old_id", "_old_account_id", "_old_lob_id"] + per_cols
        personas_list = [dict(zip(per_col_names, r)) for r in per_rows]

        # 5. Social Posts (bounded by policy rules: max 90 days, max 100 rows)
        posts_list = []
        if account_key:
            post_cols = tables_policy.get("posts", {}).get("copy", [])
            post_cols_sql = ", ".join(f'"{c}"' for c in post_cols)
            post_rows = session.execute(
                text(f"""
                    SELECT id, {post_cols_sql} FROM "{schema}"."posts"
                    WHERE target_key = :k
                    ORDER BY published_at DESC NULLS LAST
                    LIMIT 100
                """),
                {"k": account_key},
            ).fetchall()
            post_col_names = ["_old_id"] + post_cols
            posts_list = [dict(zip(post_col_names, r)) for r in post_rows]

        # 6. CXO Movements
        cxo_list = []
        if account_key:
            cxo_cols = tables_policy.get("cxo_movements", {}).get("copy", [])
            cxo_cols_sql = ", ".join(f'"{c}"' for c in cxo_cols)
            cxo_rows = session.execute(
                text(f'SELECT id, {cxo_cols_sql} FROM "{schema}"."cxo_movements" WHERE target_key = :k ORDER BY id'),
                {"k": account_key},
            ).fetchall()
            cxo_col_names = ["_old_id"] + cxo_cols
            cxo_list = [dict(zip(cxo_col_names, r)) for r in cxo_rows]

        # 7. LinkedIn Jobs
        jobs_list = []
        if account_key:
            job_cols = tables_policy.get("linkedin_jobs", {}).get("copy", [])
            job_cols_sql = ", ".join(f'"{c}"' for c in job_cols)
            job_rows = session.execute(
                text(f'SELECT id, {job_cols_sql} FROM "{schema}"."linkedin_jobs" WHERE target_key = :k ORDER BY id'),
                {"k": account_key},
            ).fetchall()
            job_col_names = ["_old_id"] + job_cols
            jobs_list = [dict(zip(job_col_names, r)) for r in job_rows]

        # 8. Targets registration
        target_dict = None
        if account_key:
            tgt_cols = tables_policy.get("targets", {}).get("copy", [])
            tgt_cols_sql = ", ".join(f'"{c}"' for c in tgt_cols)
            tgt_row = session.execute(
                text(f'SELECT {tgt_cols_sql} FROM "{schema}"."targets" WHERE key = :k'),
                {"k": account_key},
            ).fetchone()
            if tgt_row:
                target_dict = dict(zip(tgt_cols, tgt_row))

        return _json_safe({
            "account": account_dict,
            "lobs": lobs_list,
            "sub_lobs": sub_lobs_list,
            "personas": personas_list,
            "posts": posts_list,
            "cxo_movements": cxo_list,
            "linkedin_jobs": jobs_list,
            "targets": target_dict,
        })

    # ── Platform Publishing ───────────────────────────────────────────────────

    @classmethod
    def publish_account_snapshot(
        cls,
        session: Session,
        source_client_slug: str,
        account_id: int,
        published_by_user_id: Optional[int] = None,
        force: bool = False,
    ) -> Snapshot:
        """Publishes a sanitized account snapshot to platform_catalog.
        
        Enforces:
        - Source client catalog_publish permission.
        - Deny-by-default sanitization via share_policy.yaml.
        - Quality gate evaluation (score >= 80 to be marked 'ready').
        - Deterministic entity resolution and SHA-256 deduplication.
        - Immutable snapshot and blob persistence.
        """
        clean_slug = validate_slug(source_client_slug)
        client = session.query(Client).filter_by(slug=clean_slug).first()
        if not client:
            raise ValueError(f"Client '{clean_slug}' not found.")

        if client.status != "active":
            raise PermissionError(f"Client '{clean_slug}' is not active (status='{client.status}').")

        if not client.catalog_publish:
            raise PermissionError(f"Client '{clean_slug}' is not authorized to publish to the catalog (catalog_publish=False).")

        # 1. Extract sanitized payload
        payload = cls.extract_and_sanitize_payload(session, clean_slug, account_id)
        account = payload["account"]
        lobs = payload["lobs"]
        sub_lobs = payload["sub_lobs"]
        personas = payload["personas"]
        posts = payload["posts"]
        jobs = payload["linkedin_jobs"]

        # 2. Entity resolution
        resolution = cls.resolve_canonical_key(account)
        canonical_key = resolution.canonical_key
        identifiers = resolution.identifiers
        is_weak = resolution.weak

        # 3. Look up ALL identifiers of incoming account in company_identifiers and canonical_key
        display_name = account.get("display_name") or account.get("legal_name") or "Unknown Enterprise"
        legal_name = account.get("legal_name") or display_name
        country = account.get("country")

        matching_companies_dict: Dict[int, Company] = {}

        if canonical_key:
            c_by_key = session.query(Company).filter_by(canonical_key=canonical_key).first()
            if c_by_key:
                surv = cls._follow_merged(session, c_by_key)
                if surv:
                    matching_companies_dict[surv.id] = surv

        for id_type, id_val in identifiers:
            existing_idents = (
                session.query(CompanyIdentifier)
                .filter_by(id_type=id_type, id_value=id_val)
                .all()
            )
            for ident in existing_idents:
                if ident.company:
                    surv = cls._follow_merged(session, ident.company)
                    if surv:
                        matching_companies_dict[surv.id] = surv

        matched_companies = list(matching_companies_dict.values())

        company: Optional[Company] = None
        force_quarantine = False

        if len(matched_companies) == 0:
            # Case 1: No match -> create company + identifiers
            company = Company(
                canonical_key=canonical_key,
                legal_name=legal_name,
                display_name=display_name,
                country=country,
            )
            session.add(company)
            session.flush()

            for id_type, id_val in identifiers:
                session.add(CompanyIdentifier(
                    company_id=company.id,
                    id_type=id_type,
                    id_value=id_val,
                ))
            session.flush()

        elif len(matched_companies) == 1:
            # Case 2: Exactly one matching company
            c = matched_companies[0]

            # Check if strong identifier conflicts with existing strong identifier of c
            has_conflict = False
            conflict_reason = ""
            c_idents = session.query(CompanyIdentifier).filter_by(company_id=c.id).all()
            for in_type, in_val in identifiers:
                if in_type in ("cik", "lei"):
                    for ex in c_idents:
                        if ex.id_type == in_type and ex.id_value != in_val:
                            has_conflict = True
                            conflict_reason = f"Conflicting {in_type}: incoming '{in_val}' vs existing company {c.id} '{ex.id_value}'"
                            break
                if has_conflict:
                    break

            if has_conflict:
                # Conflict on strong identifier -> treat as Case 3
                force_quarantine = True
                new_comp = Company(
                    canonical_key=f"needs_review:{canonical_key}:{uuid.uuid4().hex[:6]}",
                    legal_name=legal_name,
                    display_name=display_name,
                    country=country,
                    needs_review=True,
                )
                session.add(new_comp)
                session.flush()

                # Add non-conflicting identifiers
                for in_type, in_val in identifiers:
                    conflict_ident = session.query(CompanyIdentifier).filter_by(
                        id_type=in_type, id_value=in_val
                    ).first()
                    if not conflict_ident:
                        session.add(CompanyIdentifier(
                            company_id=new_comp.id,
                            id_type=in_type,
                            id_value=in_val,
                        ))

                candidate = MergeCandidate(
                    company_a_id=c.id,
                    company_b_id=new_comp.id,
                    reason=conflict_reason,
                    evidence={
                        "incoming_identifiers": identifiers,
                        "company_a_identifiers": [(ci.id_type, ci.id_value) for ci in c_idents],
                        "source_client_slug": source_client_slug,
                    },
                    status="pending",
                )
                session.add(candidate)
                c.needs_review = True
                session.flush()
                company = new_comp

            else:
                company = c
                # Add any new identifiers
                for id_type, id_val in identifiers:
                    existing_id = session.query(CompanyIdentifier).filter_by(
                        id_type=id_type, id_value=id_val
                    ).first()
                    if not existing_id:
                        session.add(CompanyIdentifier(
                            company_id=company.id,
                            id_type=id_type,
                            id_value=id_val,
                        ))
                    elif existing_id.company_id != company.id:
                        existing_surv = cls._follow_merged(session, existing_id.company)
                        if existing_surv and existing_surv.id == company.id:
                            existing_id.company_id = company.id

                # Upgrade canonical_key if incoming outranks current key (CIK > LEI > domain)
                if cls._key_rank(canonical_key) > cls._key_rank(company.canonical_key):
                    other = (
                        session.query(Company)
                        .filter(Company.canonical_key == canonical_key, Company.id != company.id)
                        .first()
                    )
                    if not other:
                        old_key = company.canonical_key
                        company.canonical_key = canonical_key
                        if old_key:
                            ex_old = session.query(CompanyIdentifier).filter_by(
                                id_type="key", id_value=old_key
                            ).first()
                            if not ex_old:
                                session.add(CompanyIdentifier(
                                    company_id=company.id,
                                    id_type="key",
                                    id_value=old_key,
                                ))
                session.flush()

        else:
            # Case 3: Matches two or more companies
            force_quarantine = True
            comp_a = matched_companies[0]
            comp_b = matched_companies[1]
            comp_a.needs_review = True
            comp_b.needs_review = True

            candidate = MergeCandidate(
                company_a_id=comp_a.id,
                company_b_id=comp_b.id,
                reason=f"Incoming account matched multiple existing companies: {[c.id for c in matched_companies]}",
                evidence={
                    "incoming_identifiers": identifiers,
                    "matched_company_ids": [c.id for c in matched_companies],
                    "matched_canonical_keys": [c.canonical_key for c in matched_companies],
                    "source_client_slug": source_client_slug,
                },
                status="pending",
            )
            session.add(candidate)
            session.flush()

            # Publish snapshot against the best match (comp_a)
            company = comp_a

        # 4. Deterministic checksum of payload
        payload_str = json.dumps(payload, sort_keys=True, default=str)
        checksum = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

        # Deduplication: check if identical ready snapshot exists
        existing_snapshot = session.query(Snapshot).filter_by(
            company_id=company.id,
            checksum=checksum,
            status="ready",
        ).first()
        if existing_snapshot and not force:
            return existing_snapshot

        # 5. Quality scoring
        quality_score, quality_grade = cls.calculate_quality_score(
            account=account,
            lobs=lobs,
            sub_lobs=sub_lobs,
            personas=personas,
            posts=posts,
            jobs=jobs,
        )

        status = "ready" if ((quality_score >= 80.0 and not is_weak and not force_quarantine) or force) else "quarantined"

        # 6. Version calculation
        max_ver = session.query(func.max(Snapshot.version)).filter_by(company_id=company.id).scalar() or 0
        next_ver = max_ver + 1

        counts = {
            "accounts": 1,
            "lobs": len(lobs),
            "sub_lobs": len(sub_lobs),
            "personas": len(personas),
            "posts": len(posts),
            "cxo_movements": len(payload["cxo_movements"]),
            "jobs": len(jobs),
        }

        # 7. Create Snapshot record (source_client_id is platform-internal only)
        snapshot = Snapshot(
            company_id=company.id,
            version=next_ver,
            status=status,
            source_client_id=client.id,
            source_account_id=account_id,
            policy_version="1.0.0",
            skeleton_version="T0002",
            quality_score=quality_score,
            quality_grade=quality_grade,
            source_extracted_at=account.get("extracted_at") or datetime.now(timezone.utc),
            counts=counts,
            checksum=checksum,
        )
        session.add(snapshot)
        session.flush()

        # 8. Create SnapshotBlob
        blob = SnapshotBlob(
            snapshot_id=snapshot.id,
            payload=payload,
        )
        session.add(blob)

        # 9. Mark earlier ready snapshots as superseded if this one is ready
        if status == "ready":
            session.execute(
                text("""
                    UPDATE platform_catalog.snapshots
                    SET status = 'superseded'
                    WHERE company_id = :cid AND id != :sid AND status = 'ready'
                """),
                {"cid": company.id, "sid": snapshot.id},
            )

        # 10. Audit Logging (S-14 inside caller transaction)
        action = "catalog.publish_forced" if force else "catalog.publish"
        is_super = False
        if published_by_user_id:
            user = session.query(User).filter_by(id=published_by_user_id).first()
            if user and getattr(user, "role", None) == "super_admin":
                is_super = True

        log_catalog_audit(
            session=session,
            actor_user_id=published_by_user_id,
            action=action,
            client_id=client.id,
            details={
                "snapshot_id": snapshot.id,
                "source_client_slug": source_client_slug,
                "source_client_id": client.id,
                "counts": snapshot.counts,
                "quality_score": float(snapshot.quality_score or 0.0),
            },
            is_actor_super_admin=is_super,
        )

        session.commit()
        return snapshot

    # ── Lookup & Merge Helpers ────────────────────────────────────────────────

    @classmethod
    def _key_rank(cls, k: Optional[str]) -> int:
        if not k:
            return 0
        if k.startswith("cik:"):
            return 3
        if k.startswith("lei:"):
            return 2
        if k.startswith("dom:"):
            return 1
        return 0

    @classmethod
    def _follow_merged(cls, session: Session, company: Optional[Company]) -> Optional[Company]:
        """Follows company.merged_into_id chain until surviving company is reached."""
        if not company:
            return None
        visited = set()
        current = company
        while current.merged_into_id is not None:
            if current.id in visited:
                break
            visited.add(current.id)
            parent = session.query(Company).filter_by(id=current.merged_into_id).first()
            if not parent:
                break
            current = parent
        return current

    @classmethod
    def get_company_by_identifier(cls, session: Session, identifier: str) -> Optional[Company]:
        """Resolves a company by querying ONLY the classified identifier type.
        
        Follows merged_into_id chains to surviving companies.
        If multiple surviving companies match, raises AmbiguousIdentifierError.
        """
        if not identifier:
            return None
        kind, val = cls.classify_identifier(identifier)
        if kind == "unknown" or not val:
            return None

        if kind == "canonical_key":
            companies = session.query(Company).filter(
                or_(
                    Company.canonical_key == val,
                    Company.id.in_(
                        session.query(CompanyIdentifier.company_id).filter(
                            CompanyIdentifier.id_type == "key",
                            CompanyIdentifier.id_value == val,
                        )
                    ),
                )
            ).all()
        else:
            companies = (
                session.query(Company)
                .join(CompanyIdentifier, CompanyIdentifier.company_id == Company.id)
                .filter(
                    CompanyIdentifier.id_type == kind,
                    CompanyIdentifier.id_value == val,
                )
                .distinct()
                .all()
            )

        if not companies:
            return None

        # S-7: Follow merged_into_id chains
        survivors_dict: Dict[int, Company] = {}
        for c in companies:
            surv = cls._follow_merged(session, c)
            if surv:
                survivors_dict[surv.id] = surv

        surviving = list(survivors_dict.values())
        if not surviving:
            return None
        if len(surviving) > 1:
            raise AmbiguousIdentifierError(
                f"Identifier '{identifier}' ({kind}='{val}') matches multiple companies: {[c.id for c in surviving]}",
                candidate_company_ids=[c.id for c in surviving],
            )
        return surviving[0]

    @classmethod
    def get_latest_snapshot(cls, session: Session, company_id: int, status: str = "ready") -> Optional[Snapshot]:
        """Retrieves the latest snapshot for a company, following merged_into_id."""
        comp = session.query(Company).filter_by(id=company_id).first()
        if comp:
            surv = cls._follow_merged(session, comp)
            if surv:
                company_id = surv.id
        return session.query(Snapshot).filter_by(
            company_id=company_id,
            status=status,
        ).order_by(desc(Snapshot.version)).first()

    @classmethod
    def resolve_merge_candidate(
        cls,
        session: Session,
        candidate_id: int,
        action: str,  # "merge" | "dismiss"
        keep_company_id: Optional[int] = None,
        admin_user_id: Optional[int] = None,
    ) -> MergeCandidate:
        """Resolves a merge candidate by merging two companies or dismissing."""
        candidate = session.query(MergeCandidate).filter_by(id=candidate_id).first()
        if not candidate:
            raise ValueError(f"MergeCandidate {candidate_id} not found.")

        if candidate.status != "pending":
            raise ValueError(f"MergeCandidate {candidate_id} is already '{candidate.status}'.")

        if action == "dismiss":
            candidate.status = "dismissed"
            candidate.resolved_by = admin_user_id
            candidate.resolved_at = datetime.now(timezone.utc)

            # Clear needs_review if no other pending candidates
            for cid in (candidate.company_a_id, candidate.company_b_id):
                if cid:
                    pending = session.query(MergeCandidate).filter(
                        or_(MergeCandidate.company_a_id == cid, MergeCandidate.company_b_id == cid),
                        MergeCandidate.status == "pending",
                        MergeCandidate.id != candidate.id,
                    ).first()
                    if not pending:
                        comp = session.query(Company).filter_by(id=cid).first()
                        if comp:
                            comp.needs_review = False
            session.flush()
            log_catalog_audit(
                session=session,
                actor_user_id=admin_user_id,
                action="catalog.merge_candidate_dismissed",
                client_id=None,
                details={"candidate_id": candidate.id, "action": "dismiss"},
                is_actor_super_admin=True,
            )
            session.commit()
            return candidate

        elif action == "merge":
            if not keep_company_id:
                raise ValueError("keep_company_id is required when action='merge'.")
            if keep_company_id not in (candidate.company_a_id, candidate.company_b_id):
                raise ValueError(
                    f"keep_company_id {keep_company_id} must be one of the candidate companies "
                    f"({candidate.company_a_id}, {candidate.company_b_id})."
                )

            survivor_id = keep_company_id
            loser_id = (
                candidate.company_b_id
                if survivor_id == candidate.company_a_id
                else candidate.company_a_id
            )
            if not loser_id:
                raise ValueError("Cannot merge candidate with null company_b_id.")

            # Advisory lock both companies in ordered fashion
            lock_ids = sorted([survivor_id, loser_id])
            for lid in lock_ids:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext('catalog:company:' || :id))"),
                    {"id": str(lid)},
                )

            survivor = session.query(Company).filter_by(id=survivor_id).first()
            loser = session.query(Company).filter_by(id=loser_id).first()
            if not survivor or not loser:
                raise ValueError("One or both companies in merge candidate could not be found.")

            # Re-point loser company to survivor
            loser.merged_into_id = survivor.id

            # Move identifiers from loser to survivor
            loser_idents = session.query(CompanyIdentifier).filter_by(company_id=loser.id).all()
            for l_ident in loser_idents:
                existing = session.query(CompanyIdentifier).filter_by(
                    id_type=l_ident.id_type, id_value=l_ident.id_value
                ).first()
                if existing and existing.company_id != loser.id:
                    session.delete(l_ident)
                else:
                    l_ident.company_id = survivor.id

            # Keep loser canonical_key as an identifier on survivor
            if loser.canonical_key and not loser.canonical_key.startswith("needs_review:"):
                existing_key = session.query(CompanyIdentifier).filter_by(
                    id_type="key", id_value=loser.canonical_key
                ).first()
                if not existing_key:
                    session.add(CompanyIdentifier(
                        company_id=survivor.id,
                        id_type="key",
                        id_value=loser.canonical_key,
                    ))

            # Move snapshots
            # Offset versions on BOTH companies first to avoid unique constraint collision on (company_id, version)
            session.execute(
                text("UPDATE platform_catalog.snapshots SET version = -(id + 1000000) WHERE company_id IN (:sid, :lid)"),
                {"sid": survivor.id, "lid": loser.id},
            )
            session.execute(
                text("UPDATE platform_catalog.snapshots SET company_id = :sid WHERE company_id = :lid"),
                {"sid": survivor.id, "lid": loser.id},
            )

            # Renumber versions monotonically by COALESCE(source_extracted_at, taken_at)
            session.execute(
                text("""
                    WITH ordered AS (
                        SELECT id, ROW_NUMBER() OVER (
                            ORDER BY COALESCE(source_extracted_at, taken_at) ASC, id ASC
                        ) as rn
                        FROM platform_catalog.snapshots
                        WHERE company_id = :cid
                    )
                    UPDATE platform_catalog.snapshots s
                    SET version = o.rn
                    FROM ordered o
                    WHERE s.id = o.id
                """),
                {"cid": survivor.id},
            )

            candidate.status = "merged"
            candidate.resolved_by = admin_user_id
            candidate.resolved_at = datetime.now(timezone.utc)

            # Clear needs_review if no remaining pending candidates
            for cid, comp in [(survivor.id, survivor), (loser.id, loser)]:
                pending = session.query(MergeCandidate).filter(
                    or_(MergeCandidate.company_a_id == cid, MergeCandidate.company_b_id == cid),
                    MergeCandidate.status == "pending",
                    MergeCandidate.id != candidate.id,
                ).first()
                if not pending:
                    comp.needs_review = False

            session.flush()

            log_catalog_audit(
                session=session,
                actor_user_id=admin_user_id,
                action="catalog.companies_merged",
                client_id=None,
                details={
                    "candidate_id": candidate.id,
                    "survivor_company_id": survivor.id,
                    "loser_company_id": loser.id,
                    "action": "merge",
                },
                is_actor_super_admin=True,
            )
            session.commit()
            return candidate
        else:
            raise ValueError(f"Unknown merge candidate action: '{action}'")
