"""Catalog Router — REST Endpoints for Cross-Tenant Intelligence Sharing.

Provides:
1. Anti-Oracle Intelligence Check (/api/catalog/reuse-check)
2. Atomic Single-Transaction Hydration (/api/catalog/hydrate)
3. Reuse Request Flow (/api/catalog/requests, approve, reject)
4. Super-Admin Platform Catalog Management (/api/catalog/companies, publish, assign, rollback)
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, text
from sqlalchemy.orm import Session

import auth
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
    RollbackBlockedError,
)
from services.catalog_service import CatalogService
from services.credit_service import AccountQuotaExceededError, InsufficientCreditsError

catalog_router = APIRouter(prefix="/api/catalog", tags=["Verified Intelligence Catalog"])


# ── Dependency: Platform Database Session ────────────────────────────────────

def get_platform_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def resolve_target_client(
    request: Request,
    user: auth.User,
    session: Session,
    permission: str,
    explicit_slug: Optional[str] = None,
) -> str:
    """Resolves and re-authorizes target client slug for tenant catalog operations.
    
    Rules:
    1. Compute ctx_slug = auth.resolve_request_client_slug(request, user).
    2. If user.role == 'super_admin': the explicit body/query slug wins if provided,
       else ctx_slug; must exist and be 'active'.
    3. Otherwise: the explicit slug must be absent or equal to ctx_slug after normalize_client_slug;
       any mismatch -> 403 ('Target client does not match your active client context').
       ctx_slug is required (400 if missing).
    4. After resolving, re-authorise against that exact slug:
       auth.get_effective_role(session, user, target) then auth.has_permission(role, permission);
       deny -> 403.
    """
    ctx_slug = auth.resolve_request_client_slug(request, user)

    if getattr(user, "role", None) == "super_admin":
        target = auth.normalize_client_slug(explicit_slug) if explicit_slug else ctx_slug
        if not target:
            raise HTTPException(status_code=400, detail="Target client slug required")
        client = session.query(Client).filter_by(slug=target).first()
        if not client or not client.is_active or client.status != "active":
            raise HTTPException(status_code=404, detail=f"Target client '{target}' not found or inactive")
        return target

    # Non-super-admin
    if not ctx_slug:
        raise HTTPException(status_code=400, detail="Client context required (pass X-Client-Slug header)")

    if explicit_slug:
        norm_explicit = auth.normalize_client_slug(explicit_slug)
        if norm_explicit != ctx_slug:
            raise HTTPException(
                status_code=403,
                detail="Target client does not match your active client context",
            )

    target = ctx_slug
    client = session.query(Client).filter_by(slug=target).first()
    if not client or not client.is_active or client.status != "active":
        raise HTTPException(status_code=404, detail=f"Target client '{target}' not found or inactive")

    # Re-authorize against that exact slug
    effective_role = auth.get_effective_role(session, user, target, enforce=True)
    if not effective_role or not auth.has_permission(effective_role, permission):
        raise HTTPException(status_code=403, detail="Not authorized for this client")

    return target


# ── Pydantic Request & Response Schemas ──────────────────────────────────────

class HydrateRequest(BaseModel):
    snapshot_id: int = Field(..., description="ID of catalog snapshot to hydrate")
    preview_token: str = Field(..., description="HMAC-signed preview token from reuse-check")
    mode: str = Field("instant", description="instant | refresh")
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
    client: Optional[str] = Query(None, description="Client slug context"),
    request: Request = None,
    current_user: auth.User = Depends(auth.require_permission_strict("account:reuse_check", require_client_context=True)),
    session: Session = Depends(get_platform_db),
):
    """Anti-Oracle verified intelligence lookup.
    
    Zero-Knowledge Principle:
    - NEVER reveals source client identity or presence of other clients.
    - Returns identical 'available: false' for unknown companies or non-ready snapshots.
    """
    clean_slug = resolve_target_client(
        request=request,
        user=current_user,
        session=session,
        permission="account:reuse_check",
        explicit_slug=client,
    )

    try:
        result = AccountReuseService.check_reuse_available(session, identifier, clean_slug)
        if not result or not result.get("available"):
            return {"available": False}

        # Generate cryptographically signed HMAC preview token
        token = AccountReuseService.generate_preview_token(
            snapshot_id=result["snapshot_id"],
            target_client_slug=clean_slug,
            user_id=current_user.id,
        )
        result["preview_token"] = token
        return result

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reuse check failed: {str(e)}")


@catalog_router.post("/hydrate")
def hydrate_account(
    req: HydrateRequest,
    request: Request = None,
    current_user: auth.User = Depends(auth.require_permission_strict("account:write", require_client_context=True)),
    session: Session = Depends(get_platform_db),
):
    """Atomically imports verified intelligence into the target client workspace."""
    clean_slug = resolve_target_client(
        request=request,
        user=current_user,
        session=session,
        permission="account:write",
        explicit_slug=req.client_slug,
    )

    # 1. Validate cryptographic preview token (binds snapshot, slug, and user_id)
    if not AccountReuseService.validate_preview_token(
        req.preview_token, clean_slug, req.snapshot_id, current_user.id
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired preview token. Please re-run the intelligence check.",
        )

    # 2. Check approval requirement
    target_client = session.query(Client).filter_by(slug=clean_slug).first()
    if not target_client:
        raise HTTPException(status_code=404, detail=f"Client '{clean_slug}' not found")

    if not target_client.catalog_consume:
        raise HTTPException(
            status_code=403,
            detail="Catalog reuse is not enabled for this organization",
        )

    is_super_admin = getattr(current_user, "role", None) == "super_admin"
    if target_client.reuse_requires_approval and not is_super_admin:
        raise HTTPException(
            status_code=403,
            detail="This organization requires administrator approval to reuse catalog intelligence. "
                   "Please submit a reuse request instead.",
        )

    # 3. Derive idempotency key if omitted
    idem_key = req.idempotency_key or f"hydrate_{clean_slug}_{req.snapshot_id}_{uuid.uuid4().hex[:12]}"

    try:
        hydration_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=clean_slug,
            snapshot_id=req.snapshot_id,
            user_id=current_user.id,
            idempotency_key=idem_key,
            mode=req.mode,
            fee_credits=1,
            approved_by_user_id=current_user.id if is_super_admin else None,
        )
        return hydration_res

    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except AccountQuotaExceededError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except DuplicateAccountError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hydration failed: {str(e)}")


@catalog_router.post("/requests")
def submit_reuse_request(
    req: ReuseRequestCreate,
    request: Request = None,
    current_user: auth.User = Depends(auth.require_permission_strict("account:reuse_request", require_client_context=True)),
    session: Session = Depends(get_platform_db),
):
    """Submits a reuse request for super admin approval when reuse_requires_approval is true."""
    clean_slug = resolve_target_client(
        request=request,
        user=current_user,
        session=session,
        permission="account:reuse_request",
        explicit_slug=req.client_slug,
    )

    if not AccountReuseService.validate_preview_token(
        req.preview_token, clean_slug, req.snapshot_id, current_user.id
    ):
        raise HTTPException(status_code=400, detail="Invalid or expired preview token.")

    target_client = session.query(Client).filter_by(slug=clean_slug).first()
    if not target_client:
        raise HTTPException(status_code=404, detail=f"Client '{clean_slug}' not found")

    if not target_client.catalog_consume:
        raise HTTPException(
            status_code=403,
            detail="Catalog reuse is not enabled for this organization",
        )

    snapshot = session.query(Snapshot).filter_by(id=req.snapshot_id).first()
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Snapshot {req.snapshot_id} not found")

    # Check for existing pending request
    existing_req = session.query(ShareEvent).filter_by(
        snapshot_id=req.snapshot_id,
        target_client_id=target_client.id,
        status="pending_approval",
    ).first()
    if existing_req:
        return {
            "ok": True,
            "request_id": existing_req.id,
            "status": "pending_approval",
            "message": "A reuse request for this account is already pending approval.",
        }

    idem_key = f"req_{target_client.id}_{req.snapshot_id}_{int(datetime.now().timestamp())}"
    share_event = ShareEvent(
        snapshot_id=req.snapshot_id,
        target_client_id=target_client.id,
        mode=req.mode or "instant",
        requested_by=current_user.id,
        reason=req.reason or f"Catalog reuse request for snapshot {req.snapshot_id}",
        status="pending_approval",
        idempotency_key=idem_key,
        fee_credits=1,
    )
    session.add(share_event)
    session.commit()

    return {
        "ok": True,
        "request_id": share_event.id,
        "status": "pending_approval",
        "message": "Reuse request submitted for administrator review.",
    }


@catalog_router.get("/requests")
def list_reuse_requests(
    client: Optional[str] = Query(None, description="Filter by client slug"),
    status: Optional[str] = Query(None, description="Filter by status (pending_approval, done, rejected)"),
    request: Request = None,
    current_user: auth.User = Depends(auth.require_permission_strict("account:reuse_request", require_client_context=True)),
    session: Session = Depends(get_platform_db),
):
    """Lists intelligence reuse requests."""
    is_super = getattr(current_user, "role", None) == "super_admin"
    if not is_super:
        clean_slug = resolve_target_client(
            request=request,
            user=current_user,
            session=session,
            permission="account:reuse_request",
            explicit_slug=client,
        )
    else:
        clean_slug = auth.normalize_client_slug(client) if client else None

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

    if not is_super:
        query = query.filter(Client.slug == clean_slug)
    elif clean_slug:
        query = query.filter(Client.slug == clean_slug)

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
    current_user: auth.User = Depends(auth.require_permission_strict("catalog:view_library")),
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

        # Find which clients currently hold this account
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
    current_user: auth.User = Depends(auth.require_permission_strict("catalog:publish")),
    session: Session = Depends(get_platform_db),
):
    """Publishes a sanitized account snapshot from a tenant schema to platform_catalog."""
    clean_slug = auth.normalize_client_slug(req.source_client_slug)
    try:
        snapshot = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=clean_slug,
            account_id=req.account_id,
            published_by_user_id=current_user.id,
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Publish failed: {str(e)}")


@catalog_router.post("/assign")
def assign_account(
    req: AssignRequest,
    current_user: auth.User = Depends(auth.require_permission_strict("catalog:assign")),
    session: Session = Depends(get_platform_db),
):
    """Super Admin direct assignment drawer: imports catalog intelligence directly into a client."""
    clean_slug = auth.normalize_client_slug(req.target_client_slug)
    idem_key = f"assign_{clean_slug}_{req.snapshot_id}_{int(datetime.now().timestamp())}"

    try:
        res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=clean_slug,
            snapshot_id=req.snapshot_id,
            user_id=current_user.id,
            idempotency_key=idem_key,
            mode=req.mode,
            fee_credits=0,  # Super admin direct assign is 0 fee by default
            reason=req.reason or f"Super admin direct assignment to {clean_slug}",
            approved_by_user_id=current_user.id,
            is_admin_assign=True,
        )
        return res
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except AccountQuotaExceededError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except DuplicateAccountError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Direct assignment failed: {str(e)}")


@catalog_router.post("/requests/{request_id}/approve")
def approve_reuse_request(
    request_id: int,
    current_user: auth.User = Depends(auth.require_permission_strict("catalog:approve")),
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

    target_client = session.query(Client).filter_by(id=event.target_client_id).first()
    if not target_client:
        raise HTTPException(status_code=404, detail="Target client not found")

    try:
        res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=target_client.slug,
            snapshot_id=event.snapshot_id,
            user_id=event.requested_by or current_user.id,
            idempotency_key=event.idempotency_key,
            mode=event.mode,
            fee_credits=event.fee_credits,
            reason=event.reason,
            approved_by_user_id=current_user.id,
        )
        return {
            "ok": True,
            "status": "approved",
            "request_id": event.id,
            "account_id": res["account_id"],
            "counts": res["counts"],
        }
    except Exception as e:
        event.status = "failed"
        event.error = str(e)[:2000]
        session.commit()
        raise HTTPException(status_code=400, detail=f"Approval hydration failed: {str(e)}")


@catalog_router.post("/requests/{request_id}/reject")
def reject_reuse_request(
    request_id: int,
    req: RejectRequest = None,
    current_user: auth.User = Depends(auth.require_permission_strict("catalog:approve")),
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
    session.commit()

    return {"ok": True, "request_id": event.id, "status": "rejected"}


@catalog_router.post("/share-events/{share_event_id}/rollback")
def rollback_share_event(
    share_event_id: int,
    req: RollbackRequest = None,
    current_user: auth.User = Depends(auth.require_permission_strict("catalog:rollback")),
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
            admin_user_id=current_user.id,
            reason=req.reason if req else None,
        )
        return res
    except RollbackBlockedError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "breakdown": getattr(e, "breakdown", {}),
            },
        )
    except AccountReuseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")
