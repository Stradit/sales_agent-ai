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

import yaml
from sqlalchemy import desc, func, text
from sqlalchemy.orm import Session

from db.models.client import Client
from db.models.user import User
from db.models.platform_catalog import (
    Company,
    CompanyIdentifier,
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

        # 3. Find or create canonical Company in platform_catalog
        company = session.query(Company).filter_by(canonical_key=canonical_key).first()
        display_name = account.get("display_name") or account.get("legal_name") or "Unknown Enterprise"
        legal_name = account.get("legal_name") or display_name
        country = account.get("country")

        if not company:
            company = Company(
                canonical_key=canonical_key,
                legal_name=legal_name,
                display_name=display_name,
                country=country,
            )
            session.add(company)
            session.flush()

        # Update company identifiers
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
        session.flush()

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

        status = "ready" if ((quality_score >= 80.0 and not is_weak) or force) else "quarantined"

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

    # ── Lookup Helpers ────────────────────────────────────────────────────────

    @classmethod
    def get_company_by_identifier(cls, session: Session, identifier: str) -> Optional[Company]:
        """Resolves a company by querying ONLY the classified identifier type.
        
        If multiple companies match, raises AmbiguousIdentifierError.
        """
        if not identifier:
            return None
        kind, val = cls.classify_identifier(identifier)
        if kind == "unknown" or not val:
            return None

        if kind == "canonical_key":
            companies = session.query(Company).filter_by(canonical_key=val).all()
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
        if len(companies) > 1:
            raise AmbiguousIdentifierError(
                f"Identifier '{identifier}' ({kind}='{val}') matches multiple companies: {[c.id for c in companies]}",
                candidate_company_ids=[c.id for c in companies],
            )
        return companies[0]

    @classmethod
    def get_latest_snapshot(cls, session: Session, company_id: int, status: str = "ready") -> Optional[Snapshot]:
        """Retrieves the latest snapshot for a company."""
        return session.query(Snapshot).filter_by(
            company_id=company_id,
            status=status,
        ).order_by(desc(Snapshot.version)).first()
