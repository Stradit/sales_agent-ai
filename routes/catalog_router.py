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
from typing import Any, Dict, List, Optional
import uuid

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
)
from db.tenant import schema_for, validate_slug
from services.account_reuse_service import (
    AccountReuseError,
    AccountReuseService,
    DuplicateAccountError,
    IdempotencyConflictError,
    LocalEditsPresentError,
    RollbackBlockedError,
    SkeletonIncompatibleError,
)
from services.catalog_audit import log_catalog_audit, reuse_check_limiter
from services.catalog_pricing import CatalogPricing
from services.catalog_service import AmbiguousIdentifierError, CatalogService
from services.credit_service import AccountQuotaExceededError, InsufficientCreditsError

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


# ── Pydantic Request & Response Schemas ──────────────────────────────────────

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

    existing_req = session.query(ShareEvent).filter_by(
        snapshot_id=req.snapshot_id,
        target_client_id=ctx.client_id,
        status="pending_approval",
    ).first()
    if existing_req:
        return {
            "ok": True,
            "request_id": existing_req.id,
            "status": "pending_approval",
            "message": "A reuse request for this account is already pending approval.",
        }

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

    return {
        "ok": True,
        "request_id": share_event.id,
        "status": "pending_approval",
        "message": "Reuse request submitted for administrator review.",
    }


@catalog_router.get("/requests")
def list_reuse_requests(
    status: Optional[str] = Query(None, description="Filter by status (pending_approval, done, rejected)"),
    ctx: TenantRequestContext = Depends(tenant_context("account:reuse_request", explicit_slug_from="query", allow_optional_client_for_super_admin=True)),
    session: Session = Depends(get_platform_db),
):
    """Lists intelligence reuse requests."""
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
        results.append({
            "request_id": event.id,
            "status": event.status,
            "client_slug": c_slug,
            "client_name": c_name,
            "snapshot_id": event.snapshot_id,
            "snapshot_version": snap_ver,
            "company_name": comp_name,
            "canonical_key": canon_key,
            "mode": event.mode,
            "fee_credits": event.fee_credits,
            "reason": event.reason,
            "created_at": event.created_at.isoformat() if event.created_at else None,
            "completed_at": event.completed_at.isoformat() if event.completed_at else None,
        })
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


@catalog_router.post("/requests/{request_id}/approve")
def approve_reuse_request(
    request_id: int,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:approve")),
    session: Session = Depends(get_platform_db),
):
    """Super Admin approves a pending reuse request and triggers hydration."""
    event = session.query(ShareEvent).filter_by(id=request_id).first()
    if not event:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    if event.status != "pending_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Request {request_id} is in status '{event.status}', expected 'pending_approval'",
        )

    target_ctx = TenantRequestContext.for_client(
        session=session,
        client=event.target_client_id,
        user_id=event.requested_by or ctx.user_id,
        user_role="super_admin",
        effective_role="super_admin",
        correlation_id=ctx.correlation_id,
    )

    try:
        res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=target_ctx,
            snapshot_id=event.snapshot_id,
            user_id=event.requested_by or ctx.user_id,
            idempotency_key=event.idempotency_key,
            mode=event.mode,
            fee_credits=event.fee_credits,
            reason=event.reason,
            approved_by_user_id=ctx.user_id,
        )
        return {
            "ok": True,
            "status": "approved",
            "request_id": event.id,
            "account_id": res["account_id"],
            "counts": res["counts"],
        }
    except LocalEditsPresentError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "local_edits_present", "message": str(e), "diff": e.diff},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except InsufficientCreditsError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except SkeletonIncompatibleError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": "skeleton_incompatible", "message": str(e)},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )
    except DuplicateAccountError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except AccountQuotaExceededError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[approve_reuse_request] Approval hydration failed (correlation_id={ctx.correlation_id}): {e}", exc_info=True)
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
            detail={"detail": "Approval hydration failed", "correlation_id": ctx.correlation_id},
            headers={"X-Correlation-ID": ctx.correlation_id},
        )


@catalog_router.post("/requests/{request_id}/reject")
def reject_reuse_request(
    request_id: int,
    req: Optional[RejectRequest] = None,
    ctx: PlatformRequestContext = Depends(platform_context("catalog:approve")),
    session: Session = Depends(get_platform_db),
):
    """Super Admin rejects a pending reuse request."""
    event = session.query(ShareEvent).filter_by(id=request_id).first()
    if not event:
        raise HTTPException(status_code=404, detail=f"Request {request_id} not found")

    if event.status != "pending_approval":
        raise HTTPException(status_code=400, detail=f"Request is in status '{event.status}'")

    event.status = "rejected"
    event.reason = (req.reason if req else None) or "Rejected by platform administrator"
    event.completed_at = datetime.now(timezone.utc)

    target_client = session.query(Client).filter_by(id=event.target_client_id).first()
    log_catalog_audit(
        session=session,
        actor_user_id=ctx.user_id,
        action="catalog.request_rejected",
        client_id=event.target_client_id,
        target_user_id=event.requested_by,
        details={
            "share_event_id": event.id,
            "snapshot_id": event.snapshot_id,
            "target_client_slug": target_client.slug if target_client else None,
            "reason": event.reason,
            "correlation_id": ctx.correlation_id,
        },
        is_actor_super_admin=True,
    )
    session.commit()

    return {"ok": True, "request_id": event.id, "status": "rejected"}


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

