"""Client Router — Multi-tenant client management endpoints.

Endpoints for super_admins and client_admins to manage:
- Clients (list, create, detail, update, soft-delete, schema provision)
- Client Users (list, assign, revoke, role update)
- Client Credit & Account quotas
"""

import re
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text, func, or_

import auth
from db.connection import engine, SessionLocal
from db.models import Client, UserClientAccess, User, Account
from db.provisioning import SchemaProvisioningService
from db.routing import scoped_client_session
from serializers.account_serializer import slugify

client_router = APIRouter(prefix="/api/clients", tags=["Clients & Multi-Tenancy"])


# ── Pydantic Request Schemas ─────────────────────────────────────────────────

class ClientCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=500, description="Organization name")
    slug: Optional[str] = Field(None, max_length=100, description="Unique slug for schema name")
    legal_name: Optional[str] = Field(None, max_length=500)
    plan_tier: Optional[str] = Field("standard", description="standard | enterprise")
    max_accounts: Optional[int] = Field(20, ge=1, le=1000)
    api_credits_total: Optional[int] = Field(100000, ge=0)


class ClientUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=500)
    legal_name: Optional[str] = Field(None, max_length=500)
    plan_tier: Optional[str] = None
    max_accounts: Optional[int] = Field(None, ge=1, le=1000)
    is_active: Optional[bool] = None
    api_credits_total: Optional[int] = Field(None, ge=0)
    api_credits_used: Optional[int] = Field(None, ge=0)


class ClientAccountCreateRequest(BaseModel):
    company_name: str
    domain: Optional[str] = None
    website_url: Optional[str] = None
    legal_name: Optional[str] = None
    stock_symbol: Optional[str] = None
    sec_cik: Optional[str] = None
    company_type: Optional[str] = "Public"
    headquarters_location: Optional[str] = None
    industry: Optional[str] = None

class ClientUserAddRequest(BaseModel):
    user_id: int
    role: str = Field("analyst", description="client_admin | analyst | viewer")


class ClientUserUpdateRequest(BaseModel):
    role: str = Field(..., description="client_admin | analyst | viewer")

class ClientCreditAdjustRequest(BaseModel):
    amount: int = Field(..., description="Amount of credits to add or set")
    action: str = Field("add", description="add | set")
    reason: Optional[str] = Field("Manual administrative adjustment", max_length=500)


# ── Helper Functions ─────────────────────────────────────────────────────────

def _get_client_stats(session, schema_name: str) -> Dict[str, int]:
    """Inspects row counts in the client's dedicated schema safely without aborting transactions."""
    stats = {"accounts_count": 0, "personas_count": 0, "lobs_count": 0, "runs_count": 0}
    if not schema_name:
        return stats
    try:
        # Check if schema exists in PostgreSQL before querying to prevent InFailedSqlTransaction
        schema_exists = session.execute(
            text("SELECT exists(SELECT 1 FROM information_schema.schemata WHERE schema_name = :s)"),
            {"s": schema_name}
        ).scalar()
        if not schema_exists:
            return stats
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return stats

    for key, table in [
        ("accounts_count", "accounts"),
        ("personas_count", "personas"),
        ("lobs_count", "lobs"),
        ("runs_count", "pipeline_runs"),
    ]:
        try:
            with session.begin_nested():
                cnt = session.execute(text(f'SELECT count(*) FROM "{schema_name}"."{table}"')).scalar()
                stats[key] = cnt or 0
        except Exception:
            pass
    return stats


def _serialize_client(c: Client, stats: Optional[Dict[str, int]] = None, users_count: int = 0) -> Dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "slug": c.slug,
        "legal_name": c.legal_name,
        "schema_name": c.schema_name,
        "plan_tier": c.plan_tier,
        "is_active": c.is_active,
        "max_accounts": c.max_accounts,
        "api_credits_total": c.api_credits_total,
        "api_credits_used": c.api_credits_used,
        "accounts_count": stats.get("accounts_count", 0) if stats else 0,
        "personas_count": stats.get("personas_count", 0) if stats else 0,
        "lobs_count": stats.get("lobs_count", 0) if stats else 0,
        "runs_count": stats.get("runs_count", 0) if stats else 0,
        "users_count": users_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


# ── Client CRUD Endpoints ────────────────────────────────────────────────────

@client_router.get("", tags=["Clients & Multi-Tenancy"])
def list_clients(current_user: User = Depends(auth.get_current_user)):
    """List all clients. Super admins see all; regular users see assigned clients."""
    session = SessionLocal()
    try:
        if not auth.AUTH_ENFORCED or getattr(current_user, "role", None) == "super_admin":
            clients = session.query(Client).order_by(Client.id.asc()).all()
        else:
            clients = (
                session.query(Client)
                .join(UserClientAccess, UserClientAccess.client_id == Client.id)
                .filter(UserClientAccess.user_id == current_user.id, Client.is_active == True)
                .order_by(Client.name.asc())
                .all()
            )

        results = []
        for c in clients:
            stats = _get_client_stats(session, c.schema_name)
            users_count = session.query(func.count(UserClientAccess.id)).filter_by(client_id=c.id).scalar() or 0
            results.append(_serialize_client(c, stats, users_count))

        return {"clients": results, "total": len(results)}
    finally:
        session.close()


@client_router.post("", tags=["Clients & Multi-Tenancy"], status_code=201)
def create_client(
    body: ClientCreateRequest,
    current_user: User = Depends(auth.require_role("super_admin")),
):
    """Creates a new client and provisions its isolated PostgreSQL schema."""
    session = SessionLocal()
    try:
        # 1. Resolve & validate slug
        raw_slug = body.slug or slugify(body.name).replace("-", "_")
        slug = SchemaProvisioningService.validate_slug(raw_slug)

        # 2. Check collisions
        existing = session.query(Client).filter_by(slug=slug).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Client slug '{slug}' is already taken.")

        schema_name = SchemaProvisioningService.get_schema_name(slug)

        # 3. Create client record in public
        client = Client(
            name=body.name.strip(),
            slug=slug,
            legal_name=(body.legal_name or body.name).strip(),
            schema_name=schema_name,
            plan_tier=body.plan_tier or "standard",
            is_active=True,
            max_accounts=body.max_accounts or 20,
            api_credits_total=body.api_credits_total if body.api_credits_total is not None else 100000,
            api_credits_used=0,
            created_by_id=current_user.id if current_user else None,
        )
        session.add(client)
        session.flush()

        # 4. Provision dedicated PostgreSQL schema with atomic rollback
        try:
            SchemaProvisioningService.create_client_schema(engine, slug)
        except Exception as prov_err:
            session.rollback()
            try:
                SchemaProvisioningService.drop_client_schema(engine, slug)
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Failed to provision client schema: {str(prov_err)}")

        # 5. Grant creator user client_admin access
        if current_user:
            session.add(UserClientAccess(
                user_id=current_user.id,
                client_id=client.id,
                role="client_admin",
                granted_by_id=current_user.id,
            ))

        try:
            session.commit()
        except Exception as commit_err:
            session.rollback()
            try:
                SchemaProvisioningService.drop_client_schema(engine, slug)
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Failed to commit client: {str(commit_err)}")
        auth.log_audit(session, getattr(current_user, "id", None), "client_created", details={"slug": slug, "name": client.name})

        stats = _get_client_stats(session, schema_name)
        return _serialize_client(client, stats, users_count=1)
    finally:
        session.close()


# ── Multi-Tenant System Overview KPIs ───────────────────────────────────────

@client_router.get("/overview-kpis", tags=["Clients & Multi-Tenancy"])
def get_clients_overview_kpis(current_user: User = Depends(auth.get_current_user)):
    """Returns aggregated multi-tenant KPIs: total clients, active schemas, accounts, and credits."""
    session = SessionLocal()
    try:
        clients = session.query(Client).all()
        total_clients = len(clients)
        active_clients = sum(1 for c in clients if c.is_active)
        total_credits_alloc = sum(c.api_credits_total or 0 for c in clients)
        total_credits_used = sum(c.api_credits_used or 0 for c in clients)
        
        # Aggregate account counts across active client schemas
        total_accounts = 0
        for c in clients:
            if c.is_active:
                stats = _get_client_stats(session, c.schema_name)
                total_accounts += stats.get("accounts_count", 0)

        utilization_pct = round((total_credits_used / max(total_credits_alloc, 1)) * 100, 1)

        return {
            "total_clients": total_clients,
            "active_clients": active_clients,
            "total_accounts": total_accounts,
            "total_credits_allocated": total_credits_alloc,
            "total_credits_used": total_credits_used,
            "credit_utilization_pct": utilization_pct,
        }
    finally:
        session.close()


@client_router.get("/platform-users", tags=["Clients & Multi-Tenancy"])
def list_platform_users(current_user: User = Depends(auth.require_role("super_admin", "client_admin"))):
    """Lists registered platform users for assignment to client workspaces."""
    session = SessionLocal()
    try:
        users = session.query(User).filter_by(is_active=True).order_by(User.full_name.asc()).all()
        return {
            "users": [
                {
                    "id": u.id,
                    "email": u.email,
                    "full_name": u.full_name,
                    "role": u.role,
                    "is_active": u.is_active,
                }
                for u in users
            ],
            "total": len(users)
        }
    finally:
        session.close()



@client_router.get("/{slug}", tags=["Clients & Multi-Tenancy"])
def get_client(slug: str, current_user: User = Depends(auth.get_current_user)):
    """Retrieves detailed intelligence & resource quotas for a specific client."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        stats = _get_client_stats(session, client.schema_name)
        users_count = session.query(func.count(UserClientAccess.id)).filter_by(client_id=client.id).scalar() or 0
        return _serialize_client(client, stats, users_count)
    finally:
        session.close()


@client_router.patch("/{slug}", tags=["Clients & Multi-Tenancy"])
def update_client(
    slug: str,
    body: ClientUpdateRequest,
    current_user: User = Depends(auth.require_role("super_admin")),
):
    """Updates client plan, quotas, active state, or credits."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if body.name is not None:
            client.name = body.name.strip()
        if body.legal_name is not None:
            client.legal_name = body.legal_name.strip()
        if body.plan_tier is not None:
            client.plan_tier = body.plan_tier.strip()
        if body.max_accounts is not None:
            client.max_accounts = body.max_accounts
        if body.is_active is not None:
            client.is_active = body.is_active
        if body.api_credits_total is not None:
            client.api_credits_total = body.api_credits_total
        if body.api_credits_used is not None:
            client.api_credits_used = body.api_credits_used

        session.commit()
        auth.log_audit(session, getattr(current_user, "id", None), "client_updated", details={"slug": slug})

        stats = _get_client_stats(session, client.schema_name)
        users_count = session.query(func.count(UserClientAccess.id)).filter_by(client_id=client.id).scalar() or 0
        return _serialize_client(client, stats, users_count)
    finally:
        session.close()


@client_router.delete("/{slug}", tags=["Clients & Multi-Tenancy"])
def delete_client(
    slug: str,
    current_user: User = Depends(auth.require_role("super_admin")),
):
    """Soft deletes (deactivates) a client organization."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")
        if slug == "stradit_india":
            raise HTTPException(status_code=400, detail="Cannot deactivate the primary platform client.")

        client.is_active = False
        session.commit()
        auth.log_audit(session, getattr(current_user, "id", None), "client_deactivated", details={"slug": slug})
        return {"ok": True, "message": f"Client '{slug}' has been deactivated."}
    finally:
        session.close()


@client_router.post("/{slug}/provision", tags=["Clients & Multi-Tenancy"])
def reprovision_client_schema(
    slug: str,
    current_user: User = Depends(auth.require_role("super_admin")),
):
    """Idempotently re-provisions or repairs a client's PostgreSQL schema."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        schema_name = SchemaProvisioningService.create_client_schema(engine, slug)
        stats = _get_client_stats(session, schema_name)
        return {"ok": True, "schema_name": schema_name, "stats": stats}
    finally:
        session.close()


# ── Client User Management ───────────────────────────────────────────────────

@client_router.get("/{slug}/users", tags=["Clients & Multi-Tenancy"])
def list_client_users(
    slug: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Lists users with access to this client organization."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        rows = (
            session.query(UserClientAccess, User)
            .join(User, User.id == UserClientAccess.user_id)
            .filter(UserClientAccess.client_id == client.id)
            .all()
        )

        members = []
        for grant, user in rows:
            members.append({
                "grant_id": grant.id,
                "user_id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "platform_role": user.role,
                "client_role": grant.role,
                "is_active": user.is_active,
                "granted_at": grant.granted_at.isoformat() if grant.granted_at else None,
            })

        return {"client_slug": slug, "members": members, "total": len(members)}
    finally:
        session.close()


@client_router.post("/{slug}/users", tags=["Clients & Multi-Tenancy"])
def add_client_user(
    slug: str,
    body: ClientUserAddRequest,
    current_user: User = Depends(auth.require_role("super_admin", "client_admin")),
):
    """Assigns user access to this client with specified role."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        user = session.query(User).filter_by(id=body.user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="Target user not found.")

        if body.role not in auth.CLIENT_ROLES:
            raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(auth.CLIENT_ROLES)}")

        existing = session.query(UserClientAccess).filter_by(client_id=client.id, user_id=user.id).first()
        if existing:
            existing.role = body.role
        else:
            grant = UserClientAccess(
                user_id=user.id,
                client_id=client.id,
                role=body.role,
                granted_by_id=getattr(current_user, "id", None),
            )
            session.add(grant)

        session.commit()
        return {"ok": True, "client_slug": slug, "user_id": user.id, "role": body.role}
    finally:
        session.close()


@client_router.delete("/{slug}/users/{user_id}", tags=["Clients & Multi-Tenancy"])
def remove_client_user(
    slug: str,
    user_id: int,
    current_user: User = Depends(auth.require_role("super_admin", "client_admin")),
):
    """Revokes user access from this client organization."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        grant = session.query(UserClientAccess).filter_by(client_id=client.id, user_id=user_id).first()
        if not grant:
            raise HTTPException(status_code=404, detail="Access grant not found.")

        session.delete(grant)
        session.commit()
        return {"ok": True, "message": "User access revoked."}
    finally:
        session.close()


@client_router.get("/{slug}/accounts", tags=["Clients & Multi-Tenancy"])
def list_client_accounts(
    slug: str,
    current_user: User = Depends(auth.get_current_user),
):
    """Lists enterprise accounts contained inside a specific client organization schema."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        schema_name = client.schema_name
        # Fetch accounts with per-account LOB and Persona counts
        rows = session.execute(
            text(f'SELECT a.id, a.display_name, a.legal_name, a.primary_domain, a.website_url, a.created_at, '
                 f'(SELECT count(*) FROM "{schema_name}"."lobs" l WHERE l.account_id = a.id) as lobs_count, '
                 f'(SELECT count(*) FROM "{schema_name}"."personas" p WHERE p.account_id = a.id) as personas_count '
                 f'FROM "{schema_name}"."accounts" a ORDER BY a.id ASC')
        ).fetchall()

        accounts = []
        for r in rows:
            accounts.append({
                "id": r[0],
                "display_name": r[1],
                "legal_name": r[2],
                "primary_domain": r[3],
                "website_url": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
                "lobs_count": r[6] or 0,
                "personas_count": r[7] or 0,
            })

        return {"client_slug": slug, "accounts": accounts, "total": len(accounts)}
    finally:
        session.close()

@client_router.post("/{slug}/accounts", tags=["Clients & Multi-Tenancy"], status_code=201)
def add_client_account(
    slug: str,
    body: ClientAccountCreateRequest,
    current_user: User = Depends(auth.require_role("super_admin", "client_admin")),
):
    """Enrolls and creates a new enterprise account inside this client's schema.
    Enforces the client's max_accounts quota limit."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug, is_active=True).first()
        if not client:
            raise HTTPException(status_code=404, detail=f"Client '{slug}' not found or inactive.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        with scoped_client_session(slug) as c_session:
            current_cnt = c_session.query(Account).count()
            if current_cnt >= client.max_accounts:
                raise HTTPException(
                    status_code=403,
                    detail=f"Account limit reached ({current_cnt}/{client.max_accounts}) for client '{client.name}'. Upgrade plan tier to add more accounts."
                )

            clean_name = body.company_name.strip()
            acct_slug = slugify(clean_name)
            clean_dom = (
                body.domain.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0].strip().lower()
                if body.domain
                else None
            )
            if clean_dom and clean_dom.startswith("www."):
                clean_dom = clean_dom[4:]

            # Check if account already exists
            existing = c_session.query(Account).filter(
                or_(
                    Account.key == acct_slug,
                    Account.display_name.ilike(clean_name),
                    Account.domain == clean_dom if clean_dom else False,
                    Account.primary_domain == clean_dom if clean_dom else False,
                )
            ).first()

            if existing:
                return {
                    "ok": True,
                    "status": "exists",
                    "account": {
                        "id": existing.id,
                        "display_name": existing.display_name,
                        "primary_domain": existing.primary_domain,
                    },
                    "message": f"Account '{existing.display_name}' already exists in client '{slug}'."
                }

            new_acct = Account(
                key=acct_slug,
                display_name=clean_name,
                legal_name=body.legal_name.strip() if body.legal_name else clean_name,
                domain=clean_dom,
                primary_domain=clean_dom,
                website_url=body.website_url.strip() if body.website_url else (f"https://{clean_dom}" if clean_dom else None),
                stock_symbol=body.stock_symbol.strip().upper() if body.stock_symbol else None,
                sec_cik=body.sec_cik.strip() if body.sec_cik else None,
                company_type=body.company_type.strip() if body.company_type else "Public",
                headquarters_location=body.headquarters_location.strip() if body.headquarters_location else None,
                industries=[body.industry.strip()] if body.industry else [],
            )
            c_session.add(new_acct)
            c_session.commit()
            c_session.refresh(new_acct)

            return {
                "ok": True,
                "status": "created",
                "client_slug": slug,
                "account": {
                    "id": new_acct.id,
                    "display_name": new_acct.display_name,
                    "primary_domain": new_acct.primary_domain,
                    "key": new_acct.key,
                },
                "quota_used": current_cnt + 1,
                "quota_max": client.max_accounts,
            }
    finally:
        session.close()


@client_router.delete("/{slug}/accounts/{account_id}", tags=["Clients & Multi-Tenancy"])
def remove_client_account(
    slug: str,
    account_id: int,
    current_user: User = Depends(auth.require_role("super_admin", "client_admin")),
):
    """Removes an enterprise account and child hierarchies from this client's schema."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug, is_active=True).first()
        if not client:
            raise HTTPException(status_code=404, detail=f"Client '{slug}' not found or inactive.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        with scoped_client_session(slug) as c_session:
            acct = c_session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail=f"Account {account_id} not found in client '{slug}'.")

            c_session.delete(acct)
            c_session.commit()
            return {"ok": True, "message": f"Account {account_id} deleted from client '{slug}'."}
    finally:
        session.close()


@client_router.get("/{slug}/users/{user_id}/accounts", tags=["Clients & Multi-Tenancy"])
def list_client_user_account_access(
    slug: str,
    user_id: int,
    current_user: User = Depends(auth.require_role("super_admin", "client_admin")),
):
    """Lists all accounts in this client and whether this specific user has access."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug, is_active=True).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        target_user = session.query(User).filter_by(id=user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="Target user not found.")

        with scoped_client_session(slug) as c_session:
            granted_ids = set(auth.get_granted_account_ids(c_session, user_id))
            accounts = c_session.query(Account).order_by(Account.display_name).all()
            return {
                "client_slug": slug,
                "user_id": user_id,
                "role": target_user.role,
                "accounts": [
                    {
                        "id": a.id,
                        "display_name": a.display_name,
                        "primary_domain": a.primary_domain,
                        "granted": a.id in granted_ids,
                    }
                    for a in accounts
                ],
            }
    finally:
        session.close()


@client_router.post("/{slug}/users/{user_id}/accounts/{account_id}", tags=["Clients & Multi-Tenancy"])
def grant_client_user_account_access(
    slug: str,
    user_id: int,
    account_id: int,
    current_user: User = Depends(auth.require_role("super_admin", "client_admin")),
):
    """Grants a user access to an account inside this client's schema."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug, is_active=True).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        target_user = session.query(User).filter_by(id=user_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="Target user not found in platform.")

        with scoped_client_session(slug) as c_session:
            account = c_session.query(Account).filter_by(id=account_id).first()
            if not account:
                raise HTTPException(status_code=404, detail=f"Account {account_id} not found in client '{slug}'.")

            created = auth.grant_account_access(c_session, user_id, account_id, current_user.id)
            if created:
                auth.log_audit(
                    session, current_user.id, "client_account_access_granted", target_user_id=user_id,
                    details={"client_slug": slug, "account_id": account_id, "account_name": account.display_name}
                )
            return {"ok": True, "client_slug": slug, "user_id": user_id, "account_id": account_id}
    finally:
        session.close()


@client_router.delete("/{slug}/users/{user_id}/accounts/{account_id}", tags=["Clients & Multi-Tenancy"])
def revoke_client_user_account_access(
    slug: str,
    user_id: int,
    account_id: int,
    current_user: User = Depends(auth.require_role("super_admin", "client_admin")),
):
    """Revokes a user's access to an account inside this client's schema."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug, is_active=True).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        with scoped_client_session(slug) as c_session:
            removed = auth.revoke_account_access(c_session, user_id, account_id)
            if removed:
                auth.log_audit(
                    session, current_user.id, "client_account_access_revoked", target_user_id=user_id,
                    details={"client_slug": slug, "account_id": account_id}
                )
            return {"ok": True, "client_slug": slug, "user_id": user_id, "account_id": account_id}
    finally:
        session.close()




# ── Schema Health & Self-Healing Diagnostics ─────────────────────────────────

@client_router.get("/{slug}/health", tags=["Clients & Multi-Tenancy"])
def get_client_schema_health(slug: str, current_user: User = Depends(auth.get_current_user)):
    """Verifies schema table integrity, sequences, foreign keys, and storage metrics."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")
        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        report = SchemaProvisioningService.verify_schema_integrity(engine, slug)
        report["client_name"] = client.name
        report["plan_tier"] = client.plan_tier
        report["is_active"] = client.is_active
        return report
    finally:
        session.close()


@client_router.post("/{slug}/repair", tags=["Clients & Multi-Tenancy"])
def repair_client_schema(
    slug: str,
    current_user: User = Depends(auth.require_role("super_admin")),
):
    """Self-heals and idempotently provisions any missing tables, sequences, or constraints."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        repair_result = SchemaProvisioningService.repair_client_schema(engine, slug)
        auth.log_audit(session, getattr(current_user, "id", None), "client_schema_repaired", details={"slug": slug, "result": repair_result})
        return {"ok": True, "slug": slug, "repair": repair_result}
    finally:
        session.close()


# ── Atomic Credit Top-Up / Budget Modification ───────────────────────────────

@client_router.post("/{slug}/credits/adjust", tags=["Clients & Multi-Tenancy"])
def adjust_client_credits(
    slug: str,
    body: ClientCreditAdjustRequest,
    current_user: User = Depends(auth.require_role("super_admin")),
):
    """Atomically top up or adjust credit allocation for a client organization."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        old_total = client.api_credits_total or 0
        if body.action == "add":
            new_total = max(0, old_total + body.amount)
        elif body.action == "set":
            new_total = max(0, body.amount)
        else:
            raise HTTPException(status_code=400, detail="Action must be 'add' or 'set'.")

        client.api_credits_total = new_total
        session.commit()

        auth.log_audit(session, getattr(current_user, "id", None), "client_credits_adjusted", details={
            "slug": slug,
            "old_total": old_total,
            "new_total": new_total,
            "reason": body.reason,
        })

        return {
            "ok": True,
            "slug": slug,
            "api_credits_total": client.api_credits_total,
            "api_credits_used": client.api_credits_used,
            "remaining": (client.api_credits_total or 0) - (client.api_credits_used or 0),
        }
    finally:
        session.close()


@client_router.patch("/{slug}/users/{user_id}", tags=["Clients & Multi-Tenancy"])
def update_client_user_role(
    slug: str,
    user_id: int,
    body: ClientUserUpdateRequest,
    current_user: User = Depends(auth.require_role("super_admin", "client_admin")),
):
    """Updates assigned user role for this client organization."""
    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=slug).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        if not auth.user_has_client_access(session, current_user, slug):
            raise HTTPException(status_code=403, detail="Access denied to this client organization.")

        if body.role not in auth.CLIENT_ROLES:
            raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(auth.CLIENT_ROLES)}")

        grant = session.query(UserClientAccess).filter_by(client_id=client.id, user_id=user_id).first()
        if not grant:
            raise HTTPException(status_code=404, detail="User is not assigned to this client organization.")

        grant.role = body.role
        session.commit()
        return {"ok": True, "slug": slug, "user_id": user_id, "role": body.role}
    finally:
        session.close()
