"""TenantRequestContext — Unified Multi-Tenant Security & Request Isolation Architecture.

Encapsulates the tenant security context resolved exactly once per request:
1. Canonical slug resolution and validation
2. Active client registry lookup and status verification
3. Effective RBAC role derivation
4. Strict permission check
5. Bounded database schema identifier binding
"""

from dataclasses import dataclass
import json
import logging
from typing import Any, Literal, Optional, Union
import uuid

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

import auth
from db.connection import SessionLocal
from db.models.client import Client
from db.tenant import schema_for, validate_slug

logger = logging.getLogger(__name__)


def get_platform_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@dataclass(frozen=True)
class ClientFlags:
    catalog_publish: bool
    catalog_consume: bool
    reuse_requires_approval: bool
    status: str
    max_accounts: int


@dataclass(frozen=True)
class TenantRequestContext:
    user_id: int
    user_role: str            # global role
    effective_role: str       # role in THIS client (super_admin | client_admin | analyst | viewer)
    client_id: int
    client_slug: str          # canonical, validated
    schema: str               # schema_for(client_slug), computed once
    is_super_admin: bool
    client_flags: ClientFlags # catalog_publish, catalog_consume, reuse_requires_approval, status, max_accounts
    correlation_id: str       # uuid4().hex[:12], also put on the response header X-Correlation-ID

    @classmethod
    def for_client(
        cls,
        session: Session,
        client: Union[Client, str, int],
        user_id: int = 1,
        user_role: str = "super_admin",
        effective_role: str = "super_admin",
        correlation_id: Optional[str] = None,
    ) -> "TenantRequestContext":
        """Factory creating TenantRequestContext for an existing client without re-resolving request parameters."""
        corr_id = correlation_id or uuid.uuid4().hex[:12]
        if isinstance(client, Client):
            client_obj = client
        elif isinstance(client, int):
            client_obj = session.query(Client).filter_by(id=client).first()
        else:
            norm_slug = auth.normalize_client_slug(str(client))
            client_obj = session.query(Client).filter_by(slug=norm_slug).first()

        if not client_obj:
            raise ValueError(f"Client '{client}' not found in platform registry")

        clean_slug = validate_slug(client_obj.slug)
        flags = ClientFlags(
            catalog_publish=bool(client_obj.catalog_publish),
            catalog_consume=bool(client_obj.catalog_consume),
            reuse_requires_approval=bool(client_obj.reuse_requires_approval),
            status=str(client_obj.status),
            max_accounts=int(client_obj.max_accounts or 0),
        )
        return cls(
            user_id=user_id,
            user_role=user_role,
            effective_role=effective_role,
            client_id=client_obj.id,
            client_slug=clean_slug,
            schema=schema_for(clean_slug),
            is_super_admin=(user_role == "super_admin"),
            client_flags=flags,
            correlation_id=corr_id,
        )


@dataclass(frozen=True)
class PlatformRequestContext:
    user_id: int
    user_role: str
    is_super_admin: bool
    correlation_id: str


def tenant_context(
    permission: str,
    *,
    allow_explicit_for_super_admin: bool = True,
    explicit_slug_from: Literal["none", "body", "query"] = "none",
    allow_optional_client_for_super_admin: bool = False,
):
    """FastAPI dependency factory resolving TenantRequestContext once per request."""

    async def dependency(
        request: Request,
        response: Response,
        session: Session = Depends(get_platform_db),
        user: auth.User = Depends(auth.get_current_user_strict),
    ) -> TenantRequestContext:
        correlation_id = uuid.uuid4().hex[:12]
        response.headers["X-Correlation-ID"] = correlation_id

        # 1. Resolve explicit slug based on strategy
        explicit_slug = None
        if explicit_slug_from == "query":
            explicit_slug = request.query_params.get("client") or request.query_params.get("client_slug")
        elif explicit_slug_from == "body":
            try:
                body_bytes = await request.body()
                if body_bytes:
                    body_data = json.loads(body_bytes)
                    if isinstance(body_data, dict):
                        explicit_slug = (
                            body_data.get("client_slug")
                            or body_data.get("client")
                            or body_data.get("target_client_slug")
                        )
            except Exception:
                explicit_slug = None

        ctx_slug = auth.resolve_request_client_slug(request, user)
        is_super = getattr(user, "role", None) == "super_admin"

        if is_super:
            if allow_explicit_for_super_admin and explicit_slug:
                target = auth.normalize_client_slug(explicit_slug)
            else:
                target = ctx_slug

            if not target:
                if allow_optional_client_for_super_admin:
                    return TenantRequestContext(
                        user_id=user.id,
                        user_role=user.role,
                        effective_role="super_admin",
                        client_id=0,
                        client_slug="",
                        schema="",
                        is_super_admin=True,
                        client_flags=ClientFlags(
                            catalog_publish=True,
                            catalog_consume=True,
                            reuse_requires_approval=False,
                            status="active",
                            max_accounts=999999,
                        ),
                        correlation_id=correlation_id,
                    )
                raise HTTPException(status_code=400, detail="Target client slug required")
        else:
            if not ctx_slug:
                raise HTTPException(
                    status_code=400,
                    detail="Client context required (pass X-Client-Slug header or client parameter)",
                )
            if explicit_slug:
                norm_explicit = auth.normalize_client_slug(explicit_slug)
                if norm_explicit != ctx_slug:
                    raise HTTPException(
                        status_code=403,
                        detail="Target client does not match your active client context",
                    )
            target = ctx_slug

        # 2. Load the Client row (must be active)
        client = session.query(Client).filter_by(slug=target).first()
        if not client:
            raise HTTPException(status_code=404, detail=f"Target client '{target}' not found")
        if not client.is_active or client.status != "active":
            raise HTTPException(
                status_code=403,
                detail=f"Target client '{target}' is not active (status='{client.status}')",
            )

        # 3. Compute effective_role for this client slug
        if is_super:
            effective_role = "super_admin"
        else:
            effective_role = auth.get_effective_role(session, user, target, enforce=True)

        # 4. Check permission
        if not effective_role or not auth.has_permission(effective_role, permission):
            raise HTTPException(status_code=403, detail="Not authorized for this client")

        # 5. Build and return the frozen context
        clean_slug = validate_slug(target)
        client_flags = ClientFlags(
            catalog_publish=bool(client.catalog_publish),
            catalog_consume=bool(client.catalog_consume),
            reuse_requires_approval=bool(client.reuse_requires_approval),
            status=str(client.status),
            max_accounts=int(client.max_accounts or 0),
        )
        return TenantRequestContext(
            user_id=user.id,
            user_role=user.role,
            effective_role=effective_role,
            client_id=client.id,
            client_slug=clean_slug,
            schema=schema_for(clean_slug),
            is_super_admin=is_super,
            client_flags=client_flags,
            correlation_id=correlation_id,
        )

    return dependency


def platform_context(permission: str):
    """FastAPI dependency for super-admin platform routes carrying correlation_id."""

    def dependency(
        response: Response,
        user: auth.User = Depends(auth.get_current_user_strict),
    ) -> PlatformRequestContext:
        correlation_id = uuid.uuid4().hex[:12]
        response.headers["X-Correlation-ID"] = correlation_id
        is_super = getattr(user, "role", None) == "super_admin"
        if not is_super or not auth.has_permission(user.role, permission):
            raise HTTPException(status_code=403, detail="Platform administrator privileges required")
        return PlatformRequestContext(
            user_id=user.id,
            user_role=user.role,
            is_super_admin=True,
            correlation_id=correlation_id,
        )

    return dependency
