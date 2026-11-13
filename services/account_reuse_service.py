"""Account Reuse Service — Atomic Cross-Tenant Hydration & Rollback Engine.

Manages tenant consumption of verified intelligence from platform_catalog:
1. Anti-Oracle Catalog Lookups (sanitized metadata, zero source tenant leakage).
2. Cryptographically signed HMAC preview tokens (15-min expiration).
3. Single-transaction atomic hydration:
   - Transactional advisory locks (pg_advisory_xact_lock) preventing concurrent hydration races.
   - Atomic quota enforcement (CreditService.check_and_enforce_account_quota).
   - Credit fee deduction and immutable ledger logging.
   - Topological relational insertion with ID remapping (accounts, lobs, sub_lobs, personas).
   - Deep traversal and remapping of organisational_hierarchy_tree (Spike S1).
   - Sub-LOB remapping via lob_id (sub_lobs has NO account_id column).
   - AccountOrigin provenance logging.
4. Non-destructive exact rollback:
   - Rollback manifest tracking all created IDs in share_events.
   - Private CRM child detection (deals, action items, copilot sessions) — blocks rollback if private work exists.
   - Clean reversal in reverse topological order.
"""

import base64
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth
import config
from db.column_types import get_column_kinds, get_real_columns, get_table_required_columns
from db.models.client import Client
from db.models.credit_ledger import CreditLedger
from db.models.user import User
from db.models.platform_catalog import (
    AccountOrigin,
    Company,
    CompanyIdentifier,
    ShareEvent,
    Snapshot,
    SnapshotBlob,
)
from db.tenant import schema_for, validate_slug
from services.catalog_audit import log_catalog_audit
from services.catalog_pricing import CatalogPricing
from services.catalog_service import CatalogService, get_share_policy
from services.credit_service import (
    AccountQuotaExceededError,
    CreditService,
    InsufficientCreditsError,
)

logger = logging.getLogger("account_reuse_service")

CATALOG_TOKEN_SECRET = os.getenv("CATALOG_TOKEN_SECRET", "")
if not CATALOG_TOKEN_SECRET:
    if getattr(config, "AUTH_ENFORCED", False) or os.getenv("AUTH_ENFORCED", "").lower() == "true":
        raise RuntimeError("CATALOG_TOKEN_SECRET environment variable is required when AUTH_ENFORCED is true")
    base_secret = getattr(config, "JWT_SECRET_KEY", None) or os.getenv("JWT_SECRET_KEY", "dev-jwt-secret")
    CATALOG_TOKEN_SECRET = hmac.new(
        base_secret.encode("utf-8"),
        b"catalog_preview_token_secret_key",
        hashlib.sha256,
    ).hexdigest()


class AccountReuseError(Exception):
    """Base exception for account reuse failures."""
    pass


class DuplicateAccountError(AccountReuseError):
    """Raised when an account already exists in the target tenant workspace."""
    pass


class IdempotencyConflictError(AccountReuseError):
    """Raised when an idempotency key conflict occurs."""
    pass


class SkeletonIncompatibleError(AccountReuseError):
    """Raised when target tenant schema has required columns that snapshot lacks (S-13)."""
    pass


class RollbackBlockedError(AccountReuseError):
    """Raised when rollback cannot proceed due to private tenant children."""
    def __init__(self, message: str, breakdown: Optional[Dict[str, int]] = None):
        super().__init__(message)
        self.breakdown = breakdown or {}


def _bind_params(row: dict, kinds: dict) -> dict:
    """Prepares bound parameter values according to column kinds.
    
    - json/jsonb column: json.dumps(v) (None stays None)
    - ARRAY column: Python list (coerces JSON strings, tuples, sets to list; None stays None)
    - scalar: passed as is
    """
    params = {}
    for k, v in row.items():
        kind = kinds.get(k, "scalar")
        if kind == "json":
            if v is None:
                params[k] = None
            elif isinstance(v, (dict, list)):
                params[k] = json.dumps(v)
            elif isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    params[k] = json.dumps(parsed)
                except Exception:
                    params[k] = json.dumps(v)
            else:
                params[k] = json.dumps(v)
        elif kind == "array":
            if v is None:
                params[k] = None
            elif isinstance(v, list):
                params[k] = v
            elif isinstance(v, (tuple, set)):
                params[k] = list(v)
            elif isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, (list, tuple)):
                        params[k] = list(parsed)
                    else:
                        params[k] = [parsed]
                except Exception:
                    params[k] = [v]
            else:
                params[k] = [v]
        else:
            params[k] = v
    return params


def _insert_row(
    session: Session,
    schema: str,
    table: str,
    row: dict,
    kinds: dict,
    returning: Optional[str] = "id",
    on_conflict: Optional[str] = None,
) -> Any:
    """Inserts a single row into schema.table with type-safe casts and bound parameters.
    
    Allow-lists columns against actual database schema from information_schema to guarantee
    zero raw unverified identifiers are interpolated into SQL.
    """
    if not row:
        return None

    real_cols = get_real_columns(session, schema, table)
    cols = [c for c in row.keys() if c in real_cols]
    if not cols:
        return None

    cols_str = ", ".join(f'"{c}"' for c in cols)
    val_exprs = []
    filtered_row = {}
    for c in cols:
        filtered_row[c] = row[c]
        k = kinds.get(c, "scalar")
        if k == "json":
            val_exprs.append(f"CAST(:{c} AS jsonb)")
        elif k == "array":
            val_exprs.append(f"CAST(:{c} AS text[])")
        else:
            val_exprs.append(f":{c}")

    vals_str = ", ".join(val_exprs)
    sql = f'INSERT INTO "{schema}"."{table}" ({cols_str}) VALUES ({vals_str})'
    if on_conflict:
        sql += f" ON CONFLICT {on_conflict}"
    if returning:
        sql += f' RETURNING "{returning}"'

    bound = _bind_params(filtered_row, kinds)
    res = session.execute(text(sql), bound)
    if returning:
        return res.scalar()
    return None


def _filter_payload_for_table(
    session: Session,
    schema: str,
    table: str,
    payload: dict,
    policy: dict,
    dropped_tracker: Optional[Dict[str, int]] = None,
) -> dict:
    """Intersects payload keys with target table real columns and policy copy list."""
    real_cols = get_real_columns(session, schema, table)
    policy_copy = set(policy.get("tables", {}).get(table, {}).get("copy", []))
    allowed_keys = real_cols.intersection(policy_copy)

    # Internal metadata and topological foreign keys handled separately during hydration
    skip_keys = {
        "_old_id", "_old_lob_id", "_old_account_id", "_source_id", "lob_id", "account_id"
    }

    clean = {}
    for k, v in payload.items():
        if k.startswith("_") or k in skip_keys:
            continue
        if k not in allowed_keys:
            if k not in real_cols and dropped_tracker is not None:
                tracker_key = f"{table}.{k}"
                dropped_tracker[tracker_key] = dropped_tracker.get(tracker_key, 0) + 1
            logger.warning(
                "Dropping unknown or disallowed column '%s' for table '%s.%s'",
                k, schema, table,
            )
            continue
        clean[k] = v
    return clean


class AccountReuseService:
    """Enterprise Account Intelligence Reuse & Provenance Engine."""

    get_column_kinds = staticmethod(get_column_kinds)

    # ── 1. Anti-Oracle Catalog Lookup ─────────────────────────────────────────

    @classmethod
    def check_reuse_available(
        cls,
        session: Session,
        arg1: Any,
        arg2: Any,
    ) -> Optional[Dict[str, Any]]:
        """Checks if verified intelligence is available for an identifier.
        
        Zero-Knowledge Principle:
        - NEVER exposes source tenant slug or ID.
        - Returns identical None if company does not exist or has no ready snapshot.
        Supports both signatures:
        - Modern: (session, ctx: TenantRequestContext, identifier: str)
        - Legacy: (session, identifier: str, target_client_slug: str)
        """
        from core.tenant_context import TenantRequestContext
        if isinstance(arg1, TenantRequestContext):
            ctx = arg1
            identifier = str(arg2)
        elif isinstance(arg2, TenantRequestContext):
            ctx = arg2
            identifier = str(arg1)
        elif isinstance(arg1, str) and isinstance(arg2, str):
            norm_a = auth.normalize_client_slug(arg1)
            client_a = session.query(Client).filter_by(slug=norm_a).first() if norm_a else None
            if client_a:
                ctx = TenantRequestContext.for_client(session, client_a, user_role="user", effective_role="client_admin")
                identifier = arg2
            else:
                norm_b = auth.normalize_client_slug(arg2)
                client_b = session.query(Client).filter_by(slug=norm_b).first() if norm_b else None
                if client_b:
                    ctx = TenantRequestContext.for_client(session, client_b, user_role="user", effective_role="client_admin")
                    identifier = arg1
                else:
                    clean_slug = validate_slug(arg2)
                    raise ValueError(f"Target client '{clean_slug}' not found.")
        else:
            raise ValueError("Invalid arguments to check_reuse_available")

        if not ctx.client_flags.catalog_consume:
            return None

        company = CatalogService.get_company_by_identifier(session, identifier)
        if not company:
            return None

        # Fetch latest ready snapshot
        snapshot = CatalogService.get_latest_snapshot(session, company.id, status="ready")
        if not snapshot:
            return None

        # Check if target client already has this account
        target_schema = ctx.schema
        clean_slug = ctx.client_slug
        already_exists = False
        existing_acct = session.execute(
            text(f"""
                SELECT id, key FROM "{target_schema}"."accounts"
                WHERE domain = :dom OR key = :k OR sec_cik = :cik
                LIMIT 1
            """),
            {
                "dom": CatalogService.normalize_domain(identifier) or "",
                "k": company.canonical_key.split(":", 1)[-1],
                "cik": CatalogService.normalize_cik(identifier) or "",
            },
        ).fetchone()

        if existing_acct:
            already_exists = True

        # Freshness calculation
        now = datetime.now(timezone.utc)
        taken_at = snapshot.taken_at if snapshot.taken_at.tzinfo else snapshot.taken_at.replace(tzinfo=timezone.utc)
        age_days = max(0, (now - taken_at).days)

        if age_days <= 15:
            freshness = "fresh"
            default_action = "use_verified"
        elif age_days <= 45:
            freshness = "stale"
            default_action = "use_verified_refresh"
        else:
            freshness = "old"
            default_action = "fetch_fresh"

        # S-8: Unified rate card fee and credit savings estimation via CatalogPricing
        counts = snapshot.counts or {}
        estimated_credits_saved = CatalogPricing.estimated_credits_saved(snapshot)
        fee_credits = CatalogPricing.fee_for(
            snapshot=snapshot,
            is_super_admin=ctx.is_super_admin,
        )

        return {
            "available": True,
            "already_exists": already_exists,
            "canonical_key": company.canonical_key,
            "display_name": company.display_name,
            "snapshot_id": snapshot.id,
            "snapshot_version": snapshot.version,
            "taken_at": taken_at.isoformat(),
            "age_days": age_days,
            "freshness": freshness,
            "default_action": default_action,
            "quality_score": float(snapshot.quality_score or 0.0),
            "quality_grade": snapshot.quality_grade,
            "counts": counts,
            "estimated_credits_saved": estimated_credits_saved,
            "fee_credits": fee_credits,
            "requires_approval": ctx.client_flags.reuse_requires_approval,
        }

    # ── 2. Cryptographic Preview Signing ──────────────────────────────────────

    @classmethod
    def generate_preview_token(
        cls,
        snapshot_id: int,
        target_client_slug: str,
        user_id: int,
        expires_in_seconds: int = 900,
    ) -> str:
        """Generates a tamper-proof signed preview token."""
        exp = int(datetime.now(timezone.utc).timestamp()) + expires_in_seconds
        payload = f"{snapshot_id}:{target_client_slug}:{user_id}:{exp}"
        signature = hmac.new(CATALOG_TOKEN_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        token_data = {"payload": payload, "sig": signature}
        return base64.urlsafe_b64encode(json.dumps(token_data).encode("utf-8")).decode("utf-8")

    @classmethod
    def validate_preview_token(
        cls,
        token: str,
        target_client_slug: str,
        snapshot_id: int,
        user_id: int,
    ) -> bool:
        """Validates that a preview token is authentic, untampered, and unexpired."""
        try:
            raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
            data = json.loads(raw)
            payload = data.get("payload", "")
            sig = data.get("sig", "")

            expected_sig = hmac.new(CATALOG_TOKEN_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                return False

            parts = payload.split(":")
            if len(parts) != 4:
                return False

            token_sid, token_slug, token_uid, exp = parts
            if int(token_sid) != snapshot_id:
                return False
            if token_slug != target_client_slug:
                return False
            if int(token_uid) != user_id:
                return False
            if int(exp) < int(datetime.now(timezone.utc).timestamp()):
                return False

            return True
        except Exception:
            return False

    # ── Idempotency Formatting & Validation (S-9) ─────────────────────────────

    IDEMPOTENCY_KEY_REGEX = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")

    @classmethod
    def format_idempotency_key(
        cls,
        key: Optional[str],
        client_id: int,
        snapshot_id: int,
        mode: str,
    ) -> str:
        """Formats and validates an idempotency key (S-9).
        
        Caller-supplied keys must match ^[A-Za-z0-9_\-:.]{8,128}$ and are namespaced as '{client_id}:{key}'.
        Default deterministic key is '{client_id}:{snapshot_id}:{mode}'.
        """
        if key:
            if not cls.IDEMPOTENCY_KEY_REGEX.match(key):
                raise ValueError(
                    f"Invalid idempotency key '{key}'. Must match ^[A-Za-z0-9_\\-:.]{{8,128}}$"
                )
            prefix = f"{client_id}:"
            if not key.startswith(prefix):
                return f"{prefix}{key}"
            return key
        return f"{client_id}:{snapshot_id}:{mode}"

    # ── 3. Relational Remapping Engine ────────────────────────────────────────

    @classmethod
    def _remap_tree_ids(
        cls,
        node: Any,
        lob_id_map: Dict[int, int],
        persona_id_map: Dict[int, int],
    ) -> Any:
        """Recursively walks organisational_hierarchy_tree and translates embedded IDs (Spike S1)."""
        if isinstance(node, dict):
            new_node = {}
            for k, v in node.items():
                if k == "lob_id" and v in lob_id_map:
                    new_node[k] = lob_id_map[v]
                elif k == "persona_id" and v in persona_id_map:
                    new_node[k] = persona_id_map[v]
                else:
                    new_node[k] = cls._remap_tree_ids(v, lob_id_map, persona_id_map)
            return new_node
        elif isinstance(node, list):
            return [cls._remap_tree_ids(item, lob_id_map, persona_id_map) for item in node]
        return node

    # ── 4. Atomic Single-Transaction Hydration ─────────────────────────────────

    @classmethod
    def hydrate_account_from_catalog(
        cls,
        session: Session,
        ctx: Any = None,
        snapshot_id: int = None,
        user_id: int = None,
        idempotency_key: str = None,
        mode: str = "instant",
        fee_credits: int = 1,
        reason: Optional[str] = None,
        approved_by_user_id: Optional[int] = None,
        is_admin_assign: bool = False,
        target_client_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically imports a catalog snapshot into the target tenant workspace.
        
        Non-negotiable guarantees:
        - 100% atomic: executes within a single database transaction.
        - Advisory locks prevent concurrent quota and hydration races.
        - Idempotency key guarantees exactly-once billing and execution.
        - Deep foreign key remapping for lobs, sub_lobs, and personas.
        - Sub-LOBs linked strictly via lob_id (no account_id column).
        - Provenance logged in account_origin.
        - Audit trail recorded in share_events.
        """
        if ctx is None and target_client_slug is not None:
            ctx = target_client_slug
        from core.tenant_context import TenantRequestContext
        if isinstance(ctx, TenantRequestContext):
            clean_slug = ctx.client_slug
            target_schema = ctx.schema
            target_client_id = ctx.client_id
            target_client = session.query(Client).filter_by(id=ctx.client_id).first()
        else:
            clean_slug = validate_slug(str(ctx))
            target_schema = schema_for(clean_slug)
            target_client = session.query(Client).filter_by(slug=clean_slug).first()
            if not target_client:
                raise ValueError(f"Target client '{clean_slug}' not found.")
            target_client_id = target_client.id

        # 1. Verify target client and snapshot
        if target_client.status != "active":
            raise AccountReuseError(f"Target client is in '{target_client.status}' status.")
        if not target_client.catalog_consume and not is_admin_assign:
            raise PermissionError("Catalog reuse is not enabled for this organization")

        snapshot = session.query(Snapshot).filter_by(id=snapshot_id).first()
        if not snapshot:
            raise ValueError(f"Snapshot ID {snapshot_id} not found.")
        if snapshot.status not in ("ready", "superseded"):
            raise AccountReuseError(f"Snapshot {snapshot_id} is in status '{snapshot.status}' and cannot be hydrated.")

        blob = session.query(SnapshotBlob).filter_by(snapshot_id=snapshot.id).first()
        if not blob:
            raise ValueError(f"Snapshot payload blob missing for snapshot {snapshot_id}.")

        company = session.query(Company).filter_by(id=snapshot.company_id).first()
        canonical_key = company.canonical_key if company else f"snap:{snapshot.id}"

        # 2. Transaction-scoped Concurrency Locks (taken BEFORE idempotency lookup)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"catalog:hydrate:{clean_slug}:{canonical_key}"},
        )
        CreditService.acquire_account_quota_lock(session, clean_slug)

        # S-9: Format and namespace idempotency key
        idem_key = cls.format_idempotency_key(idempotency_key, target_client.id, snapshot.id, mode)

        # 3. Idempotency Check
        existing_event = session.query(ShareEvent).filter_by(idempotency_key=idem_key).first()
        if existing_event:
            if existing_event.snapshot_id != snapshot.id or existing_event.target_client_id != target_client.id:
                raise IdempotencyConflictError(
                    f"Idempotency key '{idempotency_key}' reused for different snapshot ({existing_event.snapshot_id} vs {snapshot.id}) or target."
                )
            if existing_event.status == "rolled_back":
                raise IdempotencyConflictError("This operation was previously rolled back and cannot be re-driven.")
            if existing_event.status == "done":
                return {
                    "ok": True,
                    "idempotent": True,
                    "share_event_id": existing_event.id,
                    "account_id": (existing_event.created_ids or {}).get("accounts", [None])[0],
                    "counts": existing_event.counts,
                }
            if existing_event.status == "running" and not (existing_event and approved_by_user_id):
                raise IdempotencyConflictError("Operation is already in progress.")

        # 4. Quota Enforcement
        CreditService.check_and_enforce_account_quota(
            conn=session,
            client_slug=clean_slug,
            schema_name=target_schema,
            max_accounts=target_client.max_accounts,
        )

        # 5. Check for duplicate account in target tenant
        account_payload = blob.payload.get("account", {})
        acct_domain = account_payload.get("domain") or account_payload.get("primary_domain")
        acct_key = account_payload.get("key")
        acct_cik = account_payload.get("sec_cik")

        dup_check = session.execute(
            text(f"""
                SELECT id FROM "{target_schema}"."accounts"
                WHERE (domain = :dom AND :dom != '')
                   OR (key = :k AND :k != '')
                   OR (sec_cik = :cik AND :cik IS NOT NULL AND :cik != '')
                LIMIT 1
            """),
            {"dom": acct_domain or "", "k": acct_key or "", "cik": acct_cik or ""},
        ).fetchone()

        if dup_check:
            raise DuplicateAccountError(f"Account '{acct_key or acct_domain}' already exists in tenant workspace.")

        # S-13: Skeleton version compatibility check before any writes or reservations
        target_skeleton_version = getattr(target_client, "skeleton_version", None) or "T0002"
        snapshot_skeleton_version = getattr(snapshot, "skeleton_version", None) or "T0002"

        tables_to_check = [
            ("accounts", [account_payload], {"id", "owner_user_id", "primary_business_line_id", "is_manually_verified", "manually_verified_at", "api_credits_total", "api_credits_used", "created_at", "updated_at"}),
            ("lobs", blob.payload.get("lobs", []), {"id", "account_id", "is_manually_verified", "manually_verified_at", "created_at", "updated_at"}),
            ("sub_lobs", blob.payload.get("sub_lobs", []), {"id", "lob_id", "is_manually_verified", "manually_verified_at", "created_at", "updated_at"}),
            ("personas", blob.payload.get("personas", []), {"id", "account_id", "lob_id", "created_by_user_id", "is_manually_verified", "manually_verified_at", "created_at", "updated_at"}),
            ("posts", blob.payload.get("posts", []), {"id", "target_key", "new_in_last_run", "created_at", "updated_at"}),
            ("cxo_movements", blob.payload.get("cxo_movements", []), {"id", "target_key", "new_in_last_run", "created_at", "updated_at"}),
            ("linkedin_jobs", blob.payload.get("linkedin_jobs", []), {"id", "target_key", "new_in_last_run", "created_at", "updated_at"}),
        ]
        if blob.payload.get("targets"):
            tables_to_check.append(("targets", [blob.payload["targets"]], {"key", "created_at", "updated_at"}))

        missing_not_null_cols = []
        for tbl, rows_data, injected_cols in tables_to_check:
            if not rows_data:
                continue
            req_cols = get_table_required_columns(session, target_schema, tbl)
            for req_col in req_cols:
                if req_col in injected_cols:
                    continue
                for r in rows_data:
                    if r.get(req_col) is None:
                        missing_not_null_cols.append(f"{tbl}.{req_col}")
                        break

        if missing_not_null_cols:
            raise SkeletonIncompatibleError(
                f"Target tenant requires column(s) without default which snapshot lacks: {', '.join(sorted(set(missing_not_null_cols)))}"
            )

        if target_skeleton_version > snapshot_skeleton_version:
            logger.info(
                "Target client '%s' skeleton version (%s) is newer than snapshot (%s). Using schema defaults for missing columns.",
                clean_slug, target_skeleton_version, snapshot_skeleton_version,
            )

        dropped_columns: Dict[str, int] = {}

        try:
            # S-8: Compute fee via CatalogPricing if not provided
            if fee_credits is None:
                fee_credits = CatalogPricing.fee_for(
                    snapshot=snapshot,
                    client=target_client,
                    mode=mode,
                    is_super_admin=bool(approved_by_user_id and approved_by_user_id != user_id),
                )

            # 6. Create or update ShareEvent
            if existing_event and existing_event.status in ("pending_approval", "failed"):
                share_event = existing_event
                share_event.status = "running"
                share_event.approved_by = approved_by_user_id or user_id
                if reason:
                    share_event.reason = reason
            else:
                share_event = ShareEvent(
                    snapshot_id=snapshot.id,
                    target_client_id=target_client.id,
                    mode=mode,
                    requested_by=user_id,
                    approved_by=approved_by_user_id or user_id,
                    reason=reason or f"Hydrated verified intelligence for {canonical_key}",
                    status="running",
                    idempotency_key=idem_key,
                    fee_credits=fee_credits,
                )
                session.add(share_event)
            session.flush()

            # 7. Credit Fee Billing & Ledger (atomic in-transaction reservation with ref_type and ref_id)
            if fee_credits > 0:
                CreditService.reserve_credits_in_tx(
                    session=session,
                    client_id=target_client.id,
                    amount=fee_credits,
                    ref_type="share_event",
                    ref_id=share_event.id,
                    reason=f"Verified catalog intelligence hydration: {canonical_key}",
                )

            created_ids: Dict[str, List[int]] = {
                "accounts": [],
                "lobs": [],
                "sub_lobs": [],
                "personas": [],
                "posts": [],
                "cxo_movements": [],
                "linkedin_jobs": [],
                "targets": [],
            }
    
            # 8. Relational Insertion with Topological Remapping
            share_policy = get_share_policy()
    
            # 8a. Insert Account
            clean_acct = _filter_payload_for_table(
                session, target_schema, "accounts", account_payload, share_policy, dropped_tracker=dropped_columns
            )
            # Reset tenant-specific CRM/verification flags
            clean_acct["owner_user_id"] = user_id
            clean_acct["primary_business_line_id"] = None
            clean_acct["is_manually_verified"] = False
            clean_acct["manually_verified_at"] = None
            clean_acct["api_credits_total"] = 0
            clean_acct["api_credits_used"] = 0
    
            # Ensure unique target key
            target_acct_key = clean_acct.get("key") or f"acct_{int(datetime.now().timestamp())}"
            key_exists = session.execute(
                text(f'SELECT 1 FROM "{target_schema}"."accounts" WHERE key = :k'),
                {"k": target_acct_key},
            ).scalar()
            if key_exists:
                target_acct_key = f"{target_acct_key}_{target_client.id}"
                clean_acct["key"] = target_acct_key
    
            acct_kinds = get_column_kinds(session, target_schema, "accounts")
            new_account_id = _insert_row(
                session=session,
                schema=target_schema,
                table="accounts",
                row=clean_acct,
                kinds=acct_kinds,
                returning="id",
            )
            created_ids["accounts"].append(new_account_id)
    
            # 8b. Insert LOBs
            lob_kinds = get_column_kinds(session, target_schema, "lobs")
            lob_id_map: Dict[int, int] = {}
            for lob_data in blob.payload.get("lobs", []):
                old_lob_id = lob_data.get("_old_id")
                clean_lob = _filter_payload_for_table(
                    session, target_schema, "lobs", lob_data, share_policy, dropped_tracker=dropped_columns
                )
                clean_lob["account_id"] = new_account_id
                clean_lob["is_manually_verified"] = False
                clean_lob["manually_verified_at"] = None
    
                new_lob_id = _insert_row(
                    session=session,
                    schema=target_schema,
                    table="lobs",
                    row=clean_lob,
                    kinds=lob_kinds,
                    returning="id",
                )
                created_ids["lobs"].append(new_lob_id)
                if old_lob_id:
                    lob_id_map[old_lob_id] = new_lob_id
    
            # 8c. Insert Sub-LOBs (no account_id column; link strictly via lob_id)
            sub_kinds = get_column_kinds(session, target_schema, "sub_lobs")
            for sub_data in blob.payload.get("sub_lobs", []):
                old_lob_ref = sub_data.get("lob_id")
                new_lob_ref = lob_id_map.get(old_lob_ref)
                if not new_lob_ref:
                    continue
    
                clean_sub = _filter_payload_for_table(
                    session, target_schema, "sub_lobs", sub_data, share_policy, dropped_tracker=dropped_columns
                )
                clean_sub["lob_id"] = new_lob_ref
                clean_sub["is_manually_verified"] = False
                clean_sub["manually_verified_at"] = None
    
                new_sub_id = _insert_row(
                    session=session,
                    schema=target_schema,
                    table="sub_lobs",
                    row=clean_sub,
                    kinds=sub_kinds,
                    returning="id",
                )
                created_ids["sub_lobs"].append(new_sub_id)
    
            # 8d. Insert Personas
            per_kinds = get_column_kinds(session, target_schema, "personas")
            persona_id_map: Dict[int, int] = {}
            for per_data in blob.payload.get("personas", []):
                old_per_id = per_data.get("_old_id")
                old_lob_id = per_data.get("_old_lob_id")
                clean_per = _filter_payload_for_table(
                    session, target_schema, "personas", per_data, share_policy, dropped_tracker=dropped_columns
                )
                clean_per["account_id"] = new_account_id
                clean_per["lob_id"] = lob_id_map.get(old_lob_id) if old_lob_id else None
                clean_per["created_by_user_id"] = user_id
                clean_per["is_manually_verified"] = False
                clean_per["manually_verified_at"] = None
    
                new_per_id = _insert_row(
                    session=session,
                    schema=target_schema,
                    table="personas",
                    row=clean_per,
                    kinds=per_kinds,
                    returning="id",
                )
                created_ids["personas"].append(new_per_id)
                if old_per_id:
                    persona_id_map[old_per_id] = new_per_id
    
            # 8e. Remap hierarchical reports_to_persona_id if present
            # 8f. Deep Remap of organisational_hierarchy_tree (Spike S1)
            raw_tree = clean_acct.get("organisational_hierarchy_tree")
            if raw_tree:
                if isinstance(raw_tree, str):
                    try:
                        raw_tree = json.loads(raw_tree)
                    except Exception:
                        raw_tree = None
                if raw_tree:
                    remapped_tree = cls._remap_tree_ids(raw_tree, lob_id_map, persona_id_map)
                    session.execute(
                        text(f'UPDATE "{target_schema}"."accounts" SET organisational_hierarchy_tree = CAST(:tree AS jsonb) WHERE id = :aid'),
                        {"tree": json.dumps(remapped_tree), "aid": new_account_id},
                    )
    
            # 8g. Insert Social Posts, CXO movements, Jobs
            post_kinds = get_column_kinds(session, target_schema, "posts")
            for post_data in blob.payload.get("posts", []):
                clean_post = _filter_payload_for_table(
                    session, target_schema, "posts", post_data, share_policy, dropped_tracker=dropped_columns
                )
                clean_post["target_key"] = target_acct_key
                clean_post["new_in_last_run"] = False
                pid = _insert_row(
                    session=session,
                    schema=target_schema,
                    table="posts",
                    row=clean_post,
                    kinds=post_kinds,
                    returning="id",
                    on_conflict="DO NOTHING",
                )
                if pid:
                    created_ids["posts"].append(pid)
    
            cxo_kinds = get_column_kinds(session, target_schema, "cxo_movements")
            for cxo_data in blob.payload.get("cxo_movements", []):
                clean_cxo = _filter_payload_for_table(
                    session, target_schema, "cxo_movements", cxo_data, share_policy, dropped_tracker=dropped_columns
                )
                clean_cxo["target_key"] = target_acct_key
                clean_cxo["new_in_last_run"] = False
                cid = _insert_row(
                    session=session,
                    schema=target_schema,
                    table="cxo_movements",
                    row=clean_cxo,
                    kinds=cxo_kinds,
                    returning="id",
                    on_conflict="DO NOTHING",
                )
                if cid:
                    created_ids["cxo_movements"].append(cid)
    
            job_kinds = get_column_kinds(session, target_schema, "linkedin_jobs")
            for job_data in blob.payload.get("linkedin_jobs", []):
                clean_job = _filter_payload_for_table(
                    session, target_schema, "linkedin_jobs", job_data, share_policy, dropped_tracker=dropped_columns
                )
                clean_job["target_key"] = target_acct_key
                clean_job["new_in_last_run"] = False
                jid = _insert_row(
                    session=session,
                    schema=target_schema,
                    table="linkedin_jobs",
                    row=clean_job,
                    kinds=job_kinds,
                    returning="id",
                    on_conflict="DO NOTHING",
                )
                if jid:
                    created_ids["linkedin_jobs"].append(jid)
    
            # 8h. Register Target for Continuous Monitoring (Spike S2)
            tgt_data = blob.payload.get("targets")
            if tgt_data:
                clean_tgt = _filter_payload_for_table(
                    session, target_schema, "targets", tgt_data, share_policy, dropped_tracker=dropped_columns
                )
                clean_tgt["key"] = target_acct_key
                target_kinds = get_column_kinds(session, target_schema, "targets")
                _insert_row(
                    session=session,
                    schema=target_schema,
                    table="targets",
                    row=clean_tgt,
                    kinds=target_kinds,
                    returning=None,
                    on_conflict="(key) DO UPDATE SET updated_at = NOW()",
                )
                created_ids["targets"].append(target_acct_key)
    
            # 8i. Provenance: Insert into target account_origin
            session.execute(
                text(f"""
                    INSERT INTO "{target_schema}"."account_origin"
                    (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, hydrated_at, hydrated_by, local_edits)
                    VALUES (:aid, 'catalog', :ck, :sid, :sver, NOW(), :uid, false)
                """),
                {
                    "aid": new_account_id,
                    "ck": canonical_key,
                    "sid": snapshot.id,
                    "sver": snapshot.version,
                    "uid": user_id,
                },
            )
    
            # 9. Update ShareEvent with success status and manifest
            counts_summary = {
                "accounts": len(created_ids["accounts"]),
                "lobs": len(created_ids["lobs"]),
                "sub_lobs": len(created_ids["sub_lobs"]),
                "personas": len(created_ids["personas"]),
                "posts": len(created_ids["posts"]),
                "cxo_movements": len(created_ids["cxo_movements"]),
                "jobs": len(created_ids["linkedin_jobs"]),
                "dropped_columns": dropped_columns,
            }
            share_event.status = "done"
            share_event.completed_at = datetime.now(timezone.utc)
            share_event.created_ids = created_ids
            share_event.counts = counts_summary

            # 10. Audit Logging (S-14 inside caller transaction)
            actor_id = approved_by_user_id or user_id
            actor_user = session.query(User).filter_by(id=actor_id).first() if actor_id else None
            is_actor_super = (actor_user.role == "super_admin") if actor_user else False

            if is_admin_assign and not target_client.catalog_consume:
                log_catalog_audit(
                    session=session,
                    actor_user_id=actor_id,
                    action="catalog.consume_bypassed_by_admin",
                    client_id=target_client.id,
                    details={
                        "snapshot_id": snapshot.id,
                        "target_client_slug": clean_slug,
                        "reason": "Admin assigned account while catalog_consume flag is false",
                    },
                    is_actor_super_admin=True,
                )

            if is_admin_assign:
                audit_action = "catalog.assign"
            elif existing_event and existing_event.status == "running" and approved_by_user_id:
                audit_action = "catalog.request_approved"
            else:
                audit_action = "catalog.hydrate"

            log_catalog_audit(
                session=session,
                actor_user_id=actor_id,
                action=audit_action,
                client_id=target_client.id,
                target_user_id=share_event.requested_by if audit_action == "catalog.request_approved" else None,
                details={
                    "share_event_id": share_event.id,
                    "snapshot_id": snapshot.id,
                    "target_client_slug": clean_slug,
                    "account_id": new_account_id,
                    "fee_credits": fee_credits,
                    "counts": counts_summary,
                    "reason": reason,
                    "dropped_columns": dropped_columns,
                    "skeleton_version": target_skeleton_version,
                },
                is_actor_super_admin=is_actor_super,
            )

            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.query(ShareEvent).filter_by(idempotency_key=idempotency_key).first()
            if existing and existing.status == 'done':
                return {
                    'ok': True,
                    'idempotent': True,
                    'share_event_id': existing.id,
                    'account_id': (existing.created_ids or {}).get('accounts', [None])[0],
                    'counts': existing.counts,
                }
            raise
        except Exception:
            session.rollback()
            raise

        logger.info(
            f"Successfully hydrated snapshot {snapshot.id} for client '{clean_slug}' "
            f"-> Account ID {new_account_id} with {counts_summary['lobs']} LOBs and {counts_summary['personas']} Personas."
        )

        return {
            "ok": True,
            "share_event_id": share_event.id,
            "account_id": new_account_id,
            "account_key": target_acct_key,
            "counts": counts_summary,
            "fee_credits": fee_credits,
            "mode": mode,
            "dropped_columns": dropped_columns,
            "skeleton_version": target_skeleton_version,
        }

    # ── 5. Safe Rollback Execution ────────────────────────────────────────────

    @classmethod
    def rollback_account_reuse(
        cls,
        session: Session,
        share_event_id: int,
        admin_user_id: int,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Safely rolls back a catalog hydration if no private CRM children exist.
        
        Rollback Invariants:
        - 1. Row lock on share_event (SELECT ... FOR UPDATE) and status == 'done' required.
        - 2. Transaction advisory lock taken before inspecting children.
        - 3. Dynamic introspection guard checks all FKs and columns referencing accounts/lobs/sub_lobs/personas.
        - 4. Non-manifest rows in posts/cxo/jobs and tenant-added lobs/personas/sub_lobs block rollback.
        - 5. Reverse topological deletion: posts/cxo/jobs -> targets -> personas -> sub_lobs -> lobs -> account_origin -> account.
        - 6. Delete counts verified against manifest; count mismatch aborts transaction.
        - 7. Refunds fee in credit_ledger and decrements api_credits_used.
        - 8. Records status='rolled_back', rolled_back_at, rolled_back_by, rollback_reason.
        """
        # 1. SELECT ... FOR UPDATE share_event
        share_event = (
            session.query(ShareEvent)
            .filter_by(id=share_event_id)
            .with_for_update()
            .first()
        )
        if not share_event:
            raise ValueError(f"ShareEvent ID {share_event_id} not found.")

        if share_event.status != "done":
            raise AccountReuseError(
                f"ShareEvent {share_event_id} is in status '{share_event.status}' and cannot be rolled back."
            )

        target_client = session.query(Client).filter_by(id=share_event.target_client_id).first()
        if not target_client:
            raise ValueError("Target client not found.")

        clean_slug = validate_slug(target_client.slug)
        target_schema = schema_for(clean_slug)

        # 2. Advisory lock before reading children
        snapshot = session.query(Snapshot).filter_by(id=share_event.snapshot_id).first()
        company = session.query(Company).filter_by(id=snapshot.company_id).first() if snapshot else None
        canonical_key = company.canonical_key if company else f"snap:{share_event.snapshot_id}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"catalog:hydrate:{clean_slug}:{canonical_key}"},
        )

        created_ids = share_event.created_ids or {}
        acct_ids = created_ids.get("accounts", [])
        if not acct_ids:
            raise AccountReuseError("No account ID found in share event manifest.")
        target_account_id = acct_ids[0]

        lob_ids = created_ids.get("lobs", [])
        sub_ids = created_ids.get("sub_lobs", [])
        per_ids = created_ids.get("personas", [])
        post_ids = created_ids.get("posts", [])
        cxo_ids = created_ids.get("cxo_movements", [])
        job_ids = created_ids.get("linkedin_jobs", [])

        # Fetch account key for targets / feed checking
        acct_key_row = session.execute(
            text(f'SELECT key FROM "{target_schema}"."accounts" WHERE id = :aid'),
            {"aid": target_account_id},
        ).fetchone()
        target_acct_key = acct_key_row[0] if acct_key_row else None

        # 3. Dynamic Introspection Guard
        breakdown: Dict[str, int] = {}

        # 3a. Introspect foreign keys
        fk_rows = session.execute(
            text("""
                SELECT
                    tc.table_name,
                    kcu.column_name,
                    ccu.table_name AS foreign_table_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                  AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage AS ccu
                  ON ccu.constraint_name = tc.constraint_name
                  AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = :s
                  AND ccu.table_name IN ('accounts', 'lobs', 'sub_lobs', 'personas')
            """),
            {"s": target_schema},
        ).fetchall()

        # 3b. Introspect columns for unconstrained foreign references
        col_rows = session.execute(
            text("""
                SELECT table_name, column_name,
                       CASE 
                         WHEN column_name = 'account_id' THEN 'accounts'
                         WHEN column_name = 'lob_id' THEN 'lobs'
                         WHEN column_name = 'sub_lob_id' THEN 'sub_lobs'
                         WHEN column_name = 'persona_id' THEN 'personas'
                       END as parent_table
                FROM information_schema.columns
                WHERE table_schema = :s
                  AND column_name IN ('account_id', 'lob_id', 'sub_lob_id', 'persona_id')
            """),
            {"s": target_schema},
        ).fetchall()

        all_refs = set()
        for r in fk_rows:
            all_refs.add((r[0], r[1], r[2]))
        for r in col_rows:
            all_refs.add((r[0], r[1], r[2]))

        # Count referencing rows
        for t_name, c_name, parent_table in sorted(all_refs):
            if t_name in ("accounts", "account_origin", "targets"):
                continue

            if t_name == "lobs":
                if lob_ids:
                    extra = session.execute(
                        text(f'SELECT count(*) FROM "{target_schema}"."lobs" WHERE account_id = :aid AND id != ALL(:lids)'),
                        {"aid": target_account_id, "lids": lob_ids},
                    ).scalar() or 0
                else:
                    extra = session.execute(
                        text(f'SELECT count(*) FROM "{target_schema}"."lobs" WHERE account_id = :aid'),
                        {"aid": target_account_id},
                    ).scalar() or 0
                if extra > 0:
                    breakdown["lobs"] = breakdown.get("lobs", 0) + extra

            elif t_name == "sub_lobs":
                if lob_ids:
                    if sub_ids:
                        extra = session.execute(
                            text(f'SELECT count(*) FROM "{target_schema}"."sub_lobs" WHERE lob_id = ANY(:lids) AND id != ALL(:sids)'),
                            {"lids": lob_ids, "sids": sub_ids},
                        ).scalar() or 0
                    else:
                        extra = session.execute(
                            text(f'SELECT count(*) FROM "{target_schema}"."sub_lobs" WHERE lob_id = ANY(:lids)'),
                            {"lids": lob_ids},
                        ).scalar() or 0
                    if extra > 0:
                        breakdown["sub_lobs"] = breakdown.get("sub_lobs", 0) + extra

            elif t_name == "personas":
                clauses = ["account_id = :aid"]
                params = {"aid": target_account_id}
                if lob_ids:
                    clauses.append("lob_id = ANY(:lids)")
                    params["lids"] = lob_ids
                where_clause = f"({' OR '.join(clauses)})"
                if per_ids:
                    where_clause += " AND id != ALL(:pids)"
                    params["pids"] = per_ids
                extra = session.execute(
                    text(f'SELECT count(*) FROM "{target_schema}"."personas" WHERE {where_clause}'),
                    params,
                ).scalar() or 0
                if extra > 0:
                    breakdown["personas"] = breakdown.get("personas", 0) + extra

            else:
                # Other referencing tables (deals, action_items, user_account_access, copilot_sessions, etc.)
                if parent_table == "accounts":
                    cnt = session.execute(
                        text(f'SELECT count(*) FROM "{target_schema}"."{t_name}" WHERE "{c_name}" = :aid'),
                        {"aid": target_account_id},
                    ).scalar() or 0
                    if cnt > 0:
                        breakdown[t_name] = breakdown.get(t_name, 0) + cnt
                elif parent_table == "lobs" and lob_ids:
                    cnt = session.execute(
                        text(f'SELECT count(*) FROM "{target_schema}"."{t_name}" WHERE "{c_name}" = ANY(:lids)'),
                        {"lids": lob_ids},
                    ).scalar() or 0
                    if cnt > 0:
                        breakdown[t_name] = breakdown.get(t_name, 0) + cnt
                elif parent_table == "sub_lobs" and sub_ids:
                    cnt = session.execute(
                        text(f'SELECT count(*) FROM "{target_schema}"."{t_name}" WHERE "{c_name}" = ANY(:sids)'),
                        {"sids": sub_ids},
                    ).scalar() or 0
                    if cnt > 0:
                        breakdown[t_name] = breakdown.get(t_name, 0) + cnt
                elif parent_table == "personas" and per_ids:
                    cnt = session.execute(
                        text(f'SELECT count(*) FROM "{target_schema}"."{t_name}" WHERE "{c_name}" = ANY(:pids)'),
                        {"pids": per_ids},
                    ).scalar() or 0
                    if cnt > 0:
                        breakdown[t_name] = breakdown.get(t_name, 0) + cnt

        # 3c. Activity links guard
        has_act_links = session.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema = :s AND table_name = 'activity_links'"),
            {"s": target_schema},
        ).scalar()
        if has_act_links:
            clauses = ["(object_type = 'account' AND object_id = :aid)"]
            params = {"aid": target_account_id}
            if lob_ids:
                clauses.append("(object_type = 'lob' AND object_id = ANY(:lids))")
                params["lids"] = lob_ids
            if per_ids:
                clauses.append("(object_type = 'persona' AND object_id = ANY(:pids))")
                params["pids"] = per_ids
            act_cnt = session.execute(
                text(f'SELECT count(*) FROM "{target_schema}"."activity_links" WHERE {" OR ".join(clauses)}'),
                params,
            ).scalar() or 0
            if act_cnt > 0:
                breakdown["activity_links"] = breakdown.get("activity_links", 0) + act_cnt

        # 3d. Check posts, cxo_movements, linkedin_jobs keyed by target_key
        if target_acct_key:
            for feed_table, manifest_items in [
                ("posts", post_ids),
                ("cxo_movements", cxo_ids),
                ("linkedin_jobs", job_ids),
            ]:
                if manifest_items:
                    feed_cnt = session.execute(
                        text(f'SELECT count(*) FROM "{target_schema}"."{feed_table}" WHERE target_key = :k AND id != ALL(:ids)'),
                        {"k": target_acct_key, "ids": manifest_items},
                    ).scalar() or 0
                else:
                    feed_cnt = session.execute(
                        text(f'SELECT count(*) FROM "{target_schema}"."{feed_table}" WHERE target_key = :k'),
                        {"k": target_acct_key},
                    ).scalar() or 0
                if feed_cnt > 0:
                    breakdown[feed_table] = breakdown.get(feed_table, 0) + feed_cnt

        if breakdown:
            raise RollbackBlockedError(
                f"Rollback blocked: account contains private tenant CRM data: {breakdown}",
                breakdown=breakdown,
            )

        # 4. Reverse Topological Deletion
        # 4a. Posts, CXO, Jobs (by manifest ids)
        if post_ids:
            res_del = session.execute(
                text(f'DELETE FROM "{target_schema}"."posts" WHERE id = ANY(:ids) RETURNING id'),
                {"ids": post_ids},
            ).fetchall()
            if len(res_del) != len(post_ids):
                raise AccountReuseError(f"Posts deletion count mismatch: expected {len(post_ids)}, deleted {len(res_del)}")

        if cxo_ids:
            res_del = session.execute(
                text(f'DELETE FROM "{target_schema}"."cxo_movements" WHERE id = ANY(:ids) RETURNING id'),
                {"ids": cxo_ids},
            ).fetchall()
            if len(res_del) != len(cxo_ids):
                raise AccountReuseError(f"CXO movements deletion count mismatch: expected {len(cxo_ids)}, deleted {len(res_del)}")

        if job_ids:
            res_del = session.execute(
                text(f'DELETE FROM "{target_schema}"."linkedin_jobs" WHERE id = ANY(:ids) RETURNING id'),
                {"ids": job_ids},
            ).fetchall()
            if len(res_del) != len(job_ids):
                raise AccountReuseError(f"LinkedIn jobs deletion count mismatch: expected {len(job_ids)}, deleted {len(res_del)}")

        # 4b. Targets row (by key from manifest or account key)
        tgt_keys = created_ids.get("targets", [])
        if not tgt_keys and target_acct_key:
            has_tgt = session.execute(
                text(f'SELECT 1 FROM "{target_schema}"."targets" WHERE key = :k'),
                {"k": target_acct_key},
            ).scalar()
            if has_tgt:
                tgt_keys = [target_acct_key]

        if tgt_keys:
            res_del = session.execute(
                text(f'DELETE FROM "{target_schema}"."targets" WHERE key = ANY(:keys) RETURNING key'),
                {"keys": tgt_keys},
            ).fetchall()
            if len(res_del) != len(tgt_keys):
                raise AccountReuseError(f"Targets deletion count mismatch: expected {len(tgt_keys)}, deleted {len(res_del)}")

        # 4c. Personas
        if per_ids:
            res_del = session.execute(
                text(f'DELETE FROM "{target_schema}"."personas" WHERE id = ANY(:pids) RETURNING id'),
                {"pids": per_ids},
            ).fetchall()
            if len(res_del) != len(per_ids):
                raise AccountReuseError(f"Personas deletion count mismatch: expected {len(per_ids)}, deleted {len(res_del)}")

        # 4d. Sub-LOBs
        if sub_ids:
            res_del = session.execute(
                text(f'DELETE FROM "{target_schema}"."sub_lobs" WHERE id = ANY(:sids) RETURNING id'),
                {"sids": sub_ids},
            ).fetchall()
            if len(res_del) != len(sub_ids):
                raise AccountReuseError(f"Sub-LOBs deletion count mismatch: expected {len(sub_ids)}, deleted {len(res_del)}")

        # 4e. LOBs
        if lob_ids:
            res_del = session.execute(
                text(f'DELETE FROM "{target_schema}"."lobs" WHERE id = ANY(:lids) RETURNING id'),
                {"lids": lob_ids},
            ).fetchall()
            if len(res_del) != len(lob_ids):
                raise AccountReuseError(f"LOBs deletion count mismatch: expected {len(lob_ids)}, deleted {len(res_del)}")

        # 4f. Account Origin
        session.execute(
            text(f'DELETE FROM "{target_schema}"."account_origin" WHERE account_id = :aid'),
            {"aid": target_account_id},
        )

        # 4g. Account
        res_del = session.execute(
            text(f'DELETE FROM "{target_schema}"."accounts" WHERE id = :aid RETURNING id'),
            {"aid": target_account_id},
        ).fetchall()
        if len(res_del) != 1:
            raise AccountReuseError(f"Account deletion count mismatch: expected 1, deleted {len(res_del)}")

        # 5. Credit Refund in Ledger (Phase B S-8 in-transaction refund with ref_type and ref_id)
        if share_event.fee_credits > 0:
            ledger_reason = (
                f"Catalog reuse rollback refund: {reason}"
                if reason
                else f"Catalog reuse rollback refund: Event #{share_event.id}"
            )
            CreditService.refund_credits_in_tx(
                session=session,
                client_id=target_client.id,
                amount=share_event.fee_credits,
                ref_type="share_event",
                ref_id=share_event.id,
                reason=ledger_reason,
            )

        # 6. Mark ShareEvent rolled_back
        share_event.status = "rolled_back"
        share_event.rolled_back_at = datetime.now(timezone.utc)
        share_event.rolled_back_by = admin_user_id
        share_event.rollback_reason = reason

        # 7. Audit Logging (S-14 inside caller transaction)
        log_catalog_audit(
            session=session,
            actor_user_id=admin_user_id,
            action="catalog.rollback",
            client_id=target_client.id,
            details={
                "share_event_id": share_event.id,
                "target_client_slug": clean_slug,
                "fee_credits": share_event.fee_credits,
                "reason": reason,
            },
            is_actor_super_admin=True,
        )

        session.commit()

        return {
            "ok": True,
            "rolled_back": True,
            "share_event_id": share_event.id,
            "account_id": target_account_id,
            "refunded_credits": share_event.fee_credits,
        }
