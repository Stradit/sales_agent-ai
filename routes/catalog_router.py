"""Catalog Router — REST Endpoints for Cross-Tenant Intelligence Sharing.

Provides:
1. Anti-Oracle Intelligence Check (/api/catalog/reuse-check)
2. Atomic Single-Transaction Hydration (/api/catalog/hydrate)
3. Reuse Request Flow (/api/catalog/requests, approve, reject)
4. Super-Admin Platform Catalog Management (/api/catalog/companies, publish, assign, rollback)
"""

from datetime import date, datetime, timezone
import hashlib
import logging
from typing import Any, Dict, List, Literal, Optional
import uuid

import config
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, text
from sqlalchemy.orm import Session

import auth
from core.tenant_context import (
    ClientFlags,
    PlatformRequestContext,
    TenantRequestContext,
    get_platform_db,
    platform_context,
    tenant_context,
)
from db.connection import SessionLocal
from db.models.client import Client
from db.models.platform_catalog import (
    AccountOrigin,
    Company,
    CompanyIdentifier,
    ShareEvent,
    Snapshot,
    SnapshotBlob,
    ReuseRequestNotification,
)
from db.models.user import User
from db.tenant import schema_for, validate_slug
from services.account_reuse_service import (
    AccountMatcher,
    AccountReuseError,
    AccountReuseService,
    DuplicateAccountError,
    IdempotencyConflictError,
    LocalEditsPresentError,
    PlanChangedError,
    RollbackBlockedError,
    SkeletonIncompatibleError,
)
from services.catalog_audit import log_catalog_audit, reuse_check_limiter
from services.catalog_presenter import build_diff_view, build_preview_view, build_request_view
from services.catalog_pricing import CatalogPricing
from services.catalog_service import AmbiguousIdentifierError, CatalogService
from services.credit_service import AccountQuotaExceededError, CreditService, InsufficientCreditsError
from services.notification_service import NotificationService
from services.refresh_planner import RefreshPlanner
import threading

logger = logging.getLogger(__name__)


def _sanitize_error_message(msg: str) -> str:
    """Sanitizes error text to ensure no raw SQL queries or sensitive internals leak."""
    if "[SQL:" in msg:
        msg = msg.split("[SQL:")[0].strip()
    return msg[:500]


def _handle_unexpected_exception(endpoint_name: str, e: Exception, correlation_id: Optional[str] = None) -> HTTPException:
    """Logs unexpected exceptions with correlation ID and returns a safe sanitized response."""
    corr_id = correlation_id or uuid.uuid4().hex[:12]
    logger.error(f"[{endpoint_name}] Unexpected error (correlation_id={corr_id}): {e}", exc_info=True)
    return HTTPException(
        status_code=500,
        detail={"detail": f"{endpoint_name} failed", "correlation_id": corr_id},
        headers={"X-Correlation-ID": corr_id},
    )


catalog_router = APIRouter(prefix="/api/catalog", tags=["Verified Intelligence Catalog"])
accounts_reuse_router = APIRouter(prefix="/api/accounts", tags=["Account Reuse"])
catalog_notifications_router = APIRouter(prefix="/api/notifications/catalog", tags=["Catalog Notifications"])


# ── Pydantic Request & Response Schemas ──────────────────────────────────────

class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"] = Field(..., description="Decision action: 'approve' or 'reject'")
    note: Optional[str] = Field(None, description="Optional decision explanation")
    fee_override: Optional[int] = Field(None, description="Optional fee override in credits")
    use_latest_version: bool = Field(False, description="Whether to approve newer ready version if snapshot superseded")


class BulkDecisionItem(BaseModel):
    id: int = Field(..., description="ShareEvent request ID")
    decision: Literal["approve", "reject"] = Field(..., description="Decision action: 'approve' or 'reject'")
    note: Optional[str] = Field(None, description="Optional decision explanation")
    fee_override: Optional[int] = Field(None, description="Optional fee override")


class BulkDecisionRequest(BaseModel):
    decisions: List[BulkDecisionItem] = Field(..., description="List of decisions (max 50)")
    use_latest_version: bool = Field(False, description="Whether to approve newer ready version if snapshot superseded")

class HydrateRequest(BaseModel):
    snapshot_id: int = Field(..., description="ID of catalog snapshot to hydrate")
    preview_token: str = Field(..., description="HMAC-signed preview token from reuse-check")
    mode: str = Field("instant", description="instant | refresh")
    idempotency_key: Optional[str] = Field(None, description="Optional caller idempotency key")
    client_slug: Optional[str] = Field(None, description="Target client slug override (super_admin only)")


class RefreshRequest(BaseModel):
    snapshot_id: Optional[int] = Field(None, description="Optional snapshot ID to refresh to; defaults to latest ready")
    idempotency_key: Optional[str] = Field(None, description="Optional caller idempotency key")
    client_slug: Optional[str] = Field(None, description="Target client slug override (super_admin only)")


class RefreshApplyRequest(BaseModel):
    snapshot_id: int = Field(..., description="Snapshot ID to refresh to")
    preview_token: str = Field(..., description="Cryptographically signed preview token carrying plan_hash")
    strategy: str = Field("apply_non_edited", description="Refresh strategy: apply_non_edited or apply_all_overwrite")
    confirm_overwrite: Optional[bool] = Field(False, description="Must be true if strategy is apply_all_overwrite")
    reason: Optional[str] = Field(None, description="Audit justification for overwriting local edits")
    idempotency_key: Optional[str] = Field(None, description="Client idempotency key")


class ReuseRequestCreate(BaseModel):
    snapshot_id: int = Field(..., description="ID of catalog snapshot requested")
    preview_token: str = Field(..., description="HMAC-signed preview token from reuse-check")
    mode: str = Field("instant", description="instant | refresh")
    reason: Optional[str] = Field(None, description="Business justification for reuse")
    client_slug: Optional[str] = Field(None, description="Target client slug override")


class PublishRequest(BaseModel):
    source_client_slug: str = Field(..., description="Client slug owning the account")
    account_id: int = Field(..., description="Account ID in source schema")
    force: bool = Field(False, description="Force publish even if quality score is borderline")


class AssignRequest(BaseModel):
    snapshot_id: int = Field(..., description="ID of snapshot to assign")
    target_client_slug: str = Field(..., description="Target client slug")
    mode: str = Field("instant", description="instant | refresh")
    reason: Optional[str] = Field(None, description="Administrative reason")


class RejectRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Rejection justification")


class RollbackRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Rollback justification")


class FetchFreshRequest(BaseModel):
    company_name: str = Field(..., description="Enterprise company name")
    domain: Optional[str] = Field(None, description="Primary domain")
    stock_symbol: Optional[str] = Field(None, description="Stock ticker symbol")
    sec_cik: Optional[str] = Field(None, description="SEC CIK")
    industry: Optional[str] = Field(None, description="Industry sector")
    headquarters_location: Optional[str] = Field(None, description="HQ location")
    company_type: Optional[str] = Field(None, description="Company type")
    declined_snapshot_id: Optional[int] = Field(None, description="Snapshot ID declined in favor of fresh fetch")
    reason: Optional[str] = Field(None, description="Reason for fresh fetch")
    client_slug: Optional[str] = Field(None, description="Target client slug override")


# ── Tenant-Facing Endpoints ──────────────────────────────────────────────────

@catalog_router.get("/reuse-check")
def reuse_check(
    identifier: str = Query(..., min_length=2, description="Domain, CIK, LEI, or company key"),
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_check", explicit_slug_from="query")),
    session: Session = Depends(get_platform_db),
):
    """Anti-Oracle verified intelligence lookup.
    
    Zero-Knowledge Principle:
    - NEVER reveals source client identity or presence of other clients.
    - Returns identical 'available: false' for unknown companies or non-ready snapshots.
    """
    rate_limit_key = f"{ctx.user_id}:{ctx.client_slug}"
    if not reuse_check_limiter.check_and_record(rate_limit_key):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: 30 calls per 5 minutes",
        )

    try:
        result = AccountReuseService.check_reuse_available(session, ctx, identifier)
        if not result or not result.get("available"):
            return {"available": False}

        ident_hash = hashlib.sha256(identifier.strip().lower().encode("utf-8")).hexdigest()
        log_catalog_audit(
            session=session,
            actor_user_id=ctx.user_id,
            action="catalog.reuse_check_available",
            client_id=ctx.client_id,
            details={
                "identifier_hash": ident_hash,
                "target_client_slug": ctx.client_slug,
                "target_client_id": ctx.client_id,
                "snapshot_id": result.get("snapshot_id"),
                "correlation_id": ctx.correlation_id,
            },
            is_actor_super_admin=ctx.is_super_admin,
        )
        session.commit()

        token = AccountReuseService.generate_preview_token(
            snapshot_id=result["snapshot_id"],
            target_client_slug=ctx.client_slug,
            user_id=ctx.user_id,
        )
        result["preview_token"] = token
        return result

    except AmbiguousIdentifierError as e:
        logger.warning(f"[reuse_check] Ambiguous identifier '{identifier}': {e}")
        ident_hash = hashlib.sha256(identifier.strip().lower().encode("utf-8")).hexdigest()
        log_catalog_audit(
            session=session,
            actor_user_id=ctx.user_id,
            action="catalog.identifier_ambiguous",
            client_id=ctx.client_id,
            details={
                "identifier_hash": ident_hash,
                "target_client_slug": ctx.client_slug,
                "target_client_id": ctx.client_id,
                "correlation_id": ctx.correlation_id,
            },
            is_actor_super_admin=ctx.is_super_admin,
        )
        session.commit()
        return {"available": False}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise _handle_unexpected_exception("Reuse check", e, ctx.correlation_id)


@catalog_router.get("/preview")
def get_catalog_preview(
    company_name: Optional[str] = Query(None, description="Company name"),
    domain: Optional[str] = Query(None, description="Company domain"),
    identifier: Optional[str] = Query(None, description="Domain, CIK, or canonical key"),
    client_slug: Optional[str] = Query(None, description="Explicit target client slug"),
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_check", explicit_slug_from="query")),
    session: Session = Depends(get_platform_db),
):
    """Returns a rich, tenant-safe PreviewView for verified catalog intelligence.

    Strict Anti-Oracle isolation:
    - Never reveals source tenant identity, other clients, or tenant counts.
    - Emits standardized typed dictionary conformant to Phase C specification.
    """
    ident = (identifier or domain or company_name or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="company_name, domain, or identifier is required")

    rate_limit_key = f"{ctx.user_id}:{ctx.client_slug}"
    if not reuse_check_limiter.check_and_record(rate_limit_key):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: 30 calls per 5 minutes",
        )

    try:
        result = AccountReuseService.check_reuse_available(session, ctx, ident)
        if not result or not result.get("available"):
            return {
                "match_found": False,
                "available": False,
                "company_name": company_name or ident,
                "message": "No verified catalog intelligence available for this company.",
            }

        snapshot_id = result.get("snapshot_id")
        snapshot = session.query(Snapshot).filter_by(id=snapshot_id).first()
        if not snapshot:
            return {
                "match_found": False,
                "available": False,
                "company_name": company_name or ident,
            }

        company = session.query(Company).filter_by(id=snapshot.company_id).first()

        # Generate HMAC-signed preview token
        token = AccountReuseService.generate_preview_token(
            snapshot_id=snapshot.id,
            target_client_slug=ctx.client_slug,
            user_id=ctx.user_id,
        )

        client_obj = session.query(Client).filter_by(id=ctx.client_id).first()
        client_balance = max(0, (client_obj.api_credits_total or 0) - (client_obj.api_credits_used or 0)) if client_obj else 0
        fee_credits = result.get("fee_credits", 0)
        estimated_credits_saved = result.get("estimated_credits_saved", 85)
        fetch_fresh_estimate_credits = 100
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        expires_at = now + timedelta(minutes=15)

        preview = build_preview_view(
            snapshot=snapshot,
            company=company,
            quality_score=float(result.get("quality_score", 85.0)),
            quality_grade=result.get("quality_grade", "A"),
            counts=result.get("counts", {}),
            already_exists=result.get("already_exists", False),
            existing_account_id=result.get("existing_account_id"),
            refresh_available=result.get("refresh_available", False),
            fee_credits=fee_credits,
            estimated_credits_saved=estimated_credits_saved,
            fetch_fresh_estimate_credits=fetch_fresh_estimate_credits,
            client_balance=client_balance,
            requires_approval=client_obj.reuse_requires_approval if client_obj else result.get("requires_approval", True),
            preview_token=token,
            expires_at=expires_at,
        )

        # Audit view event
        ident_hash = hashlib.sha256(ident.lower().encode("utf-8")).hexdigest()
        log_catalog_audit(
            session=session,
            actor_user_id=ctx.user_id,
            action="catalog.preview_viewed",
            client_id=ctx.client_id,
            details={
                "identifier_hash": ident_hash,
                "target_client_slug": ctx.client_slug,
                "target_client_id": ctx.client_id,
                "snapshot_id": snapshot.id,
                "correlation_id": ctx.correlation_id,
            },
            is_actor_super_admin=ctx.is_super_admin,
        )
        session.commit()

        return {
            "match_found": True,
            "available": True,
            **preview,
        }

    except AmbiguousIdentifierError as e:
        logger.warning(f"[preview] Ambiguous identifier '{ident}': {e}")
        return {
            "match_found": False,
            "available": False,
            "company_name": company_name or ident,
            "message": "Multiple matches found in catalog.",
        }
    except Exception as e:
        raise _handle_unexpected_exception("Catalog preview", e, ctx.correlation_id)


@catalog_router.post("/fetch-fresh")
def fetch_fresh_account(
    req: FetchFreshRequest,
    ctx: TenantRequestContext = Depends(tenant_context("account:write", explicit_slug_from="body")),
    session: Session = Depends(get_platform_db),
):
    """Enqueues fresh intelligence harvesting for a company, declining catalog reuse if specified."""
    clean_name = req.company_name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="company_name is required")

    clean_dom = (
        req.domain.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0].strip().lower()
        if req.domain
        else None
    )
    if clean_dom and clean_dom.startswith("www."):
        clean_dom = clean_dom[4:]

    # Anti-collision check in target workspace
    company_stub = type("CompanyStub", (), {
        "canonical_key": clean_dom or clean_name.lower().replace(" ", "_"),
        "display_name": clean_name,
        "legal_name": clean_name,
        "id": None,
    })()
    existing = AccountMatcher.find_existing(session, ctx, company_stub)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Account '{clean_name}' already exists in workspace '{ctx.client_slug}'.",
        )

    # Log audit event when catalog reuse is declined in favor of fresh fetch
    if req.declined_snapshot_id:
        log_catalog_audit(
            session=session,
            actor_user_id=ctx.user_id,
            action="catalog.fetch_fresh_chosen",
            client_id=ctx.client_id,
            details={
                "declined_snapshot_id": req.declined_snapshot_id,
                "company_name": clean_name,
                "target_client_slug": ctx.client_slug,
                "reason": req.reason,
                "correlation_id": ctx.correlation_id,
            },
            is_actor_super_admin=ctx.is_super_admin,
        )
        session.commit()

    from services.account_automation_engine import AccountAutomationEngine
    from services.job_registry import (
        create_job as _jreg_create,
        update_job as _jreg_update,
        STATUS_FAILED,
        TYPE_ACCOUNT,
    )
    from db.tenant import tenant_scope

    preflight = AccountAutomationEngine.preflight_account_check(clean_name, clean_dom, client_slug=ctx.client_slug)
    if not preflight.get("allowed"):
        raise HTTPException(
            status_code=402,
            detail=preflight.get("message", "Insufficient credits for autonomous pipeline enrichment."),
        )

    job_id = _jreg_create(
        TYPE_ACCOUNT,
        meta={
            "company_name": clean_name,
            "target_url": clean_dom,
            "mode": "fetch_fresh_pipeline",
            "declined_snapshot_id": req.declined_snapshot_id,
        },
        client_slug=ctx.client_slug,
    )

    def _bg_worker():
        try:
            with tenant_scope(ctx.client_slug):
                AccountAutomationEngine.execute_account_automation(
                    company_name=clean_name,
                    domain=clean_dom,
                    stock_symbol=req.stock_symbol,
                    sec_cik=req.sec_cik,
                    industry=req.industry,
                    headquarters_location=req.headquarters_location,
                    company_type=req.company_type,
                    job_id=job_id,
                    client_slug=ctx.client_slug,
                )
        except Exception as exc:
            _jreg_update(job_id, status=STATUS_FAILED, error=str(exc))

    threading.Thread(target=_bg_worker, daemon=True).start()

    return {
        "status": "queued",
        "job_id": job_id,
        "company_name": clean_name,
        "domain": clean_dom,
        "client_slug": ctx.client_slug,
        "message": "Fresh intelligence pipeline initiated in background.",
    }


@catalog_router.post("/hydrate")
def hydrate_account(
    req: HydrateRequest,
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_execute", explicit_slug_from="body")),
    session: Session = Depends(get_platform_db),
):
    """Atomically imports verified intelligence into the target client workspace."""
    # 1. Validate preview token
    if not AccountReuseService.validate_preview_token(
        req.preview_token, ctx.client_slug, req.snapshot_id, ctx.user_id
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired preview token. Please re-run the intelligence check.",
        )

    # 2. Check approval requirement
    if not ctx.client_flags.catalog_consume:
        raise HTTPException(
            status_code=403,
            detail="Catalog reuse is not enabled for this organization",
        )

    if ctx.client_flags.reuse_requires_approval and not ctx.is_super_admin:
        raise HTTPException(
            status_code=403,
            detail="This organization requires administrator approval to reuse catalog intelligence. "
                   "Please submit a reuse request instead.",
        )

    try:
        fee = CatalogPricing.fee_for(mode=req.mode, is_super_admin=ctx.is_super_admin)
        hydration_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=ctx,
            snapshot_id=req.snapshot_id,
            user_id=ctx.user_id,
            idempotency_key=req.idempotency_key,
            mode=req.mode,
            fee_credits=fee,
            approved_by_user_id=ctx.user_id if ctx.is_super_admin else None,
        )
        return hydration_res

    except LocalEditsPresentError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "local_edits_present", "message": str(e), "diff": e.diff},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except InsufficientCreditsError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except AccountQuotaExceededError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except IdempotencyConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "idempotency_key_reuse", "message": str(e)},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except SkeletonIncompatibleError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "skeleton_incompatible", "message": str(e)},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except DuplicateAccountError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404 if "not found" in str(e).lower() else 400, detail=str(e))
    except Exception as e:
        raise _handle_unexpected_exception("Hydration", e, ctx.correlation_id)


@catalog_router.post("/accounts/{company_id}/refresh")
def refresh_account(
    company_id: int,
    req: Optional[RefreshRequest] = None,
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_execute", explicit_slug_from="body")),
    session: Session = Depends(get_platform_db),
):
    """Refreshes an existing imported catalog account with newer intelligence (B9)."""
    if req is None:
        req = RefreshRequest()

    if req.snapshot_id:
        snapshot = session.query(Snapshot).filter_by(id=req.snapshot_id, company_id=company_id).first()
    else:
        snapshot = CatalogService.get_latest_snapshot(session, company_id, status="ready")

    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail=f"No ready snapshot found for company {company_id}",
            headers={"X-Correlation-ID": ctx.correlation_id},
        )

    try:
        fee = CatalogPricing.fee_for(snapshot=snapshot, mode="refresh", is_super_admin=ctx.is_super_admin)
        res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=ctx,
            snapshot_id=snapshot.id,
            user_id=ctx.user_id,
            idempotency_key=req.idempotency_key,
            mode="refresh",
            fee_credits=fee,
        )
        return res
    except LocalEditsPresentError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "local_edits_present", "message": str(e), "diff": e.diff},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except InsufficientCreditsError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except IdempotencyConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "idempotency_key_reuse", "message": str(e)},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except SkeletonIncompatibleError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "skeleton_incompatible", "message": str(e)},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except DuplicateAccountError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404 if "not found" in str(e).lower() else 400, detail=str(e))
    except Exception as e:
        raise _handle_unexpected_exception("Account refresh", e, ctx.correlation_id)


@accounts_reuse_router.get("/{account_id}/reuse/refresh-preview")
@catalog_router.get("/accounts/{account_id}/reuse/refresh-preview")
def refresh_preview(
    account_id: int,
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_check")),
    session: Session = Depends(get_platform_db),
):
    """Computes real-time DiffView between local tenant account and latest catalog snapshot (C2)."""
    clean_slug = validate_slug(ctx.client_slug)
    target_schema = schema_for(clean_slug)

    local_acct_row = session.execute(
        text(f'SELECT * FROM "{target_schema}"."accounts" WHERE id = :id'),
        {"id": account_id},
    ).mappings().first()
    if not local_acct_row:
        raise HTTPException(
            status_code=404,
            detail=f"Account {account_id} not found in workspace.",
            headers={"X-Correlation-ID": ctx.correlation_id},
        )

    local_origin = session.execute(
        text(f'SELECT * FROM "{target_schema}"."account_origin" WHERE account_id = :id'),
        {"id": account_id},
    ).mappings().first()
    if not local_origin or not local_origin.get("canonical_key"):
        raise HTTPException(
            status_code=400,
            detail=f"Account {account_id} was not imported from platform catalog.",
            headers={"X-Correlation-ID": ctx.correlation_id},
        )

    company = session.query(Company).filter_by(canonical_key=local_origin["canonical_key"]).first()
    if not company:
        raise HTTPException(
            status_code=404,
            detail="Catalog company record not found.",
            headers={"X-Correlation-ID": ctx.correlation_id},
        )

    snapshot = CatalogService.get_latest_snapshot(session, company.id, status="ready")
    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail="No ready snapshot found for company.",
            headers={"X-Correlation-ID": ctx.correlation_id},
        )

    blob = session.query(SnapshotBlob).filter_by(snapshot_id=snapshot.id).first()
    if not blob:
        raise HTTPException(
            status_code=404,
            detail="Snapshot blob payload not found.",
            headers={"X-Correlation-ID": ctx.correlation_id},
        )

    plan = RefreshPlanner.plan(
        session=session,
        schema=target_schema,
        account_id=account_id,
        snapshot=snapshot,
        blob_payload=blob.payload,
        is_super_admin=ctx.is_super_admin,
    )

    token = AccountReuseService.generate_preview_token(
        snapshot_id=snapshot.id,
        target_client_slug=clean_slug,
        user_id=ctx.user_id,
        plan_hash=plan["plan_hash"],
    )

    return build_diff_view(
        **plan,
        preview_token=token,
        snapshot_id=snapshot.id,
    )


@accounts_reuse_router.post("/{account_id}/reuse/refresh")
@catalog_router.post("/accounts/{account_id}/reuse/refresh")
def apply_refresh(
    account_id: int,
    req: RefreshApplyRequest,
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_execute")),
    session: Session = Depends(get_platform_db),
):
    """Applies catalog refresh to an account with local-edit protection or explicit overwrite (C2)."""
    try:
        res = AccountReuseService.refresh_account_from_catalog(
            session=session,
            ctx=ctx,
            account_id=account_id,
            snapshot_id=req.snapshot_id,
            preview_token=req.preview_token,
            strategy=req.strategy,
            confirm_overwrite=bool(req.confirm_overwrite),
            reason=req.reason,
            idempotency_key=req.idempotency_key,
        )
        return res
    except PlanChangedError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "plan_changed", "message": str(e), "plan_hash": e.plan_hash},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e), headers={"X-Correlation-ID": ctx.correlation_id})
    except InsufficientCreditsError as e:
        raise HTTPException(status_code=402, detail=str(e), headers={"X-Correlation-ID": ctx.correlation_id})
    except IdempotencyConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "idempotency_key_reuse", "message": str(e)},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e), headers={"X-Correlation-ID": ctx.correlation_id})
    except ValueError as e:
        raise HTTPException(status_code=404 if "not found" in str(e).lower() else 400, detail=str(e), headers={"X-Correlation-ID": ctx.correlation_id})
    except Exception as e:
        raise _handle_unexpected_exception("Refresh apply", e, ctx.correlation_id)


@catalog_router.post("/requests")
def submit_reuse_request(
    req: ReuseRequestCreate,
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_execute", explicit_slug_from="body")),
    session: Session = Depends(get_platform_db),
):
    """Submits a reuse request for super admin approval when reuse_requires_approval is true."""
    if not AccountReuseService.validate_preview_token(
        req.preview_token, ctx.client_slug, req.snapshot_id, ctx.user_id
    ):
        raise HTTPException(status_code=400, detail="Invalid or expired preview token.")

    if not ctx.client_flags.catalog_consume:
        raise HTTPException(
            status_code=403,
            detail="Catalog reuse is not enabled for this organization",
        )

    snapshot = session.query(Snapshot).filter_by(id=req.snapshot_id).first()
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Snapshot {req.snapshot_id} not found")

    company = session.query(Company).filter_by(id=snapshot.company_id).first()
    comp_name = company.display_name if company else "Company"

    existing_req = session.query(ShareEvent).filter_by(
        snapshot_id=req.snapshot_id,
        target_client_id=ctx.client_id,
        status="pending_approval",
    ).first()
    if existing_req:
        return build_request_view(
            event=existing_req,
            company_name=comp_name,
            snapshot_version=snapshot.version,
            canonical_key=company.canonical_key if company else None,
            sla_hours=config.CATALOG_APPROVAL_SLA_HOURS,
            already_pending=True,
            is_super_admin=ctx.is_super_admin,
        )

    fee = CatalogPricing.fee_for(snapshot=snapshot, mode=req.mode or "instant", is_super_admin=ctx.is_super_admin)
    idem_key = f"{ctx.client_id}:{req.snapshot_id}:request:{uuid.uuid4().hex[:12]}"
    share_event = ShareEvent(
        snapshot_id=req.snapshot_id,
        target_client_id=ctx.client_id,
        mode=req.mode or "instant",
        requested_by=ctx.user_id,
        reason=req.reason or f"Catalog reuse request for snapshot {req.snapshot_id}",
        status="pending_approval",
        idempotency_key=idem_key,
        fee_credits=fee,
    )
    session.add(share_event)
    session.flush()

    # Outbox notifications (C0/C3)
    NotificationService.enqueue(
        db=session,
        event_id=share_event.id,
        recipient_scope="super_admin",
        channels=["in_app", "email"],
        kind="request_created",
        target_client_id=ctx.client_id,
        company_name=comp_name,
        payload={"company_name": comp_name, "event_id": share_event.id, "reason": share_event.reason},
    )
    NotificationService.enqueue(
        db=session,
        event_id=share_event.id,
        recipient_user_id=ctx.user_id,
        recipient_scope="client_admin",
        channels=["in_app", "email"],
        kind="request_created",
        target_client_id=ctx.client_id,
        company_name=comp_name,
        payload={"company_name": comp_name, "event_id": share_event.id, "reason": share_event.reason},
    )

    log_catalog_audit(
        session=session,
        actor_user_id=ctx.user_id,
        action="catalog.request_created",
        client_id=ctx.client_id,
        details={
            "share_event_id": share_event.id,
            "snapshot_id": req.snapshot_id,
            "target_client_slug": ctx.client_slug,
            "reason": req.reason,
            "correlation_id": ctx.correlation_id,
        },
        is_actor_super_admin=ctx.is_super_admin,
    )
    session.commit()

    return build_request_view(
        event=share_event,
        company_name=comp_name,
        snapshot_version=snapshot.version,
        canonical_key=company.canonical_key if company else None,
        sla_hours=config.CATALOG_APPROVAL_SLA_HOURS,
        already_pending=False,
        is_super_admin=ctx.is_super_admin,
    )


@catalog_router.get("/requests/{request_id}")
def get_reuse_request(
    request_id: int,
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_request", explicit_slug_from="query", allow_optional_client_for_super_admin=True)),
    session: Session = Depends(get_platform_db),
):
    """Fetches a single reuse request with status timeline (C3)."""
    row = session.query(
        ShareEvent,
        Snapshot.version.label("snapshot_version"),
        Company.display_name.label("company_name"),
        Company.canonical_key.label("canonical_key"),
    ).join(Snapshot, Snapshot.id == ShareEvent.snapshot_id)\
     .join(Company, Company.id == Snapshot.company_id)\
     .filter(ShareEvent.id == request_id).first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    event, snap_ver, comp_name, canon_key = row
    if not ctx.is_super_admin and event.target_client_id != ctx.client_id:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    return build_request_view(
        event=event,
        company_name=comp_name,
        snapshot_version=snap_ver,
        canonical_key=canon_key,
        sla_hours=config.CATALOG_APPROVAL_SLA_HOURS,
        is_super_admin=ctx.is_super_admin,
    )


@catalog_router.post("/requests/{request_id}/cancel")
def cancel_reuse_request(
    request_id: int,
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_request", explicit_slug_from="query")),
    session: Session = Depends(get_platform_db),
):
    """Cancels a pending reuse request (requester or client_admin)."""
    event = session.query(ShareEvent).filter_by(id=request_id).first()
    if not event:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    if not ctx.is_super_admin and event.target_client_id != ctx.client_id:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    is_requester = (event.requested_by == ctx.user_id)
    is_admin = (ctx.effective_role == "client_admin" or ctx.user_role == "client_admin" or ctx.is_super_admin)
    if not (is_requester or is_admin):
        raise HTTPException(status_code=403, detail="Only the requester or client administrator can cancel this request.")

    if event.status != "pending_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel request in status '{event.status}'. Only pending requests can be cancelled.",
        )

    event.status = "cancelled"
    event.completed_at = datetime.now(timezone.utc)

    snapshot = session.query(Snapshot).filter_by(id=event.snapshot_id).first()
    company = session.query(Company).filter_by(id=snapshot.company_id).first() if snapshot else None

    log_catalog_audit(
        session=session,
        actor_user_id=ctx.user_id,
        action="catalog.request_cancelled",
        client_id=event.target_client_id,
        details={
            "share_event_id": event.id,
            "snapshot_id": event.snapshot_id,
            "target_client_slug": ctx.client_slug,
            "correlation_id": ctx.correlation_id,
        },
        is_actor_super_admin=ctx.is_super_admin,
    )
    session.commit()

    return build_request_view(
        event=event,
        company_name=company.display_name if company else "Company",
        snapshot_version=snapshot.version if snapshot else 1,
        canonical_key=company.canonical_key if company else None,
        sla_hours=config.CATALOG_APPROVAL_SLA_HOURS,
        is_super_admin=ctx.is_super_admin,
    )


@catalog_router.get("/requests")
def list_reuse_requests(
    status: Optional[str] = Query(None, description="Filter by status (pending_approval, done, rejected, cancelled)"),
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_request", explicit_slug_from="query", allow_optional_client_for_super_admin=True)),
    session: Session = Depends(get_platform_db),
):
    """Lists intelligence reuse requests with timelines and SLA hints."""
    query = session.query(
        ShareEvent,
        Client.slug.label("client_slug"),
        Client.name.label("client_name"),
        Snapshot.version.label("snapshot_version"),
        Company.display_name.label("company_name"),
        Company.canonical_key.label("canonical_key"),
    ).join(Client, Client.id == ShareEvent.target_client_id)\
     .join(Snapshot, Snapshot.id == ShareEvent.snapshot_id)\
     .join(Company, Company.id == Snapshot.company_id)

    if not ctx.is_super_admin:
        query = query.filter(Client.slug == ctx.client_slug)
    elif ctx.client_slug:
        query = query.filter(Client.slug == ctx.client_slug)

    if status:
        query = query.filter(ShareEvent.status == status)

    rows = query.order_by(desc(ShareEvent.created_at)).limit(100).all()
    results = []
    for event, c_slug, c_name, snap_ver, comp_name, canon_key in rows:
        results.append(build_request_view(
            event=event,
            company_name=comp_name,
            snapshot_version=snap_ver,
            canonical_key=canon_key,
            sla_hours=config.CATALOG_APPROVAL_SLA_HOURS,
            is_super_admin=ctx.is_super_admin,
        ))
    return {"requests": results, "total": len(results)}


# ── Super-Admin Platform Endpoints ───────────────────────────────────────────

@catalog_router.get("/companies")
def list_catalog_companies(
    search: Optional[str] = Query(None, description="Search legal or display name, or identifier"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: PlatformRequestContext = Depends(platform_context("catalog:view_library")),
    session: Session = Depends(get_platform_db),
):
    """Platform Account Library overview for Super Administrators."""
    query = session.query(Company)
    if search:
        s = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Company.display_name.ilike(s),
                Company.legal_name.ilike(s),
                Company.canonical_key.ilike(s),
            )
        )

    total = query.count()
    companies = query.order_by(Company.display_name.asc()).offset(offset).limit(limit).all()

    items = []
    now = datetime.now(timezone.utc)
    for comp in companies:
        latest_snap = session.query(Snapshot).filter_by(
            company_id=comp.id, status="ready"
        ).order_by(desc(Snapshot.version)).first()

        holder_clients = session.query(Client.slug, Client.name).join(
            ShareEvent, ShareEvent.target_client_id == Client.id
        ).filter(
            ShareEvent.snapshot_id == (latest_snap.id if latest_snap else -1),
            ShareEvent.status == "done",
        ).distinct().all()

        age_days = None
        freshness = "unknown"
        if latest_snap and latest_snap.taken_at:
            taken_at = latest_snap.taken_at if latest_snap.taken_at.tzinfo else latest_snap.taken_at.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - taken_at).days)
            freshness = "fresh" if age_days <= 15 else ("stale" if age_days <= 45 else "old")

        items.append({
            "company_id": comp.id,
            "canonical_key": comp.canonical_key,
            "display_name": comp.display_name,
            "legal_name": comp.legal_name,
            "country": comp.country,
            "snapshot_id": latest_snap.id if latest_snap else None,
            "snapshot_version": latest_snap.version if latest_snap else None,
            "quality_score": float(latest_snap.quality_score) if latest_snap and latest_snap.quality_score else None,
            "quality_grade": latest_snap.quality_grade if latest_snap else None,
            "counts": latest_snap.counts if latest_snap else {},
            "age_days": age_days,
            "freshness": freshness,
            "holder_clients": [{"slug": h[0], "name": h[1]} for h in holder_clients],
        })

    return {"companies": items, "total": total, "limit": limit, "offset": offset}


@catalog_router.post("/publish")
def publish_account(
    req: PublishRequest,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:publish")),
    session: Session = Depends(get_platform_db),
):
    """Publishes a sanitized account snapshot from a tenant schema to platform_catalog."""
    clean_slug = auth.normalize_client_slug(req.source_client_slug)
    try:
        snapshot = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=clean_slug,
            account_id=req.account_id,
            published_by_user_id=ctx.user_id,
            force=req.force,
        )
        company = session.query(Company).filter_by(id=snapshot.company_id).first()
        return {
            "ok": True,
            "snapshot_id": snapshot.id,
            "version": snapshot.version,
            "status": snapshot.status,
            "canonical_key": company.canonical_key if company else None,
            "quality_score": float(snapshot.quality_score or 0.0),
            "quality_grade": snapshot.quality_grade,
            "counts": snapshot.counts,
        }
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise _handle_unexpected_exception("Publish", e, ctx.correlation_id)


@catalog_router.post("/assign")
def assign_account(
    req: AssignRequest,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:assign")),
    session: Session = Depends(get_platform_db),
):
    """Super Admin direct assignment drawer: imports catalog intelligence directly into a client."""
    clean_slug = auth.normalize_client_slug(req.target_client_slug)
    idem_key = f"assign_{clean_slug}_{req.snapshot_id}_{int(datetime.now().timestamp())}"
    target_ctx = TenantRequestContext.for_client(
        session=session,
        client=clean_slug,
        user_id=ctx.user_id,
        user_role="super_admin",
        effective_role="super_admin",
        correlation_id=ctx.correlation_id,
    )

    try:
        fee = CatalogPricing.fee_for(mode=req.mode, is_super_admin=True)
        res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=target_ctx,
            snapshot_id=req.snapshot_id,
            user_id=ctx.user_id,
            idempotency_key=idem_key,
            mode=req.mode,
            fee_credits=fee,
            reason=req.reason or f"Super admin direct assignment to {clean_slug}",
            approved_by_user_id=ctx.user_id,
            is_admin_assign=True,
        )
        return res
    except LocalEditsPresentError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "local_edits_present", "message": str(e), "diff": e.diff},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except InsufficientCreditsError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except AccountQuotaExceededError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except IdempotencyConflictError as e:
        raise HTTPException(status_code=409, detail={"error": "idempotency_key_reuse", "message": str(e)})
    except SkeletonIncompatibleError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "skeleton_incompatible", "message": str(e)},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except DuplicateAccountError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise _handle_unexpected_exception("Direct assignment", e, ctx.correlation_id)


def apply_request_decision(
    session: Session,
    request_id: int,
    decision: str,
    actor_user_id: int,
    correlation_id: str,
    note: Optional[str] = None,
    fee_override: Optional[int] = None,
    use_latest_version: bool = False,
) -> Dict[str, Any]:
    """Core decision engine shared by single-decision, bulk-decision, and legacy approve/reject routes."""
    event = session.query(ShareEvent).filter_by(id=request_id).first()
    if not event:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    # Idempotent response if already decided
    if event.status in ("done", "approved", "rejected", "cancelled"):
        return {
            "ok": True,
            "request_id": event.id,
            "status": event.status,
            "message": f"Request has already been processed with status '{event.status}'.",
        }

    if event.status != "pending_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Request {request_id} is in status '{event.status}', expected 'pending_approval'",
        )

    snapshot = session.query(Snapshot).filter_by(id=event.snapshot_id).first()
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Snapshot {event.snapshot_id} not found")

    company = session.query(Company).filter_by(id=snapshot.company_id).first()
    comp_name = company.display_name if company else "Company"

    if decision == "approve":
        if snapshot.status != "ready":
            raise HTTPException(status_code=400, detail=f"Snapshot {snapshot.id} is not ready for hydration (status: {snapshot.status})")

        # Check for newer ready snapshot version
        latest_snapshot = (
            session.query(Snapshot)
            .filter_by(company_id=snapshot.company_id, status="ready")
            .order_by(Snapshot.version.desc())
            .first()
        )
        target_snapshot_id = snapshot.id
        if latest_snapshot and latest_snapshot.id != snapshot.id:
            if not use_latest_version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "snapshot_superseded",
                        "message": f"A newer snapshot version (v{latest_snapshot.version}) is available. Set use_latest_version=true to proceed with the newer version.",
                        "new_version": latest_snapshot.version,
                        "new_snapshot_id": latest_snapshot.id,
                    },
                )
            target_snapshot_id = latest_snapshot.id
            event.snapshot_id = target_snapshot_id
            session.flush()

        target_ctx = TenantRequestContext.for_client(
            session=session,
            client=event.target_client_id,
            user_id=event.requested_by or actor_user_id,
            user_role="super_admin",
            effective_role="super_admin",
            correlation_id=correlation_id,
        )

        fee = fee_override if fee_override is not None else event.fee_credits

        try:
            res = AccountReuseService.hydrate_account_from_catalog(
                session=session,
                ctx=target_ctx,
                snapshot_id=target_snapshot_id,
                user_id=event.requested_by or actor_user_id,
                idempotency_key=event.idempotency_key,
                mode=event.mode or "instant",
                fee_credits=fee or 0,
                reason=event.reason,
                approved_by_user_id=actor_user_id,
            )
        except LocalEditsPresentError as e:
            raise HTTPException(
                status_code=409,
                detail={"error": "local_edits_present", "message": str(e), "diff": e.diff},
                headers={"X-Correlation-ID": correlation_id},
            )
        except InsufficientCreditsError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except SkeletonIncompatibleError as e:
            raise HTTPException(
                status_code=409,
                detail={"error": "skeleton_incompatible", "message": str(e)},
                headers={"X-Correlation-ID": correlation_id},
            )
        except DuplicateAccountError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except AccountQuotaExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except AccountReuseError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"[apply_request_decision] Hydration failed (correlation_id={correlation_id}): {e}", exc_info=True)
            session.rollback()
            new_session = SessionLocal()
            try:
                failed_event = new_session.query(ShareEvent).filter_by(id=request_id).first()
                if failed_event:
                    failed_event.status = "failed"
                    failed_event.error = _sanitize_error_message(str(e))
                    new_session.commit()
            finally:
                new_session.close()

            raise HTTPException(
                status_code=500,
                detail={"detail": "Approval hydration failed", "correlation_id": correlation_id},
                headers={"X-Correlation-ID": correlation_id},
            )

        event.decided_by = actor_user_id
        event.decided_at = datetime.now(timezone.utc)
        event.decision_note = note

        NotificationService.enqueue(
            db=session,
            event_id=event.id,
            recipient_user_id=event.requested_by,
            recipient_scope="client_admin",
            channels=["in_app", "email"],
            kind="approved",
            target_client_id=event.target_client_id,
            company_name=comp_name,
            payload={
                "company_name": comp_name,
                "event_id": event.id,
                "account_id": res.get("account_id"),
                "fee_credits": fee,
                "decision_note": note,
            },
        )
        session.commit()

        return {
            "ok": True,
            "status": "approved",
            "request_id": event.id,
            "account_id": res.get("account_id"),
            "counts": res.get("counts"),
            "decision_note": note,
        }

    elif decision == "reject":
        event.status = "rejected"
        event.decided_by = actor_user_id
        event.decided_at = datetime.now(timezone.utc)
        event.completed_at = datetime.now(timezone.utc)
        event.decision_note = note or "Rejected by platform administrator"
        event.approved_by = actor_user_id

        NotificationService.enqueue(
            db=session,
            event_id=event.id,
            recipient_user_id=event.requested_by,
            recipient_scope="client_admin",
            channels=["in_app", "email"],
            kind="rejected",
            target_client_id=event.target_client_id,
            company_name=comp_name,
            payload={
                "company_name": comp_name,
                "event_id": event.id,
                "decision_note": event.decision_note,
            },
        )

        target_client = session.query(Client).filter_by(id=event.target_client_id).first()
        log_catalog_audit(
            session=session,
            actor_user_id=actor_user_id,
            action="catalog.request_rejected",
            client_id=event.target_client_id,
            target_user_id=event.requested_by,
            details={
                "share_event_id": event.id,
                "snapshot_id": event.snapshot_id,
                "target_client_slug": target_client.slug if target_client else None,
                "reason": event.decision_note,
                "correlation_id": correlation_id,
            },
            is_actor_super_admin=True,
        )
        session.commit()

        return {
            "ok": True,
            "request_id": event.id,
            "status": "rejected",
            "decision_note": event.decision_note,
        }
    else:
        raise HTTPException(status_code=400, detail=f"Invalid decision '{decision}', expected 'approve' or 'reject'")


@catalog_router.get("/inbox")
def get_catalog_approval_inbox(
    status: Optional[str] = Query("pending_approval", description="Filter status: pending_approval, done, rejected, all"),
    client: Optional[str] = Query(None, description="Filter by client slug or ID"),
    q: Optional[str] = Query(None, description="Search company name or requester"),
    sort: Optional[str] = Query("created_desc", description="Sort order: created_desc, created_asc, score_desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    ctx: PlatformRequestContext = Depends(platform_context("catalog:approve")),
    session: Session = Depends(get_platform_db),
):
    """Super-Admin Approval Inbox with QualityReports, RiskFlags, and Quota Checks (C3)."""
    query = (
        session.query(ShareEvent, Client, Snapshot, Company, User)
        .join(Client, Client.id == ShareEvent.target_client_id)
        .join(Snapshot, Snapshot.id == ShareEvent.snapshot_id)
        .join(Company, Company.id == Snapshot.company_id)
        .outerjoin(User, User.id == ShareEvent.requested_by)
    )

    if status and status != "all":
        query = query.filter(ShareEvent.status == status)

    if client:
        c_clean = client.strip()
        if c_clean.isdigit():
            query = query.filter(Client.id == int(c_clean))
        else:
            query = query.filter(Client.slug == c_clean)

    if q:
        search = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Company.display_name.ilike(search),
                Company.legal_name.ilike(search),
                Company.canonical_key.ilike(search),
                User.full_name.ilike(search),
                User.email.ilike(search),
            )
        )

    if sort == "created_asc":
        query = query.order_by(ShareEvent.created_at.asc())
    elif sort == "score_desc":
        query = query.order_by(Snapshot.quality_score.desc())
    else:
        query = query.order_by(ShareEvent.created_at.desc())

    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    now = datetime.now(timezone.utc)
    inbox_items = []

    for event, cl, snap, comp, req_user in rows:
        t_at = snap.taken_at if snap.taken_at else snap.created_at or now
        if t_at.tzinfo is None:
            t_at = t_at.replace(tzinfo=timezone.utc)
        age_days = max(0, (now - t_at).days)

        if age_days <= 15:
            freshness = "fresh"
        elif age_days <= 45:
            freshness = "moderate"
        else:
            freshness = "stale"

        latest_ready = (
            session.query(Snapshot)
            .filter_by(company_id=comp.id, status="ready")
            .order_by(Snapshot.version.desc())
            .first()
        )
        is_latest_ready = bool(latest_ready and latest_ready.id == snap.id)
        ready_version = latest_ready.version if latest_ready else snap.version

        identifiers = session.query(CompanyIdentifier).filter_by(company_id=comp.id).all()
        id_types = {ident.id_type.lower() for ident in identifiers}
        has_domain = ("domain" in id_types) or bool(comp.canonical_key and "." in comp.canonical_key)
        has_cik = ("sec_cik" in id_types) or ("cik" in id_types)
        has_lei = ("lei" in id_types)
        identifier_coverage = {
            "domain": has_domain,
            "cik": has_cik,
            "lei": has_lei,
        }

        score = round(float(snap.quality_score or 85.0), 1)
        grade = snap.quality_grade or ("A" if score >= 85 else ("B" if score >= 70 else ("C" if score >= 50 else "D")))
        counts = snap.counts or {}

        acc_pts = 30.0 if counts.get("accounts", 1) >= 1 else 0.0
        lob_pts = min(25.0, round(counts.get("lobs", 0) * 8.33, 1))
        per_pts = min(25.0, round(counts.get("personas", 0) * 5.0, 1))
        sig_pts = min(20.0, round((counts.get("posts", 0) + counts.get("cxo_movements", 0) + counts.get("jobs", 0)) * 2.0, 1))

        rubric_breakdown = [
            {"criterion": "Core Enterprise Metadata", "points": acc_pts, "max": 30.0},
            {"criterion": "Lines of Business Hierarchy", "points": lob_pts, "max": 25.0},
            {"criterion": "Key Decision Makers & Personas", "points": per_pts, "max": 25.0},
            {"criterion": "Signal Intelligence & Activity", "points": sig_pts, "max": 20.0},
        ]

        prev_reuse_count = (
            session.query(ShareEvent)
            .join(Snapshot, Snapshot.id == ShareEvent.snapshot_id)
            .filter(
                Snapshot.company_id == comp.id,
                ShareEvent.status.in_(["done", "approved"]),
            )
            .count()
        )

        quality_report = {
            "score": score,
            "grade": grade,
            "freshness": freshness,
            "breakdown": rubric_breakdown,
            "counts_by_entity": counts,
            "identifier_coverage": identifier_coverage,
            "policy_gaps": getattr(snap, "policy_gaps", []) or [],
            "merge_candidate": bool(getattr(comp, "merge_candidate", False)),
            "previous_reuse_count": prev_reuse_count,
        }

        balance_info = CreditService.get_balance(cl.slug)
        remaining_credits = balance_info.get("remaining", 0)
        max_accounts = cl.max_accounts or 0

        target_schema = schema_for(cl.slug)
        curr_accts = session.execute(text(f'SELECT COUNT(*) FROM "{target_schema}"."accounts"')).scalar() or 0
        quota_exceeded = (max_accounts > 0 and curr_accts >= max_accounts)
        insufficient_credits = (remaining_credits < (event.fee_credits or 0))

        client_credit_status = {
            "balance": remaining_credits,
            "max_accounts": max_accounts,
            "usage": curr_accts,
            "allowed": not (quota_exceeded or insufficient_credits),
            "quota_exceeded": quota_exceeded,
            "insufficient_credits": insufficient_credits,
        }

        existing = AccountMatcher.find_existing(session, cl.slug, comp)
        duplicate_warning = {
            "exists": bool(existing),
            "account_id": existing.account_id if existing else None,
            "matching_reason": existing.match_reason if existing else None,
        }

        risk_flags = []
        if age_days > 60:
            risk_flags.append("stale")
        if not has_domain and not has_cik:
            risk_flags.append("weak_identifier")
        if quota_exceeded:
            risk_flags.append("client_quota_exceeded")
        elif max_accounts > 0 and (max_accounts - curr_accts) <= 2:
            risk_flags.append("client_near_quota")
        if insufficient_credits:
            risk_flags.append("insufficient_credits")
        if existing:
            risk_flags.append("duplicate_in_target")
        if getattr(comp, "status", None) == "quarantined":
            risk_flags.append("quarantined")

        inbox_items.append({
            "id": event.id,
            "status": event.status,
            "mode": event.mode,
            "created_at": event.created_at.isoformat() if event.created_at else None,
            "business_reason": event.reason,
            "decision_note": event.decision_note,
            "decided_at": event.decided_at.isoformat() if getattr(event, "decided_at", None) else None,
            "requester": {
                "id": req_user.id if req_user else None,
                "name": req_user.full_name if req_user else "Unknown User",
                "email": req_user.email if req_user else "",
            },
            "client": {
                "id": cl.id,
                "slug": cl.slug,
                "name": cl.name,
            },
            "company": {
                "id": comp.id,
                "display_name": comp.display_name,
                "legal_name": comp.legal_name,
                "domain": comp.canonical_key,
            },
            "snapshot": {
                "id": snap.id,
                "version": snap.version,
                "taken_at": t_at.isoformat(),
                "age_days": age_days,
                "is_latest_ready": is_latest_ready,
                "ready_version": ready_version,
            },
            "quality_report": quality_report,
            "client_credit_status": client_credit_status,
            "estimated_fee": event.fee_credits or 0,
            "estimated_savings": max(0, 50 - (event.fee_credits or 0)),
            "duplicate_warning": duplicate_warning,
            "risk_flags": risk_flags,
        })

    return {
        "items": inbox_items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@catalog_router.post("/inbox/{request_id}/decision")
def decide_approval_inbox(
    request_id: int,
    req: DecisionRequest,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:approve")),
    session: Session = Depends(get_platform_db),
):
    """Super Admin decision on a single reuse request (C3)."""
    return apply_request_decision(
        session=session,
        request_id=request_id,
        decision=req.decision,
        actor_user_id=ctx.user_id,
        correlation_id=ctx.correlation_id,
        note=req.note,
        fee_override=req.fee_override,
        use_latest_version=req.use_latest_version,
    )


@catalog_router.post("/inbox/bulk-decision")
def bulk_decision_approval_inbox(
    req: BulkDecisionRequest,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:approve")),
):
    """Super Admin batch decision (up to 50 items) with per-item transaction isolation (C3)."""
    if len(req.decisions) > 50:
        raise HTTPException(status_code=400, detail="Cannot process more than 50 decisions at once.")

    results = []
    succeeded = 0
    failed = 0

    for item in req.decisions:
        with SessionLocal() as item_session:
            try:
                res = apply_request_decision(
                    session=item_session,
                    request_id=item.id,
                    decision=item.decision,
                    actor_user_id=ctx.user_id,
                    correlation_id=ctx.correlation_id,
                    note=item.note,
                    fee_override=item.fee_override,
                    use_latest_version=req.use_latest_version,
                )
                results.append({"id": item.id, "ok": True, **res})
                succeeded += 1
            except Exception as e:
                item_session.rollback()
                err_msg = _sanitize_error_message(str(e))
                results.append({"id": item.id, "ok": False, "error": err_msg})
                failed += 1

    return {
        "total": len(req.decisions),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


@catalog_router.post("/requests/{request_id}/approve")
def approve_reuse_request(
    request_id: int,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:approve")),
    session: Session = Depends(get_platform_db),
):
    """Super Admin approves a pending reuse request and triggers hydration."""
    return apply_request_decision(
        session=session,
        request_id=request_id,
        decision="approve",
        actor_user_id=ctx.user_id,
        correlation_id=ctx.correlation_id,
    )


@catalog_router.post("/requests/{request_id}/reject")
def reject_reuse_request(
    request_id: int,
    req: Optional[RejectRequest] = None,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:approve")),
    session: Session = Depends(get_platform_db),
):
    """Super Admin rejects a pending reuse request."""
    return apply_request_decision(
        session=session,
        request_id=request_id,
        decision="reject",
        actor_user_id=ctx.user_id,
        correlation_id=ctx.correlation_id,
        note=req.reason if req else None,
    )


# ── In-App Notifications Endpoints ───────────────────────────────────────────

@catalog_notifications_router.get("")
def list_catalog_notifications(
    unread_only: bool = Query(False, description="Filter for unread notifications only"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ctx: TenantRequestContext = Depends(tenant_context("account:read", allow_optional_client_for_super_admin=True)),
    session: Session = Depends(get_platform_db),
):
    """Lists in-app notifications for the caller, anti-oracle sanitized (C3)."""
    items = NotificationService.list_in_app_notifications(
        session,
        user_id=ctx.user_id,
        client_id=ctx.client_id,
        is_super_admin=ctx.is_super_admin,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )
    return {"notifications": items, "count": len(items)}


@catalog_notifications_router.get("/unread-count")
def get_unread_catalog_notification_count(
    ctx: TenantRequestContext = Depends(tenant_context("account:read", allow_optional_client_for_super_admin=True)),
    session: Session = Depends(get_platform_db),
):
    """Returns the unread count of catalog notifications for the bell badge (C3)."""
    count = NotificationService.count_unread(
        session,
        user_id=ctx.user_id,
        client_id=ctx.client_id,
        is_super_admin=ctx.is_super_admin,
    )
    return {"unread_count": count}


@catalog_notifications_router.post("/{notification_id}/read")
def mark_catalog_notification_read(
    notification_id: int,
    ctx: TenantRequestContext = Depends(tenant_context("account:read", allow_optional_client_for_super_admin=True)),
    session: Session = Depends(get_platform_db),
):
    """Marks a single notification as read (C3)."""
    ok = NotificationService.mark_read(session, notification_id=notification_id, user_id=ctx.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Notification not found")
    session.commit()
    return {"ok": True, "id": notification_id, "status": "read"}


@catalog_notifications_router.post("/read-all")
def mark_all_catalog_notifications_read(
    ctx: TenantRequestContext = Depends(tenant_context("account:read", allow_optional_client_for_super_admin=True)),
    session: Session = Depends(get_platform_db),
):
    """Marks all catalog notifications for the caller as read (C3)."""
    marked = NotificationService.mark_all_read(
        session,
        user_id=ctx.user_id,
        client_id=ctx.client_id,
        is_super_admin=ctx.is_super_admin,
    )
    session.commit()
    return {"ok": True, "marked_read": marked}


@catalog_router.post("/share-events/{share_event_id}/rollback")
def rollback_share_event(
    share_event_id: int,
    req: Optional[RollbackRequest] = None,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:rollback")),
    session: Session = Depends(get_platform_db),
):
    """Super Admin exact rollback of a completed hydration event.
    
    Invariants:
    - Blocked if target account contains private CRM children (deals, action items).
    - Reverse topological deletion and fee refund.
    """
    try:
        res = AccountReuseService.rollback_account_reuse(
            session=session,
            share_event_id=share_event_id,
            admin_user_id=ctx.user_id,
            reason=req.reason if req else None,
        )
        return res
    except RollbackBlockedError as e:
        session.rollback()
        event = session.query(ShareEvent).filter_by(id=share_event_id).first()
        client_id = event.target_client_id if event else None
        target_client = session.query(Client).filter_by(id=client_id).first() if client_id else None
        log_catalog_audit(
            session=session,
            actor_user_id=ctx.user_id,
            action="catalog.rollback_blocked",
            client_id=client_id,
            details={
                "share_event_id": share_event_id,
                "target_client_slug": target_client.slug if target_client else None,
                "reason": str(e),
                "breakdown": getattr(e, "breakdown", {}),
                "correlation_id": ctx.correlation_id,
            },
            is_actor_super_admin=True,
        )
        session.commit()
        status_code = 400 if "refresh" in str(e).lower() else 409
        raise HTTPException(
            status_code=status_code,
            detail={
                "message": str(e),
                "breakdown": getattr(e, "breakdown", {}),
            },
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise _handle_unexpected_exception("Rollback", e, ctx.correlation_id)


class ResolveMergeCandidateRequest(BaseModel):
    action: str  # "merge" | "dismiss"
    keep_company_id: Optional[int] = None


@catalog_router.post("/merge-candidates/{candidate_id}/resolve")
def resolve_merge_candidate(
    candidate_id: int,
    request: ResolveMergeCandidateRequest,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:assign")),
    session: Session = Depends(get_platform_db),
):
    """Super-admin resolution of duplicate company merge candidates."""
    try:
        candidate = CatalogService.resolve_merge_candidate(
            session=session,
            candidate_id=candidate_id,
            action=request.action,
            keep_company_id=request.keep_company_id,
            admin_user_id=ctx.user_id,
        )
        return {
            "success": True,
            "candidate_id": candidate.id,
            "status": candidate.status,
            "resolved_at": candidate.resolved_at.isoformat() if candidate.resolved_at else None,
        }
    except ValueError as e:
        session.rollback()
        raise HTTPException(
            status_code=400,
            detail=str(e),
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except Exception as e:
        session.rollback()
        raise _handle_unexpected_exception("ResolveMergeCandidate", e, ctx.correlation_id)

