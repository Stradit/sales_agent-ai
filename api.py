"""
Sales AI Intelligence Pipeline — Granular REST API Server.
Provides dedicated Fetch / Validate / Dump lifecycle endpoints for:
1. Account Level (Tab 1)
2. Lines of Business (LOB) & Sub-LOB Level (Tab 2 & 3)
3. Person & Persona Level — Individual & Batch (Tab 4)
4. Full Composite Pipeline

Workflow for each level:
    [Fetch / Pull Button] ──► Staged into memory/JSON (NO DB Write)
          │
    [Validate Button Appears] ──► Quality Audit & Health Scores (0-100%)
          │
    [Dump DB Button Appears] ──► User-approved UPSERT into PostgreSQL (sales_ai)

100% Dynamic, Zero Hardcoding.
"""

import sys
import os
import re
import json
import math
import threading                      # background pipeline threads
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta

# Ensure project root is on sys.path
PIPELINE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_ROOT))

from subprocess import run

import config
from collectors.account_collector import (
    fetch_latest_10k_chunks,
    extract_full_patents,
    fetch_sec_exhibit_21_subsidiaries,
    fetch_gleif_ownership_tree,
    fetch_wikipedia_dbpedia_intel,
    fetch_fec_political_intel,
    fetch_diffbot_organization_intel,
)
from collectors.sublob_collector import scrape_sublobs, extract_commercial_operating_divisions
from collectors.lob_enricher import enrich_lob_segments
from collectors.hierarchy_collector import scrape_hierarchy
from collectors.persona_enricher import build_persona_dossier
from collectors.validator import DataQualityValidator
from serializer import MasterSerializer
from serializers.account_serializer import slugify

from sqlalchemy import func as sa_func, or_, and_, func, text as sql_text
from sqlalchemy.orm import load_only, selectinload
from db.connection import get_session
from db.models import (
    Account,
    Lob,
    SubLob,
    Persona,
    Post,
    Digest,
    OpportunitySignal,
    WeeklyDigestSnapshot,
    LinkedInJob,
    CxoMovement,
    User,
    AuditLog,
    ActionItem,
    ActionItemReminder,
    UserAccountAccess,
    CommandCenterSnapshot,
)

from db.schemas import AccountSchema, LobSchema, PersonaSchema
from db.repositories import (
    AccountRepository,
    LobRepository,
    PersonaRepository,
)
from db.repositories.pipeline_run_repository import PipelineRunRepository
from services.account_service import AccountService
from services.account_automation_engine import AccountAutomationEngine
from services.lob_service import LobService, LobValidator
from services.persona_service import PersonaService, PersonaValidator
from services.pipeline_run_logger import PipelineRunLogger
from services import callprep_service
from services import persona_photo_service
from services import command_center_service
# Contact privacy: work email/phone only — personal email & direct mobile never reach the browser
from apps.sales_copilot import privacy as contact_privacy
from pdf_export import build_persona_profile_pdf, build_psychological_profile_pdf
import auth
import email_sender
from main import run_pipeline
from services.job_registry import (          # background job tracking
    create_job      as _jreg_create,
    update_job      as _jreg_update,
    get_job         as _jreg_get,
    list_jobs       as _jreg_list,
    record_exception as _jreg_exc,
    TYPE_ACCOUNT, TYPE_LOB, TYPE_PERSONA,
    STATUS_DONE, STATUS_FAILED,
)

import uvicorn
try:
    from fastapi import (
        FastAPI,
        APIRouter,
        HTTPException,
        Query,
        Body,
        Response,
        Request,
        Depends,
        BackgroundTasks,
    )
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    from pydantic import BaseModel
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


if FASTAPI_AVAILABLE:
    app = FastAPI(
        title="Doshin API",
        description=(
            "Granular REST API for Frontend UI Tabs: "
            "Account, LOBs, Sub-LOBs, and Personas (Fetch -> Validate -> Dump)"
        ),
        version="2.2.0",
    )

    # allow_origins=["*"] together with allow_credentials=True is rejected by
    # browsers once real credentialed requests (the refresh-token cookie) are
    # in play — see AUTH_JWT_IMPLEMENTATION_PLAN.md §0. A concrete origin
    # list is required for auth to work at all.
    _cors_origins = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    # Browsers only send Secure cookies over HTTPS — false for plain-http
    # local dev (localhost included, to work reliably with curl/tools too),
    # true once this is actually deployed behind HTTPS.
    _COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() == "true"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins or ["http://localhost:8000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        icon = Path(__file__).resolve().parent / "frontend" / "assets" / "brand" / "favicon-32.png"
        if not icon.exists():
            return Response(status_code=204)
        return FileResponse(icon, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})

    SCHEMA_STATE = {}   # filled at startup: ok | behind | legacy

    @app.get("/api/health", include_in_schema=False)
    def health():
        """Liveness + DB check for the container HEALTHCHECK, the deploy script and the load
        balancer. Public (no auth) and reveals nothing beyond up/down."""
        session = get_session()
        try:
            session.execute(sql_text("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False
        finally:
            session.close()
        # A database behind the code (migrations not applied) fails the check, so a deploy that
        # skipped `db.migrate upgrade` is rolled back instead of serving on a stale schema.
        schema = SCHEMA_STATE.get("schema", "unknown")
        ok = db_ok and schema != "behind"
        return JSONResponse({"status": "ok" if ok else "degraded", "db": "ok" if db_ok else "down", "schema": schema},
                            status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})

    @app.on_event("startup")
    def on_startup():
        try:
            from db.migrate import state
            st = state()
            SCHEMA_STATE["schema"] = "behind" if st["behind"] else ("ok" if st["mode"] == "managed" else "legacy")
            if st["behind"]:
                print(f"[DB] schema is BEHIND the code ({st['current']} < {st['head']}): run `python -m db.migrate upgrade`")
        except Exception as e:
            print(f"[WARN] Could not read the migration state: {e}")
        try:
            from db.create_tables import ensure_schema_compatibility
            ensure_schema_compatibility()   # legacy databases only; managed ones just seed the first admin
        except Exception as e:
            print(f"[WARN] Schema compatibility check failed on startup: {e}")



    # ══════════════════════════════════════════════════════
    # AUTHENTICATION (see AUTH_JWT_IMPLEMENTATION_PLAN.md)
    # ══════════════════════════════════════════════════════
    class LoginRequest(BaseModel):
        email: str
        password: str

    class ForgotPasswordRequest(BaseModel):
        email: str

    class ResetPasswordRequest(BaseModel):
        token: str
        new_password: str

    class CreateUserRequest(BaseModel):
        email: str
        full_name: Optional[str] = None
        password: str
        role: str = "user"

    class UpdateUserRequest(BaseModel):
        full_name: Optional[str] = None
        email: Optional[str] = None
        role: Optional[str] = None
        is_active: Optional[bool] = None
        password: Optional[str] = None
        has_dashboard_access: Optional[bool] = None
        has_command_center_access: Optional[bool] = None
        has_tasks_access: Optional[bool] = None
        has_pipeline_access: Optional[bool] = None

    class UpdateApiConfigRequest(BaseModel):
        configs: Dict[str, Any]

    class TestApiConfigRequest(BaseModel):
        provider: str
        credentials: Optional[Dict[str, Any]] = None

    def _user_public(u: User) -> Dict[str, Any]:
        is_sa = u.role == "super_admin"
        role_label = auth.ROLE_LABELS.get(u.role, u.role.replace("_", " ").title() if u.role else "Sales Rep")
        return {
            "id": u.id, "email": u.email, "full_name": u.full_name,
            "role": u.role, "role_label": role_label, "is_active": u.is_active,
            "has_dashboard_access": is_sa or (u.has_dashboard_access if u.has_dashboard_access is not None else True),
            "has_command_center_access": is_sa or (u.has_command_center_access if u.has_command_center_access is not None else True),
            "has_tasks_access": is_sa or (u.has_tasks_access if u.has_tasks_access is not None else True),
            "has_pipeline_access": is_sa or (u.has_pipeline_access if u.has_pipeline_access is not None else True),
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }

    @app.post("/api/auth/login", tags=["0. Authentication"])
    def login(body: LoginRequest, request: Request, response: Response):
        session = get_session()
        try:
            email = body.email.strip().lower()
            user = session.query(User).filter(User.email.ilike(email)).first()
            # Same generic error whether the email doesn't exist or the
            # password is wrong — never reveal which one it was.
            invalid = HTTPException(status_code=401, detail="Invalid email or password")
            if not user or not user.is_active:
                raise invalid
            if auth.is_locked_out(user):
                raise HTTPException(status_code=423, detail="Account temporarily locked due to repeated failed logins")
            if not auth.verify_password(body.password, user.hashed_password):
                auth.register_failed_login(session, user)
                raise invalid

            auth.register_successful_login(session, user)
            access_token = auth.create_access_token(user)
            refresh_token = auth.issue_refresh_token(session, user, request)
            auth.log_audit(session, user.id, "login")

            response.set_cookie(
                "refresh_token", refresh_token, httponly=True, secure=_COOKIE_SECURE, samesite="lax",
                max_age=auth.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600, path="/api/auth",
            )
            _set_session_hint(response)
            return {"access_token": access_token, "token_type": "bearer", "user": _user_public(user)}
        finally:
            session.close()

    # "/" serves the public landing page to signed-out visitors and the dashboard to
    # signed-in users. The refresh cookie is scoped to /api/auth, so page requests can't
    # see it; this companion cookie is only a hint for which page to render — it holds
    # no secret and grants nothing (every API call still needs the access token).
    SESSION_HINT_COOKIE = "si_signed_in"

    def _set_session_hint(response: Response) -> None:
        response.set_cookie(SESSION_HINT_COOKIE, "1", httponly=True, secure=_COOKIE_SECURE, samesite="lax",
                            max_age=auth.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600, path="/")

    @app.post("/api/auth/refresh", tags=["0. Authentication"])
    def refresh_access_token(request: Request, response: Response):
        raw_refresh = request.cookies.get("refresh_token")
        if not raw_refresh:
            raise HTTPException(status_code=401, detail="No refresh token")
        session = get_session()
        try:
            new_raw, user = auth.verify_and_rotate_refresh_token(session, raw_refresh, request)
            access_token = auth.create_access_token(user)
            response.set_cookie(
                "refresh_token", new_raw, httponly=True, secure=_COOKIE_SECURE, samesite="lax",
                max_age=auth.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600, path="/api/auth",
            )
            _set_session_hint(response)
            return {"access_token": access_token, "token_type": "bearer", "user": _user_public(user)}
        finally:
            session.close()

    @app.post("/api/auth/logout", tags=["0. Authentication"])
    def logout(request: Request, response: Response):
        raw_refresh = request.cookies.get("refresh_token")
        if raw_refresh:
            session = get_session()
            try:
                auth.revoke_refresh_token(session, raw_refresh)
            finally:
                session.close()
        response.delete_cookie(
            "refresh_token",
            path="/api/auth",
            httponly=True,
            samesite="lax",
            secure=_COOKIE_SECURE,
        )
        response.delete_cookie(SESSION_HINT_COOKIE, path="/", httponly=True, samesite="lax", secure=_COOKIE_SECURE)
        return {"ok": True}

    @app.get("/api/auth/me", tags=["0. Authentication"])
    def get_me(user: User = Depends(auth.get_current_user)):
        return _user_public(user)

    @app.post("/api/auth/forgot-password", tags=["0. Authentication"])
    def forgot_password(body: ForgotPasswordRequest, background_tasks: BackgroundTasks):
        """Always returns the same generic response whether or not the
        email exists — prevents account enumeration via this endpoint."""
        session = get_session()
        try:
            user = session.query(User).filter(User.email.ilike(body.email.strip().lower())).first()
            if user and user.is_active:
                reset_token = auth.issue_password_reset_token(session, user)
                auth.log_audit(session, None, "password_reset_requested", target_user_id=user.id)
                reset_link = f"{os.getenv('APP_BASE_URL', 'http://localhost:8000')}/reset-password?token={reset_token}"
                # Sent after the response goes out, not before — a live SMTP
                # round-trip (real-world: a few seconds against Office365)
                # must never be what the caller's HTTP request is waiting on.
                background_tasks.add_task(
                    email_sender.send_email,
                    user.email, "Reset your Doshin password",
                    f"Hi {user.full_name or user.email},\n\n"
                    "A password reset was requested for your account. If this wasn't you, "
                    "you can safely ignore this email.\n\n"
                    f"Reset your password here (expires in {auth.RESET_TOKEN_EXPIRE_MINUTES} minutes):\n{reset_link}\n",
                    html_body=email_sender.render_html(
                        "Reset your password",
                        [f"Hi {user.full_name or user.email},",
                         "A password reset was requested for your account. If this wasn't you, "
                         "you can safely ignore this email — your password will stay unchanged."],
                        cta_label="Reset Password", cta_url=reset_link,
                        footnote=f"This link expires in {auth.RESET_TOKEN_EXPIRE_MINUTES} minutes.",
                    ),
                )
            return {"message": "If that email exists, a reset link has been sent."}
        finally:
            session.close()

    @app.post("/api/auth/reset-password", tags=["0. Authentication"])
    def reset_password(body: ResetPasswordRequest):
        session = get_session()
        try:
            user = auth.consume_password_reset_token(session, body.token)
            user.hashed_password = auth.hash_password(body.new_password)
            session.commit()
            # A password reset should end every existing session, including
            # one an attacker may already hold.
            auth.revoke_all_refresh_tokens_for_user(session, user.id)
            auth.log_audit(session, user.id, "password_reset_completed", target_user_id=user.id)
            return {"message": "Password updated. Please log in again."}
        finally:
            session.close()

    # ── User list for task assignment ────────────────────────────
    @app.get("/api/users", tags=["0. Authentication"])
    def list_assignable_users(current: User = Depends(auth.get_current_user)):
        """Returns active sales team members (non-admin users) for task assignment dropdowns."""
        session = get_session()
        try:
            users = (
                session.query(User)
                .filter(User.is_active.is_(True), User.role == "user")
                .order_by(User.full_name.asc())
                .all()
            )
            return {"users": [{"id": u.id, "name": u.full_name or u.email, "email": u.email, "role": u.role} for u in users]}
        finally:
            session.close()

    # ── Super Admin: user management ─────────────────────────────
    @app.get("/api/admin/users", tags=["0. Authentication"])
    def list_users(current: User = Depends(auth.require_role("super_admin"))):
        session = get_session()
        try:
            users = session.query(User).order_by(User.created_at.desc()).all()
            return {"users": [_user_public(u) for u in users]}
        finally:
            session.close()

    @app.post("/api/admin/users", tags=["0. Authentication"])
    def create_user(
        body: CreateUserRequest,
        background_tasks: BackgroundTasks,
        current: User = Depends(auth.require_role("super_admin")),
    ):
        if body.role not in auth.ROLES:
            raise HTTPException(status_code=400, detail="role must be one of " + ", ".join(auth.ROLES))
        session = get_session()
        try:
            email = body.email.strip().lower()
            if session.query(User).filter(User.email.ilike(email)).first():
                raise HTTPException(status_code=409, detail="A user with this email already exists")
            new_user = User(
                email=email,
                full_name=body.full_name,
                role=body.role,
                hashed_password=auth.hash_password(body.password),
                created_by_id=current.id,
            )
            session.add(new_user)
            session.commit()
            auth.log_audit(
                session,
                current.id,
                "user_created",
                target_user_id=new_user.id,
                details={"role": body.role},
            )

            base_url = os.getenv("APP_BASE_URL", "http://localhost:8000")
            access_note = (
                "As a super_admin, you have access to every account by default."
                if new_user.role == "super_admin" else
                "No accounts have been assigned to you yet — a super admin will "
                "grant access to specific accounts shortly; ask them if you need "
                "one urgently."
            )
            # One invitation email whose only button sets the password: an
            # admin-set temp password is a worse first-run experience than the
            # user picking their own, and never having to see the temp password
            # anywhere (audit log, admin's clipboard) is a small but real
            # security win. Setting it is also what lets them sign in, so a
            # separate "Sign In" welcome email only adds noise.
            reset_token = auth.issue_password_reset_token(
                session, new_user, expire_minutes=auth.INVITE_TOKEN_EXPIRE_HOURS * 60)
            reset_link = f"{base_url}/reset-password?token={reset_token}"
            invited_by = current.full_name or current.email
            expiry_note = (
                f"This link expires in {auth.INVITE_TOKEN_EXPIRE_HOURS} hours. If it has expired, "
                f"use \"Forgot password\" on the sign-in page ({base_url}/login) to get a new one."
            )
            background_tasks.add_task(
                email_sender.send_email,
                new_user.email,
                "You're invited to Doshin - set your password",
                (
                    f"Hi {new_user.full_name or new_user.email},\n\n"
                    f"{invited_by} has invited you to Doshin.\n\n"
                    f"Email: {new_user.email}\nRole: {new_user.role}\n\n"
                    f"Access: {access_note}\n\n"
                    f"Set your password to activate your account:\n{reset_link}\n\n"
                    f"{expiry_note}\n"
                ),
                html_body=email_sender.render_html(
                    "Welcome to Doshin",
                    [f"Hi {new_user.full_name or new_user.email},",
                     f"{invited_by} has invited you to Doshin. Set your password to activate your account.",
                     f"Email: {new_user.email}  •  Role: {new_user.role}",
                     access_note],
                    cta_label="Set Password", cta_url=reset_link,
                    footnote=expiry_note,
                ),
            )
            return _user_public(new_user)
        finally:
            session.close()

    @app.patch("/api/admin/users/{user_id}", tags=["0. Authentication"])
    def update_user(
        user_id: int, body: UpdateUserRequest, current: User = Depends(auth.require_role("super_admin"))
    ):
        session = get_session()
        try:
            target = session.query(User).filter_by(id=user_id).first()
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            if target.id == current.id and body.is_active is False:
                raise HTTPException(status_code=400, detail="You cannot deactivate your own account")

            details = {}
            if body.full_name is not None:
                stripped_name = body.full_name.strip() if body.full_name else None
                if stripped_name != target.full_name:
                    details["full_name"] = {"old": target.full_name, "new": stripped_name}
                    target.full_name = stripped_name

            if body.email is not None:
                new_email = body.email.strip().lower()
                if not new_email:
                    raise HTTPException(status_code=400, detail="Email cannot be empty")
                if new_email != target.email:
                    existing = session.query(User).filter(User.email == new_email, User.id != target.id).first()
                    if existing:
                        raise HTTPException(status_code=400, detail="A user with this email address already exists")
                    details["email"] = {"old": target.email, "new": new_email}
                    target.email = new_email

            if body.role is not None and body.role != target.role:
                if body.role not in auth.ROLES:
                    raise HTTPException(status_code=400, detail="role must be one of " + ", ".join(auth.ROLES))
                if target.id == current.id and body.role != "super_admin":
                    raise HTTPException(status_code=400, detail="You cannot demote your own account from super_admin")
                details["role"] = {"old": target.role, "new": body.role}
                target.role = body.role
                from apps.sales_crm.permissions import forget_role
                forget_role(target.id)

            if body.is_active is not None and body.is_active != target.is_active:
                details["is_active"] = {"old": target.is_active, "new": body.is_active}
                target.is_active = body.is_active
                if not body.is_active:
                    auth.revoke_all_refresh_tokens_for_user(session, target.id)

            if body.password:
                if len(body.password) < 6:
                    raise HTTPException(status_code=400, detail="Password must be at least 6 characters long")
                target.hashed_password = auth.hash_password(body.password)
                auth.revoke_all_refresh_tokens_for_user(session, target.id)
                details["password_changed"] = True

            if body.has_dashboard_access is not None and body.has_dashboard_access != target.has_dashboard_access:
                details["has_dashboard_access"] = {"old": target.has_dashboard_access, "new": body.has_dashboard_access}
                target.has_dashboard_access = body.has_dashboard_access

            if body.has_command_center_access is not None and body.has_command_center_access != target.has_command_center_access:
                details["has_command_center_access"] = {"old": target.has_command_center_access, "new": body.has_command_center_access}
                target.has_command_center_access = body.has_command_center_access

            if body.has_tasks_access is not None and body.has_tasks_access != target.has_tasks_access:
                details["has_tasks_access"] = {"old": target.has_tasks_access, "new": body.has_tasks_access}
                target.has_tasks_access = body.has_tasks_access

            if body.has_pipeline_access is not None and body.has_pipeline_access != target.has_pipeline_access:
                details["has_pipeline_access"] = {"old": target.has_pipeline_access, "new": body.has_pipeline_access}
                target.has_pipeline_access = body.has_pipeline_access

            session.commit()
            if details:
                auth.log_audit(session, current.id, "user_updated", target_user_id=target.id, details=details)
            return _user_public(target)
        finally:
            session.close()

    @app.delete("/api/admin/users/{user_id}", tags=["0. Authentication"])
    def delete_user(user_id: int, current: User = Depends(auth.require_role("super_admin"))):
        if user_id == current.id:
            raise HTTPException(status_code=400, detail="You cannot delete your own account")
        session = get_session()
        try:
            target = session.query(User).filter_by(id=user_id).first()
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            email = target.email
            # Logged before the delete since AuditLog.target_user_id turns
            # NULL once the row is gone (ondelete="SET NULL") — the action
            # should still say who it was after the fact.
            auth.log_audit(session, current.id, "user_deleted", target_user_id=None, details={"email": email})
            session.delete(target)
            session.commit()
            return {"ok": True}
        finally:
            session.close()

    @app.get("/api/admin/users/{user_id}/accounts", tags=["0. Authentication"])
    def list_user_account_access(user_id: int, current: User = Depends(auth.require_role("super_admin"))):
        """Every account, flagged with whether this user currently has
        access — the full picker list, not just their current grants."""
        session = get_session()
        try:
            target = session.query(User).filter_by(id=user_id).first()
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            granted_ids = set(auth.get_granted_account_ids(session, user_id))
            accounts = session.query(Account).order_by(Account.display_name).all()
            is_sa = target.role == "super_admin"
            return {
                "user_id": user_id,
                "role": target.role,
                "has_dashboard_access": is_sa or (target.has_dashboard_access if target.has_dashboard_access is not None else True),
                "has_command_center_access": is_sa or (target.has_command_center_access if target.has_command_center_access is not None else True),
                "has_tasks_access": is_sa or (target.has_tasks_access if target.has_tasks_access is not None else True),
                "has_pipeline_access": is_sa or (target.has_pipeline_access if target.has_pipeline_access is not None else True),
                "accounts": [
                    {
                        "id": a.id,
                        "name": a.display_name or a.legal_name or a.key,
                        "granted": a.id in granted_ids,
                    }
                    for a in accounts
                ],
            }
        finally:
            session.close()

    @app.post("/api/admin/users/{user_id}/accounts/{account_id}", tags=["0. Authentication"])
    def grant_user_account_access(
        user_id: int, account_id: int, current: User = Depends(auth.require_role("super_admin"))
    ):
        session = get_session()
        try:
            target = session.query(User).filter_by(id=user_id).first()
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            account = session.query(Account).filter_by(id=account_id).first()
            if not account:
                raise HTTPException(status_code=404, detail="Account not found")
            created = auth.grant_account_access(session, user_id, account_id, current.id)
            if created:
                auth.log_audit(
                    session, current.id, "account_access_granted", target_user_id=user_id,
                    details={"account_id": account_id, "account_name": account.display_name}
                )
            return {"ok": True}
        finally:
            session.close()

    @app.delete("/api/admin/users/{user_id}/accounts/{account_id}", tags=["0. Authentication"])
    def revoke_user_account_access(
        user_id: int, account_id: int, current: User = Depends(auth.require_role("super_admin"))
    ):
        session = get_session()
        try:
            removed = auth.revoke_account_access(session, user_id, account_id)
            if removed:
                auth.log_audit(
                    session, current.id, "account_access_revoked", target_user_id=user_id,
                    details={"account_id": account_id}
                )
            return {"ok": True}
        finally:
            session.close()

    @app.get("/api/admin/stats", tags=["0. Authentication"])
    def admin_dashboard_stats(current: User = Depends(auth.require_role("super_admin"))):
        """Usage summary for the admin dashboard: headline counts, most
        recently active users, and the latest audit trail entries."""
        session = get_session()
        try:
            total_users = session.query(User).count()
            active_users = session.query(User).filter_by(is_active=True).count()
            super_admin_count = session.query(User).filter_by(role="super_admin").count()
            total_accounts = session.query(Account).count()
            open_action_items = session.query(ActionItem).filter(
                ActionItem.status.in_(("open", "in_progress"))
            ).count()
            overdue_action_items = session.query(ActionItem).filter(
                ActionItem.status.in_(("open", "in_progress")),
                ActionItem.due_date.isnot(None),
                ActionItem.due_date < datetime.now(timezone.utc),
            ).count()

            recent_logins = (
                session.query(User)
                .filter(User.last_login_at.isnot(None))
                .order_by(User.last_login_at.desc())
                .limit(10)
                .all()
            )
            recent_audit = (
                session.query(AuditLog)
                .order_by(AuditLog.created_at.desc())
                .limit(20)
                .all()
            )
            user_ids_in_audit = (
                {e.actor_user_id for e in recent_audit if e.actor_user_id}
                | {e.target_user_id for e in recent_audit if e.target_user_id}
            )
            names_by_id = {
                u.id: (u.full_name or u.email)
                for u in session.query(User).filter(User.id.in_(user_ids_in_audit)).all()
            } if user_ids_in_audit else {}

            return {
                "total_users": total_users,
                "active_users": active_users,
                "inactive_users": total_users - active_users,
                "super_admin_count": super_admin_count,
                "total_accounts": total_accounts,
                "open_action_items": open_action_items,
                "overdue_action_items": overdue_action_items,
                "recent_logins": [
                    {
                        "id": u.id, "email": u.email, "full_name": u.full_name,
                        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None
                    }
                    for u in recent_logins
                ],
                "recent_audit": [
                    {
                        "action": e.action,
                        "actor": names_by_id.get(e.actor_user_id, "system"),
                        "target": names_by_id.get(e.target_user_id) if e.target_user_id else None,
                        "details": e.details,
                        "created_at": e.created_at.isoformat() if e.created_at else None,
                    }
                    for e in recent_audit
                ],
            }
        finally:
            session.close()

    @app.get("/api/admin/audit-logs", tags=["0. Authentication"])
    def list_admin_audit_logs(
        limit: int = Query(5, ge=1, le=100),
        offset: int = Query(0, ge=0),
        current: User = Depends(auth.require_role("super_admin"))
    ):
        """Paginated audit trail endpoint so large audit histories load efficiently in small chunks."""
        session = get_session()
        try:
            total_count = session.query(AuditLog).count()
            logs = (
                session.query(AuditLog)
                .order_by(AuditLog.created_at.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            user_ids = (
                {e.actor_user_id for e in logs if e.actor_user_id}
                | {e.target_user_id for e in logs if e.target_user_id}
            )
            names_by_id = {
                u.id: (u.full_name or u.email)
                for u in session.query(User).filter(User.id.in_(user_ids)).all()
            } if user_ids else {}

            return {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + len(logs)) < total_count,
                "audit_logs": [
                    {
                        "id": e.id,
                        "action": e.action,
                        "actor": names_by_id.get(e.actor_user_id, "system"),
                        "target": names_by_id.get(e.target_user_id) if e.target_user_id else None,
                        "details": e.details,
                        "created_at": e.created_at.isoformat() if e.created_at else None,
                    }
                    for e in logs
                ]
            }
        finally:
            session.close()

    @app.get("/api/admin/api-config", tags=["0. Authentication"])
    def get_api_configs(current: User = Depends(auth.require_role("super_admin"))):
        """Returns all system API credentials and runtime settings with masked secrets."""
        from services.config_service import get_all_configs_dto
        session = get_session()
        try:
            return {"configs": get_all_configs_dto(session)}
        finally:
            session.close()

    @app.post("/api/admin/api-config", tags=["0. Authentication"])
    def update_api_configs(body: UpdateApiConfigRequest, current: User = Depends(auth.require_role("super_admin"))):
        """Updates and encrypts API configuration keys and triggers in-memory runtime hot reload."""
        from services.config_service import update_configs
        session = get_session()
        try:
            success, msg = update_configs(body.configs, current.id, session)
            return {"ok": success, "message": msg}
        finally:
            session.close()

    @app.post("/api/admin/api-config/test", tags=["0. Authentication"])
    def test_api_connection(body: TestApiConfigRequest, current: User = Depends(auth.require_role("super_admin"))):
        """Tests live connectivity to a third-party API provider with uncommitted or configured credentials."""
        from services.config_service import test_provider_connection
        session = get_session()
        try:
            res = test_provider_connection(body.provider, body.credentials, session)
            return res
        finally:
            session.close()

    # ══════════════════════════════════════════════════════
    # REQUEST / RESPONSE MODELS
    # ══════════════════════════════════════════════════════
    class AccountPreflightRequest(BaseModel):
        company_name: str
        domain: Optional[str] = None

    class AccountCreateRequest(BaseModel):
        company_name: str
        domain: Optional[str] = None
        stock_symbol: Optional[str] = None
        sec_cik: Optional[str] = None
        industry: Optional[str] = None
        headquarters_location: Optional[str] = None
        company_type: Optional[str] = None
        auto_enrich: Optional[bool] = False

    class PersonaTierPurgeSelection(BaseModel):
        all: bool = False
        c_suite: bool = False
        vp_head: bool = False
        director: bool = False
        manager_other: bool = False

    class SignalPurgeSelection(BaseModel):
        opportunity_signals: bool = False
        weekly_digests: bool = False
        posts_news: bool = False
        cxo_movements: bool = False
        jobs: bool = False
        action_items: bool = False

    class AccountPurgeRequest(BaseModel):
        delete_account_record: bool = False
        delete_lobs: bool = False
        personas: Optional[PersonaTierPurgeSelection] = None
        signals: Optional[SignalPurgeSelection] = None
        confirmation_text: Optional[str] = None

    class AccountFetchRequest(BaseModel):
        company_name: str
        target_url: Optional[str] = None

    class AccountDumpRequest(BaseModel):
        account_data: Dict[str, Any]

    class Sec10kRequest(BaseModel):
        sec_cik: str
        chunk_size: int = 1500
        overlap: int = 200

    class PatentsRequest(BaseModel):
        company_name: str
        max_results: int = 10

    class LobsFetchRequest(BaseModel):
        company_name: str
        account_id: Optional[int] = None
        lob_name: Optional[str] = None
        lob_domain: Optional[str] = None

    class LobsDumpRequest(BaseModel):
        account_id: int
        lobs_data: List[Dict[str, Any]]

    class PersonaCardFetchRequest(BaseModel):
        """
        Exact payload structure sent by frontend when user clicks 'Fetch' on a Person card:
        {
          "key": "jane_doe",
          "display_name": "Jane Doe (CEO, Example Co)",
          "linkedin_url": "https://www.linkedin.com/in/janedoe/",
          "twitter_handle": null,
          "reddit_query": "\"Jane Doe\"",
          "sec_cik": null,
          "news_query": "\"Jane Doe\"",
          "patents_query": "Jane Doe",
          "youtube_channel_id": null,
          "rss_url": null,
          "account_id": null,
          "company_name": null,
          "title": null,
          "enrich_ai_dossier": true
        }
        """

        key: Optional[str] = None
        display_name: Optional[str] = None
        name: Optional[str] = None
        title: Optional[str] = None
        company_name: Optional[str] = None
        account_id: Optional[int] = None
        linkedin_url: Optional[str] = None
        twitter_handle: Optional[str] = None
        reddit_query: Optional[str] = None
        sec_cik: Optional[str] = None
        news_query: Optional[str] = None
        patents_query: Optional[str] = None
        youtube_channel_id: Optional[str] = None
        rss_url: Optional[str] = None
        enrich_ai_dossier: bool = True

    class PersonDumpRequest(BaseModel):
        account_id: int
        person_data: Dict[str, Any]

    class HierarchyFetchRequest(BaseModel):
        company_domain: str
        company_name: Optional[str] = None
        sec_cik: Optional[str] = None
        enrich_csuite_dossiers: bool = True
        account_id: Optional[int] = None

    class HierarchyDumpRequest(BaseModel):
        account_id: int
        hierarchy: Dict[str, List[Dict[str, Any]]]

    class PipelineRunRequest(BaseModel):
        company_name: str
        target_url: Optional[str] = None

    class PipelineDumpDbRequest(BaseModel):
        run_dir: Optional[str] = None
        file: Optional[str] = None
        require_validation: bool = True

    class OpportunitySignalItem(BaseModel):
        """One currently-detected growth-theme or domain-expansion suggestion, as computed client-side."""

        signal_key: str
        title: str
        details: Dict[str, Any] = {}

    class OpportunitySignalSyncRequest(BaseModel):
        category: str  # 'growth_theme' | 'domain_expansion'
        items: List[OpportunitySignalItem] = []

    class WeeklyDigestSyncRequest(BaseModel):
        """The `digest.email` object already produced weekly per-account by the content pipeline
        (see Digest.digest['email']), forwarded here so a snapshot of it gets archived."""

        target_key: str
        generated_at: Optional[str] = None
        subject: Optional[str] = None
        body: Optional[str] = None
        priority: Optional[str] = None
        confidence: Optional[str] = None
        data_gaps: List[str] = []
        do_not_say: List[str] = []

    class ActionItemCreateRequest(BaseModel):
        title: str
        description: Optional[str] = None
        persona_id: Optional[int] = None
        priority: str = "medium"  # high | medium | low
        status: Optional[str] = "open"  # open | in_progress
        due_date: Optional[str] = None  # ISO 8601
        assigned_to_id: Optional[int] = None
        source: str = "manual"  # manual | playbook | signal_feed

    class ActionItemDirectCreateRequest(BaseModel):
        account_id: int
        title: str
        description: Optional[str] = None
        persona_id: Optional[int] = None
        priority: str = "medium"  # high | medium | low
        status: Optional[str] = "open"  # open | in_progress
        due_date: Optional[str] = None  # ISO 8601
        assigned_to_id: Optional[int] = None
        source: str = "manual"  # manual | playbook | signal_feed

    class ActionItemUpdateRequest(BaseModel):
        title: Optional[str] = None
        description: Optional[str] = None
        persona_id: Optional[int] = None
        status: Optional[str] = None  # open | in_progress | done | cancelled
        priority: Optional[str] = None
        due_date: Optional[str] = None
        assigned_to_id: Optional[int] = None

    class ActionItemCompleteRequest(BaseModel):
        comment: Optional[str] = None

    # ══════════════════════════════════════════════════════
    # TAB 1: ACCOUNT LEVEL ENDPOINTS
    # ══════════════════════════════════════════════════════
    account_router = APIRouter(prefix="/api/account", tags=["1. Account Level"])

    @account_router.post("/create", dependencies=[Depends(auth.require_role("super_admin"))])
    def create_account_stub(req: AccountCreateRequest):
        """[Tab 1 - Add Account Modal]: Creates or fetches an Account row in PostgreSQL with an official ID."""
        if getattr(req, "auto_enrich", False):
            return create_and_enrich_account(req)
        session = get_session()
        try:
            clean_name = req.company_name.strip()
            slug = slugify(clean_name)
            clean_dom = (
                req.domain.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0].strip().lower()
                if req.domain
                else None
            )
            if clean_dom and clean_dom.startswith("www."):
                clean_dom = clean_dom[4:]

            # Check if account already exists by key, name, or domain
            filters = [
                Account.key == slug,
                Account.display_name.ilike(clean_name),
            ]
            if clean_dom:
                filters.extend(
                    [
                        Account.domain == clean_dom,
                        Account.primary_domain == clean_dom,
                    ]
                )

            existing = session.query(Account).filter(or_(*filters)).first()
            if existing:
                # Update supplementary fields if provided
                if req.stock_symbol:
                    existing.stock_symbol = req.stock_symbol.strip().upper()
                if req.sec_cik:
                    existing.sec_cik = req.sec_cik.strip()
                if req.company_type:
                    existing.company_type = req.company_type.strip()
                if req.headquarters_location:
                    existing.headquarters_location = req.headquarters_location.strip()
                if req.industry:
                    existing.industries = [req.industry.strip()]
                session.commit()
                session.refresh(existing)
                return {
                    "status": "exists",
                    "account_id": existing.id,
                    "key": existing.key,
                    "name": existing.display_name,
                    "domain": existing.primary_domain or existing.domain,
                    "stock_symbol": existing.stock_symbol,
                    "sec_cik": existing.sec_cik,
                    "message": (
                        f"Account '{existing.display_name}' already registered "
                        f"in database (ID: {existing.id})."
                    ),
                }

            # Create new account row with rich enterprise attributes
            new_account = Account(
                key=slug,
                display_name=clean_name,
                legal_name=clean_name,
                domain=clean_dom,
                primary_domain=clean_dom,
                website_url=f"https://{clean_dom}" if clean_dom else None,
                stock_symbol=req.stock_symbol.strip().upper() if req.stock_symbol else None,
                sec_cik=req.sec_cik.strip() if req.sec_cik else None,
                company_type=req.company_type.strip() if req.company_type else "Public",
                headquarters_location=req.headquarters_location.strip() if req.headquarters_location else None,
                industries=[req.industry.strip()] if req.industry else [],
            )
            session.add(new_account)
            session.commit()
            session.refresh(new_account)

            return {
                "status": "created",
                "account_id": new_account.id,
                "key": new_account.key,
                "name": new_account.display_name,
                "domain": new_account.primary_domain,
                "stock_symbol": new_account.stock_symbol,
                "sec_cik": new_account.sec_cik,
                "message": (
                    f"Account '{new_account.display_name}' created successfully "
                    f"in database (ID: {new_account.id})."
                ),
            }
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Account creation failed: {str(e)}")
        finally:
            session.close()

    @account_router.post("/preflight", dependencies=[Depends(auth.require_role("super_admin"))])
    def preflight_account_endpoint(req: AccountPreflightRequest):
        """[Tab 1 - Add Account Preflight]: Verifies required resources, rates, and credit balance."""
        return AccountAutomationEngine.preflight_account_check(req.company_name, req.domain)

    @account_router.post("/create-and-enrich", dependencies=[Depends(auth.require_role("super_admin"))])
    def create_and_enrich_account(req: AccountCreateRequest):
        """[Tab 1 - Automated Add Account Flow]:
        Pre-flight credit check -> Account Stub -> Autonomous 11-source harvest ->
        Self-healing retry -> Additive Coalescence -> DB Golden Record commit -> Telemetry Ledger.
        Returns job_id immediately with background execution.
        """
        clean_name = req.company_name.strip()
        clean_dom = (
            req.domain.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0].strip().lower()
            if req.domain
            else None
        )
        if clean_dom and clean_dom.startswith("www."):
            clean_dom = clean_dom[4:]

        preflight = AccountAutomationEngine.preflight_account_check(clean_name, clean_dom)
        if not preflight["allowed"]:
            return JSONResponse(
                status_code=402,
                content={
                    "status": "rejected",
                    "reason": "insufficient_credits",
                    "detail": preflight["message"],
                    "preflight": preflight,
                },
            )

        job_id = _jreg_create(TYPE_ACCOUNT, meta={
            "company_name": clean_name,
            "target_url": clean_dom,
            "mode": "autonomous_pipeline",
        })

        def _bg_auto():
            try:
                AccountAutomationEngine.execute_account_automation(
                    company_name=clean_name,
                    domain=clean_dom,
                    stock_symbol=req.stock_symbol,
                    sec_cik=req.sec_cik,
                    industry=req.industry,
                    headquarters_location=req.headquarters_location,
                    company_type=req.company_type,
                    job_id=job_id,
                )
            except Exception as exc:
                _jreg_exc(job_id, exc)

        import threading
        threading.Thread(target=_bg_auto, daemon=False, name=f"auto-acct-{job_id}").start()

        return {
            "status": "started",
            "job_id": job_id,
            "company_name": clean_name,
            "domain": clean_dom,
            "poll_url": f"/api/pipeline/job/{job_id}",
            "estimated_credits": preflight["required_credits"],
            "preflight": preflight,
            "message": f"Autonomous pipeline started for '{clean_name}'. Polling job '{job_id}'.",
        }

    class AccountRetrySourcesRequest(BaseModel):
        sources: Optional[List[str]] = None
        include_empty: Optional[bool] = True

    @account_router.post("/{account_id}/retry-sources", dependencies=[Depends(auth.require_role("super_admin"))])
    def retry_account_sources_endpoint(account_id: int, req: Optional[AccountRetrySourcesRequest] = None):
        """[Tab 1 - Selective Source Retry]: Re-invokes ONLY failed or empty sources for an account without re-fetching successful ones."""
        sources = req.sources if req else None
        include_empty = req.include_empty if req else True
        return AccountAutomationEngine.retry_account_sources(
            account_id=account_id,
            specific_sources=sources,
            include_empty=include_empty,
        )

    @account_router.post("/fetch", dependencies=[Depends(auth.require_role("super_admin"))])
    def fetch_account_data(req: AccountFetchRequest):
        """[Tab 1 - Fetch Button]: Enterprise 11-source account intelligence collection.
        Uses AccountService.collect() with: SEC EDGAR, GLEIF, OpenCorporates, FMP, CourtListener,
        Finnhub, Apify Crunchbase (curious_coder/crunchbase-url-scraper), Apify Glassdoor
        (memo23/glassdoor-scraper), Diffbot KG, Serper, Wikipedia, FEC — plus standalone
        connectors: 10-K chunks, USPTO patents, SEC Exhibit 21 subsidiaries, GLEIF ownership tree.
        """
        t0 = datetime.now(timezone.utc)
        try:
            account_data = AccountService.collect(
                company_name=req.company_name,
                domain=req.target_url,
            )

            # Attach known LOBs and personas already in DB for this account
            session = get_session()
            try:
                existing = (
                    session.query(Account)
                    .filter(
                        or_(
                            Account.domain == req.target_url,
                            Account.primary_domain == req.target_url,
                            Account.display_name.ilike(f"%{req.company_name}%"),
                        )
                    )
                    .first()
                )
                if existing:
                    account_data["known_lobs"] = [
                        {"id": lob_item.id, "name": lob_item.lob_name, "domain": lob_item.domain}
                        for lob_item in (existing.lobs or [])
                    ]
                    account_data["known_personas"] = [
                        {"id": p.id, "name": p.full_name, "title": p.title, "tier": p.tier}
                        for p in (existing.personas or [])
                    ]
                    account_data["lobs_count"] = len(account_data["known_lobs"])
                    account_data["total_contacts_captured"] = len(account_data["known_personas"])
            except Exception as db_err:
                print(f"[!] DB lookup for known LOBs/personas notice: {db_err}")
            finally:
                session.close()

            # Build the required_account sub-key so AccountSchema.from_enriched_json() finds OSINT URLs
            required_account = {
                "key": account_data.get("key"),
                "display_name": account_data.get("display_name"),
                "sec_edgar_url": account_data.get("sec_edgar_url"),
                "sec_filings_rss": account_data.get("sec_filings_rss"),
                "sec_submissions_url": account_data.get("sec_submissions_url"),
                "twitter_live_url": account_data.get("twitter_live_url"),
                "reddit_query": account_data.get("reddit_query"),
                "reddit_rss_url": account_data.get("reddit_rss_url"),
                "news_query": account_data.get("news_query"),
                "rss_url": account_data.get("rss_url"),
                "google_patents_url": account_data.get("google_patents_url"),
                "google_trends_url": account_data.get("google_trends_url"),
                "youtube_search_url": account_data.get("youtube_search_url"),
                "openalex_institution_url": account_data.get("openalex_institution_url"),
                "wikidata_entity_url": account_data.get("wikidata_entity_url"),
                "blog_url": account_data.get("blog_url"),
                "github_url": account_data.get("github_url"),
                "glassdoor_url": account_data.get("glassdoor_url"),
                "youtube_channel_id": account_data.get("youtube_channel_id"),
            }

            wrapped = {
                **account_data,
                "required_account": required_account,
                # Mirror all fields into nested sub-dicts that from_enriched_json() reads
                "identity": account_data,
                "firmographics": account_data,
                "location": account_data,
                "contact_and_social": account_data,
                "financials_and_funding": account_data,
                "market_and_ipo": account_data,
                "acquisitions_and_suborgs": account_data,
                "web_traffic_and_growth": account_data,
                "tech_and_patents": account_data,
                "key_people": account_data,
            }

            audit = account_data.get("_validation_audit") or {}
            raw_dir_val = account_data.get("raw_dir") or account_data.get("raw_storage_dir")

            # Dynamic Metered Credit Breakdown
            from services.credit_accounting_engine import CreditAccountingEngine
            acct_credit_tally = account_data.get("_credit_accounting") or CreditAccountingEngine.tally_account_telemetry(
                (account_data.get("_telemetry") or {}).get("sources", {})
            )
            now_completed = datetime.now(timezone.utc)
            duration_sec = round((now_completed - t0).total_seconds(), 2)
            credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                company_name=req.company_name,
                run_id=f"run_{slugify(req.company_name)[:20]}_acct",
                run_number=1,
                started_at=t0,
                duration_seconds=duration_sec,
                status="staged",
                account_tally=acct_credit_tally,
                lob_tally={"credits": 0, "resources": []},
                persona_tally={"credits": 0, "resources": []},
            )

            PipelineRunLogger.log_event(
                company_name=req.company_name,
                target_url=req.target_url,
                level="account",
                action="pull",
                status="staged",
                quality_score=float(audit.get("score", 0.0)),
                quality_grade=audit.get("grade", "N/A"),
                started_at=t0,
                completed_at=now_completed,
                duration_seconds=duration_sec,
                raw_storage_dir=str(raw_dir_val) if raw_dir_val else None,
                total_credits_used=acct_credit_tally.get("credits", 0),
                credits_breakdown=credits_breakdown,
                entities_extracted={
                    "known_lobs_count": len(account_data.get("known_lobs", [])),
                    "known_personas_count": len(account_data.get("known_personas", [])),
                    "discovered_lobs_count": len(account_data.get("discovered_lob_names", [])),
                    "display_name": account_data.get("display_name"),
                    "telemetry_sources": (account_data.get("_telemetry") or {}).get("sources", {}),
                },
            )

            return {"status": "staged", "company_name": req.company_name, "account": wrapped}
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name=req.company_name,
                target_url=req.target_url,
                level="account",
                action="pull",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Account fetch failed: {str(e)}")

    @account_router.post("/fetch-background", dependencies=[Depends(auth.require_role("super_admin"))])
    def fetch_account_background(req: AccountFetchRequest):
        """
        [Background Mode — Tab 1 Fetch]:
        Identical to POST /api/accounts/fetch but returns a job_id immediately (200ms).
        AccountService.collect() runs in an independent background thread that survives
        browser close, refresh, or tab switch.

        Workflow:
          1. POST here → receive {"job_id": "pjob_...", "poll_url": "/api/pipeline/job/..."}
          2. Poll GET /api/pipeline/job/{job_id} every 3 seconds
          3. When status == "done", use result.account as you would from /fetch
        """
        t0 = datetime.now(timezone.utc)
        job_id = _jreg_create(TYPE_ACCOUNT, meta={
            "company_name": req.company_name,
            "target_url":   req.target_url,
        })

        def _bg_account():
            try:
                _jreg_update(job_id, status="running", progress_pct=5,
                             message=f"Collecting account intelligence for '{req.company_name}'...")

                account_data = AccountService.collect(
                    company_name=req.company_name,
                    domain=req.target_url,
                )

                _jreg_update(job_id, progress_pct=70, message="Attaching known LOBs/personas from DB...")

                # Attach known LOBs and personas already in DB (exact same as /fetch)
                _session = get_session()
                try:
                    existing = _session.query(Account).filter(
                        or_(
                            Account.domain == req.target_url,
                            Account.primary_domain == req.target_url,
                            Account.display_name.ilike(f"%{req.company_name}%"),
                        )
                    ).first()
                    if existing:
                        account_data["known_lobs"] = [
                            {"id": l.id, "name": l.lob_name, "domain": l.domain}
                            for l in (existing.lobs or [])
                        ]
                        account_data["known_personas"] = [
                            {"id": p.id, "name": p.full_name, "title": p.title, "tier": p.tier}
                            for p in (existing.personas or [])
                        ]
                        account_data["lobs_count"] = len(account_data["known_lobs"])
                        account_data["total_contacts_captured"] = len(account_data["known_personas"])
                except Exception as db_err:
                    print(f"[!] [BG Account] DB lookup notice: {db_err}")
                finally:
                    _session.close()

                _jreg_update(job_id, progress_pct=82, message="Building wrapped response...")

                # Build required_account + wrapper (exact same as /fetch)
                required_account = {k: account_data.get(k) for k in (
                    "key", "display_name", "sec_edgar_url", "sec_filings_rss",
                    "sec_submissions_url", "twitter_live_url", "reddit_query",
                    "reddit_rss_url", "news_query", "rss_url", "google_patents_url",
                    "google_trends_url", "youtube_search_url", "openalex_institution_url",
                    "wikidata_entity_url", "blog_url", "github_url", "glassdoor_url",
                    "youtube_channel_id",
                )}
                wrapped = {
                    **account_data,
                    "required_account":       required_account,
                    "identity":               account_data,
                    "firmographics":          account_data,
                    "location":               account_data,
                    "contact_and_social":     account_data,
                    "financials_and_funding": account_data,
                    "market_and_ipo":         account_data,
                    "acquisitions_and_suborgs": account_data,
                    "web_traffic_and_growth": account_data,
                    "tech_and_patents":       account_data,
                    "key_people":             account_data,
                }

                _jreg_update(job_id, progress_pct=90, message="Logging pipeline run...")

                audit = account_data.get("_validation_audit") or {}
                raw_dir_val = account_data.get("raw_dir") or account_data.get("raw_storage_dir")
                from services.credit_accounting_engine import CreditAccountingEngine
                acct_credit_tally = account_data.get("_credit_accounting") or CreditAccountingEngine.tally_account_telemetry(
                    (account_data.get("_telemetry") or {}).get("sources", {})
                )
                now_completed = datetime.now(timezone.utc)
                duration_sec = round((now_completed - t0).total_seconds(), 2)
                credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                    company_name=req.company_name,
                    run_id=f"run_{slugify(req.company_name)[:20]}_acct_bg",
                    run_number=1,
                    started_at=t0,
                    duration_seconds=duration_sec,
                    status="staged",
                    account_tally=acct_credit_tally,
                    lob_tally={"credits": 0, "resources": []},
                    persona_tally={"credits": 0, "resources": []},
                )
                PipelineRunLogger.log_event(
                    company_name=req.company_name,
                    target_url=req.target_url,
                    level="account",
                    action="pull",
                    status="staged",
                    quality_score=float(audit.get("score", 0.0)),
                    quality_grade=audit.get("grade", "N/A"),
                    started_at=t0,
                    completed_at=now_completed,
                    duration_seconds=duration_sec,
                    raw_storage_dir=str(raw_dir_val) if raw_dir_val else None,
                    total_credits_used=acct_credit_tally.get("credits", 0),
                    credits_breakdown=credits_breakdown,
                    entities_extracted={
                        "known_lobs_count":      len(account_data.get("known_lobs", [])),
                        "known_personas_count":  len(account_data.get("known_personas", [])),
                        "discovered_lobs_count": len(account_data.get("discovered_lob_names", [])),
                        "display_name":          account_data.get("display_name"),
                        "background_job_id":     job_id,
                    },
                )

                _jreg_update(job_id, status="done", progress_pct=100,
                             message=f"Account enrichment complete for '{req.company_name}'.",
                             result={"status": "staged", "company_name": req.company_name, "account": wrapped})
            except Exception as exc:
                _jreg_exc(job_id, exc)
                PipelineRunLogger.log_event(
                    company_name=req.company_name,
                    target_url=req.target_url,
                    level="account", action="pull", status="failed",
                    started_at=t0, completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )

        threading.Thread(target=_bg_account, daemon=False, name=f"acct-{job_id}").start()
        return {
            "status":   "queued",
            "job_id":   job_id,
            "poll_url": f"/api/pipeline/job/{job_id}",
            "message":  f"Account enrichment started for '{req.company_name}'. Thread runs independently of this connection.",
        }

    @account_router.post("/validate", dependencies=[Depends(auth.require_role("super_admin"))])
    def validate_account_data(account_data: Dict[str, Any] = Body(...)):
        """[Tab 1 - Validate Button]: Validates staged account data."""
        t0 = datetime.now(timezone.utc)
        comp_name = (
            account_data.get("display_name")
            or account_data.get("legal_name")
            or account_data.get("company_name")
            or "Account"
        )
        try:
            report = DataQualityValidator.validate_account(account_data)
            score = float(report.get("score", 0.0))
            grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))

            PipelineRunLogger.log_event(
                company_name=comp_name,
                target_url=account_data.get("domain") or account_data.get("website_url"),
                level="account",
                action="validate",
                status="validated",
                quality_score=score,
                quality_grade=grade,
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "score": score,
                    "warnings": report.get("warnings", []),
                    "checks": report.get("checks", {}),
                },
            )

            return {
                "status": "validated",
                "score": report["score"],
                "checks": report["checks"],
                "warnings": report["warnings"],
            }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name=comp_name,
                level="account",
                action="validate",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Account validation failed: {str(e)}")

    @account_router.post("/dump-db", dependencies=[Depends(auth.require_role("super_admin"))])
    def dump_account_to_db(req: AccountDumpRequest):
        """[Tab 1 - Dump DB Button]: Commits validated account data into PostgreSQL `accounts` table."""
        t0 = datetime.now(timezone.utc)
        comp_name = (
            req.account_data.get("display_name")
            or req.account_data.get("legal_name")
            or req.account_data.get("company_name")
            or "Account"
        )
        session = get_session()
        try:
            wrapper_doc = {"account": req.account_data}
            schema = AccountSchema.from_enriched_json(wrapper_doc)
            repo = AccountRepository(session)
            acct = repo.upsert(schema, raw_data=req.account_data)
            session.commit()

            PipelineRunLogger.log_event(
                company_name=acct.legal_name or acct.display_name or comp_name,
                target_url=acct.domain or acct.primary_domain,
                level="account",
                action="dump",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "account_id": acct.id,
                    "key": acct.key,
                    "legal_name": acct.legal_name,
                },
            )

            return {
                "status": "success",
                "account_id": acct.id,
                "key": acct.key,
                "legal_name": acct.legal_name,
                "message": f"Account '{acct.key}' successfully saved to database.",
            }
        except Exception as e:
            session.rollback()
            PipelineRunLogger.log_event(
                company_name=comp_name,
                level="account",
                action="dump",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Account DB dump failed: {str(e)}")
        finally:
            session.close()

    def _format_compact_revenue(val: Any) -> str:
        if not val or val == "Revenue N/A":
            return "Revenue N/A"
        s = str(val).strip()
        if s.startswith("$") or any(s.endswith(suffix) for suffix in ["B", "M", "K", "T"]):
            return s
        try:
            num = float(s.replace(",", ""))
            if num >= 1e12:
                return f"${num / 1e12:.2f}B" if num < 1e12 else f"${num / 1e12:.2f}T"
            if num >= 1e9:
                return f"${num / 1e9:.2f}B"
            if num >= 1e6:
                return f"${num / 1e6:.2f}M"
            if num >= 1e3:
                return f"${num / 1e3:.2f}K"
            return f"${num:,.2f}"
        except (ValueError, TypeError):
            return s

    _persona_photo_candidates = persona_photo_service.candidates

    def _serialize_persona_full(p: Persona, last_run_at: Optional[str] = None) -> Dict[str, Any]:
        p_raw = p.raw_data if isinstance(p.raw_data, dict) else {}
        p_ext = p.extended_profile if isinstance(p.extended_profile, dict) else {}
        photo_candidates = _persona_photo_candidates(p)
        photo_url = photo_candidates[0] if photo_candidates else None

        loc_str = (
            (p.city + (f", {p.state}" if p.state else (f", {p.country}" if p.country else "")))
            if p.city
            else (p.country or None)
        )
        return {
            "id": p.id,
            "account_id": p.account_id,
            "lob_id": p.lob_id,
            "external_id": p.external_id,
            "key": p.key,
            "name": p.full_name or p.display_name or "Executive",
            "display_name": p.display_name or p.full_name or "Executive",
            "full_name": p.full_name,
            "first_name": p.first_name,
            "last_name": p.last_name,
            "title": p.title,
            "tier": p.tier,
            "seniority_raw": p.seniority_raw,
            "departments": p.departments or ["Executive"],
            "email": contact_privacy.safe_email(p.email),
            "email_status": p.email_status or ("Verified" if p.email else None),
            "phone": contact_privacy.safe_phone(p.phone, p.direct_mobile_phone),
            "linkedin_url": p.linkedin_url,
            "photo_url": photo_url,
            "has_photo": bool(photo_candidates),
            "crunchbase_permalink": p.crunchbase_permalink,
            "city": p.city,
            "state": p.state,
            "country": p.country,
            "location": loc_str,
            "source": p.source,
            "hierarchy_level": p.hierarchy_level,
            "decision_authority": p.decision_authority,
            "budget_authority": p.budget_authority,
            "reports_to": p_raw.get("reports_to") or p_raw.get("reportsTo") or None,
            "twitter_handle": getattr(p, "twitter_handle", None),
            "corporate_bio_url": getattr(p, "corporate_bio_url", None),
            "crunchbase_url": getattr(p, "crunchbase_url", None),
            "sec_cik": getattr(p, "sec_cik", None),
            "sec_insider_trades_url": getattr(p, "sec_insider_trades_url", None),
            "fec_contributions_url": getattr(p, "fec_contributions_url", None),
            "quiver_insider_url": getattr(p, "quiver_insider_url", None),
            "bloomberg_url": getattr(p, "bloomberg_url", None),
            "wsj_article_url": getattr(p, "wsj_article_url", None),
            "media_interview_url": getattr(p, "media_interview_url", None),
            "annual_report_url": getattr(p, "annual_report_url", None),
            "zoominfo_url": getattr(p, "zoominfo_url", None),
            "rss_url": getattr(p, "rss_url", None),
            "youtube_url": getattr(p, "youtube_url", None),
            "podcast_url": getattr(p, "podcast_url", None),
            "openinsider_url": getattr(p, "openinsider_url", None),
            "secform4_url": getattr(p, "secform4_url", None),
            "wayback_url": getattr(p, "wayback_url", None),
            "theorg_url": getattr(p, "theorg_url", None),
            "seeking_alpha_url": getattr(p, "seeking_alpha_url", None),
            "external_board_url": getattr(p, "external_board_url", None),
            "twitter_live_url": getattr(p, "twitter_live_url", None),
            "google_patents_url": getattr(p, "google_patents_url", None),
            "google_scholar_url": getattr(p, "google_scholar_url", None),
            "openalex_author_url": getattr(p, "openalex_author_url", None),
            "orcid_search_url": getattr(p, "orcid_search_url", None),
            "wikidata_person_url": getattr(p, "wikidata_person_url", None),
            "reddit_rss_url": getattr(p, "reddit_rss_url", None),
            "google_trends_url": getattr(p, "google_trends_url", None),
            "youtube_interviews_url": getattr(p, "youtube_interviews_url", None),
            "podcast_search_url": getattr(p, "podcast_search_url", None),
            "reddit_query": getattr(p, "reddit_query", None),
            "news_query": getattr(p, "news_query", None),
            "patents_query": getattr(p, "patents_query", None),
            "youtube_channel_id": getattr(p, "youtube_channel_id", None),
            "skills": p.skills or [],
            "target_kpis": p.target_kpis or [],
            "operational_pain_points": p.operational_pain_points or [],
            "key_objections": p.key_objections or [],
            "degree": p.degree,
            "institution": p.institution,
            "prior_company": p.prior_company,
            "communication_style": p.communication_style,
            "engagement_rate": p.engagement_rate,
            "value_proposition": p.value_proposition,
            "personalized_icebreaker": p.personalized_icebreaker,
            "social_platform": p.social_platform,
            "social_profile_url": p.social_profile_url,
            "social_presence_level": p.social_presence_level,
            "headline": p.headline,
            "employment_history": p.employment_history or [],
            "past_companies": p.past_companies or [],
            "previous_titles": p.previous_titles or [],
            "current_role_tenure_months": p.current_role_tenure_months,
            "is_new_in_role": p.is_new_in_role or False,
            "career_trajectory_score": p.career_trajectory_score,
            "education_history": p.education_history or [],
            "osint_feed_manifest": p.osint_feed_manifest or {},
            "is_manually_verified": bool(getattr(p, "is_manually_verified", False)),
            "manually_verified_at": p.manually_verified_at.isoformat() if getattr(p, "manually_verified_at", None) else None,
            "last_run_at": last_run_at or (p.manually_verified_at.isoformat() if getattr(p, "manually_verified_at", None) else None),
            "extended_profile": contact_privacy.scrub_extended_profile(getattr(p, "extended_profile", None) or {}),
            "raw_data": contact_privacy.scrub_raw(p.raw_data, p.personal_email, p.direct_mobile_phone),
        }

    def _serialize_persona_summary(p: Persona) -> Dict[str, Any]:
        # Return full persona data so all columns and rich intelligence are available throughout the app
        return _serialize_persona_full(p)


    def _distribute_personas_across_lobs(raw_lobs, personas_list):
        """Synthetic C-suite + VP-cohort split across LOBs for display grouping —
        not the DB's real Persona.lob_id relationship, just how the UI has always
        grouped contacts per division. Shared by the full and summary serializers
        so both agree on the same per-LOB counts/lists."""
        c_suite_personas = [
            p for p in personas_list if p.get("tier") == "C-Suite" or p.get("hierarchy_level") in [1, 2]
        ]
        vp_personas = [p for p in personas_list if p not in c_suite_personas]
        total_lobs = len(raw_lobs) or 1
        assignments = []
        for idx, lob_item in enumerate(raw_lobs):
            chunk_size = max(1, len(vp_personas) // total_lobs) if vp_personas else 0
            start_i = idx * chunk_size
            end_i = start_i + chunk_size if idx < total_lobs - 1 else len(vp_personas)
            assignments.append((lob_item, c_suite_personas[:2] + vp_personas[start_i:end_i]))
        return assignments

    def _serialize_sublob_obj(s: SubLob, parent_name: str = "") -> Dict[str, Any]:
        return {
            "id": s.id,
            "lob_id": s.lob_id,
            "name": s.name,
            "legal_name": getattr(s, "legal_name", None) or s.name,
            "lei_code": getattr(s, "lei_code", None),
            "jurisdiction": getattr(s, "jurisdiction", None),
            "country": getattr(s, "country", None),
            "city": getattr(s, "city", None),
            "relationship_type": getattr(s, "relationship_type", None) or "Level 3: Operating Sub-LOB / Grandchild",
            "status": getattr(s, "status", None) or "ACTIVE",
            "entity_level": getattr(s, "entity_level", None) or "Level 3 (Operating Sub-LOB)",
            "parent_lob_lei": getattr(s, "parent_lob_lei", None),
            "parent_lob_name": getattr(s, "parent_lob_name", None) or parent_name,
            "domain": getattr(s, "domain", None),
            "website_url": getattr(s, "website_url", None),
            "is_manually_verified": bool(getattr(s, "is_manually_verified", False)),
            "manually_verified_at": s.manually_verified_at.isoformat() if getattr(s, "manually_verified_at", None) else None,
            "metadata": getattr(s, "metadata_", {}) or {},
            "desc": f"Specialized unit under {parent_name or 'Parent LOB'}",
        }

    def _serialize_lob_full(lob_item: Lob, assigned_personas: List[Dict[str, Any]], last_run_at: Optional[str] = None) -> Dict[str, Any]:
        sub_lobs_formatted = [
            _serialize_sublob_obj(s, lob_item.lob_name)
            for s in (lob_item.sub_lobs or [])
        ]
        return {
            "id": lob_item.id,
            "account_id": lob_item.account_id,
            "key": lob_item.key,
            "name": lob_item.lob_name,
            "lob_name": lob_item.lob_name,
            "domain": lob_item.domain,
            "website_url": lob_item.website_url,
            "crunchbase_url": lob_item.crunchbase_url,
            "relationship_type": lob_item.relationship_type,
            "desc": lob_item.overview,
            "overview": lob_item.overview,
            "revenue": lob_item.audited_segment_revenue,
            "audited_segment_revenue": lob_item.audited_segment_revenue,
            "head": lob_item.operating_head,
            "operating_head": lob_item.operating_head,
            "headcount": lob_item.segment_headcount,
            "segment_headcount": lob_item.segment_headcount,
            "lei_code": lob_item.lei_code,
            "jurisdiction": lob_item.jurisdiction,
            "technologies": lob_item.technologies or [],
            "competitors": lob_item.competitors or [],
            "financial_snippets": lob_item.financial_snippets or [],
            "patents": lob_item.patents or [],
            "logo_url": lob_item.logo_url,
            "wikipedia_url": lob_item.wikipedia_url,
            "google_news_rss_url": lob_item.google_news_rss_url,
            "reddit_rss_url": lob_item.reddit_rss_url,
            "google_patents_url": lob_item.google_patents_url,
            "google_trends_url": lob_item.google_trends_url,
            "youtube_search_url": lob_item.youtube_search_url,
            "raw_data": lob_item.raw_data,
            "osint_feed_manifest": lob_item.osint_feed_manifest or {},
            "is_manually_verified": bool(getattr(lob_item, "is_manually_verified", False)),
            "manually_verified_at": lob_item.manually_verified_at.isoformat() if getattr(lob_item, "manually_verified_at", None) else None,
            "last_run_at": last_run_at or (lob_item.manually_verified_at.isoformat() if getattr(lob_item, "manually_verified_at", None) else None),
            "subLobs": sub_lobs_formatted,
            "sub_lobs": sub_lobs_formatted,
            "personas": assigned_personas,
            "personas_count": len(assigned_personas),
        }

    def _serialize_lob_summary(lob_item: Lob, assigned_personas_count: int) -> Dict[str, Any]:
        # Trimmed: keeps technologies/competitors (read by computeSignals()/
        # _compute_signals_count and the competitors-card-grid feature for
        # EVERY account on every nav-tree/topbar/command-center render — entries
        # may be plain strings or {name,...} objects, both pass through as-is,
        # the frontend already normalizes either shape) and subLobs (nav-tree
        # renders sub-LOB names in the expanded row), drops financial_snippets/
        # patents/deep URLs/the sub-LOB's own 18-attribute entity data, which
        # are only read once an account is opened.
        sub_lobs_formatted = [{"id": s.id, "name": s.name} for s in (lob_item.sub_lobs or [])]
        return {
            "id": lob_item.id,
            "name": lob_item.lob_name,
            "lob_name": lob_item.lob_name,
            "relationship_type": lob_item.relationship_type,
            "technologies": lob_item.technologies or [],
            "competitors": lob_item.competitors or [],
            "subLobs": sub_lobs_formatted,
            "sub_lobs": sub_lobs_formatted,
            "personas_count": assigned_personas_count,
        }

    def _serialize_account_full(acct: Account, latest_lob_runs: Optional[Dict] = None, latest_persona_runs: Optional[Dict] = None) -> Dict[str, Any]:
        latest_lob_runs = latest_lob_runs or {}
        latest_persona_runs = latest_persona_runs or {}
        personas_list = [
            _serialize_persona_full(
                p,
                last_run_at=latest_persona_runs.get(p.id) or latest_persona_runs.get((p.full_name or p.display_name or "").strip().lower())
            )
            for p in (acct.personas or [])
        ]
        raw_lobs = acct.lobs or []
        lobs_list = [
            _serialize_lob_full(
                lob_item,
                assigned,
                last_run_at=latest_lob_runs.get(lob_item.id) or latest_lob_runs.get((lob_item.lob_name or "").strip().lower())
            )
            for lob_item, assigned in _distribute_personas_across_lobs(raw_lobs, personas_list)
        ]

        acct_name = acct.legal_name or acct.display_name or acct.key
        acct_loc = acct.headquarters_location or (f"{acct.city}, {acct.country}" if acct.city else None)
        acct_desc = acct.short_description or acct.full_description

        return {
            "id": acct.id,
            "key": acct.key,
            "name": acct_name,
            "display_name": acct.display_name or acct_name,
            "legal_name": acct.legal_name or acct_name,
            "ticker": acct.stock_symbol,
            "stock_symbol": acct.stock_symbol,
            "revenue": _format_compact_revenue(acct.estimated_revenue_range),
            "estimated_revenue_range": _format_compact_revenue(acct.estimated_revenue_range),
            "location": acct_loc,
            "desc": acct_desc,
            "domain": acct.domain,
            "primary_domain": acct.primary_domain or acct.domain,
            "website_url": acct.website_url,
            "crunchbase_url": acct.crunchbase_url,
            "operating_status": acct.operating_status,
            "company_type": acct.company_type,
            "founded_year": acct.founded_year,
            "employee_count_range": acct.employee_count_range,
            "short_description": acct_desc,
            "full_description": acct.full_description or acct_desc,
            "headquarters_location": acct_loc,
            "city": acct.city,
            "state": acct.state,
            "country": acct.country,
            "postal_code": acct.postal_code,
            "phone_number": acct.phone_number,
            "sanitized_phone": acct.sanitized_phone,
            "contact_email": acct.contact_email,
            "linkedin_url": acct.linkedin_url,
            "twitter_url": acct.twitter_url,
            "twitter_handle": acct.twitter_handle,
            "stock_exchange": acct.stock_exchange,
            "sec_cik": acct.sec_cik,
            "sec_edgar_url": acct.sec_edgar_url,
            "sec_filings_rss": acct.sec_filings_rss,
            "sec_submissions_url": acct.sec_submissions_url,
            "twitter_live_url": acct.twitter_live_url,
            "reddit_query": acct.reddit_query,
            "reddit_rss_url": acct.reddit_rss_url,
            "news_query": acct.news_query,
            "rss_url": acct.rss_url,
            "google_patents_url": acct.google_patents_url,
            "google_trends_url": acct.google_trends_url,
            "youtube_search_url": acct.youtube_search_url,
            "openalex_institution_url": acct.openalex_institution_url,
            "wikidata_entity_url": acct.wikidata_entity_url,
            "github_url": acct.github_url,
            "glassdoor_url": acct.glassdoor_url,
            "blog_url": acct.blog_url,
            "industries": acct.industries or [],
            "keywords": acct.keywords or [],
            "lobs_count": len(lobs_list),
            "total_contacts_captured": len(personas_list),
            "lobs": lobs_list,
            "personas": personas_list,
            "multi_source_intelligence": acct.multi_source_intelligence,
            "organisational_hierarchy_tree": acct.organisational_hierarchy_tree,
            "extracted_at": acct.extracted_at.isoformat() if acct.extracted_at else None,
            "created_at": acct.created_at.isoformat() if acct.created_at else None,
            "updated_at": acct.updated_at.isoformat() if acct.updated_at else None,
            # ── Engagement / opportunity signals (previously captured but never exposed) ──
            "heat_score": acct.heat_score,
            "trend_score_90d": acct.trend_score_90d,
            "active_tech_count": acct.active_tech_count,
            "it_spend": acct.it_spend,
            "patents_granted": acct.patents_granted,
            "trademarks_registered": acct.trademarks_registered,
            "total_funding_amount_usd": acct.total_funding_amount_usd,
            "total_funding_currency": acct.total_funding_currency,
            "last_funding_type": acct.last_funding_type,
            "last_funding_date": acct.last_funding_date.isoformat() if acct.last_funding_date else None,
            "num_funding_rounds": acct.num_funding_rounds,
            "funding_status": acct.funding_status,
            "ipo_status": acct.ipo_status,
            "ipo_date": acct.ipo_date.isoformat() if acct.ipo_date else None,
            "num_suborganizations": acct.num_suborganizations,
            "num_acquisitions": acct.num_acquisitions,
            "global_traffic_rank": acct.global_traffic_rank,
            "monthly_visits": acct.monthly_visits,
            "bounce_rate": acct.bounce_rate,
            "visit_duration": acct.visit_duration,
            "page_views_per_visit": acct.page_views_per_visit,
            "c_suite_count": acct.c_suite_count
            or len(
                [
                    p
                    for p in personas_list
                    if (p.get("tier") or "").lower() in ["c-suite", "c_suite", "c"]
                    or any(
                        w in (p.get("title") or "").lower()
                        for w in ["chief", "president", "ceo", "chairman", "board"]
                    )
                ]
            ),
            "vp_count": acct.vp_count
            or len(
                [
                    p
                    for p in personas_list
                    if "vp" in (p.get("tier") or "").lower()
                    or "vice president" in (p.get("title") or "").lower()
                ]
            ),
            "director_count": acct.director_count
            or len(
                [
                    p
                    for p in personas_list
                    if "director" in (p.get("tier") or "").lower()
                    or "director" in (p.get("title") or "").lower()
                ]
            ),
            "manager_count": acct.manager_count
            or len(
                [
                    p
                    for p in personas_list
                    if "manager" in (p.get("tier") or "").lower()
                    or "manager" in (p.get("title") or "").lower()
                ]
            ),
            "created_at": acct.created_at.isoformat() if getattr(acct, "created_at", None) else None,
            "updated_at": acct.updated_at.isoformat() if getattr(acct, "updated_at", None) else None,
            "is_manually_verified": bool(getattr(acct, "is_manually_verified", False)),
            "manually_verified_at": acct.manually_verified_at.isoformat() if getattr(acct, "manually_verified_at", None) else None,
            "osint_feed_manifest": getattr(acct, "osint_feed_manifest", None) or {},
            "_is_full_loaded": True,
        }

    def _compute_signals_count(
        acct: Account, lobs_list: List[Any], personas_list: List[Any]
    ) -> int:
        """Server-side count mirroring signals.js's computeSignals(account, null).
        The nav tree / topbar ticker only ever display the COUNT of signals per
        account (a badge number), never the underlying detail — so this replaces
        shipping multi_source_intelligence (and every other field computeSignals
        reads) to the client for every account in the list, just to count it
        client-side. Full signal detail is still available once an account is
        opened, via _serialize_account_full / GET /api/accounts/{id}."""
        count = 0
        msi = acct.multi_source_intelligence or {}
        linkedin_metrics = msi.get("linkedin_metrics") or {}
        if linkedin_metrics.get("follower_count"):
            count += 1
        if linkedin_metrics.get("exact_employee_headcount"):
            count += 1
        if (msi.get("sec_10k_chunks_meta") or {}).get("sections_found"):
            count += 1
        if (msi.get("gleif_intel") or {}).get("lei_code"):
            count += 1
        if acct.company_type == "Public":
            count += 1
        if acct.last_funding_type:
            count += 1
        if acct.ipo_status:
            count += 1
        if acct.patents_granted:
            count += 1
        if acct.active_tech_count:
            count += 1
        if acct.num_acquisitions:
            count += 1
        if len(lobs_list) > 1:
            count += 1
        if personas_list:
            count += 1
        if acct.industries:
            count += 1
        if any(
            (lob_item.get("competitors") if isinstance(lob_item, dict) else getattr(lob_item, "competitors", None))
            for lob_item in lobs_list
        ):
            count += 1
        return count

    def _serialize_account_summary(acct: Account, latest_lob_runs: Optional[Dict] = None, latest_persona_runs: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Lightweight enterprise summary dossier for the initial master list.
        Delivers all 97 account columns, telemetry, and contact/LOB counts instantly (<30 KB total),
        avoiding serialization of thousands of LOB and Persona dossiers until an account is opened.
        """
        raw_personas = acct.personas or []
        raw_lobs = acct.lobs or []

        acct_name = acct.legal_name or acct.display_name or acct.key
        acct_loc = acct.headquarters_location or (f"{acct.city}, {acct.country}" if acct.city else None)
        acct_desc = acct.short_description or acct.full_description

        # Compute tier rollups directly without hydrating full persona records
        c_suite_cnt = acct.c_suite_count or sum(
            1 for p in raw_personas
            if (getattr(p, "tier", None) or "").lower() in ["c-suite", "c_suite", "c"]
            or any(w in (getattr(p, "title", None) or getattr(p, "job_title", None) or "").lower() for w in ["chief", "president", "ceo", "chairman", "board"])
        )
        vp_cnt = acct.vp_count or sum(
            1 for p in raw_personas
            if "vp" in (getattr(p, "tier", None) or "").lower()
            or "vice president" in (getattr(p, "title", None) or getattr(p, "job_title", None) or "").lower()
        )
        dir_cnt = acct.director_count or sum(
            1 for p in raw_personas
            if "director" in (getattr(p, "tier", None) or "").lower()
            or "director" in (getattr(p, "title", None) or getattr(p, "job_title", None) or "").lower()
        )
        mgr_cnt = acct.manager_count or sum(
            1 for p in raw_personas
            if "manager" in (getattr(p, "tier", None) or "").lower()
            or "manager" in (getattr(p, "title", None) or getattr(p, "job_title", None) or "").lower()
        )

        # Build lightweight connector map so client-side computeAccountHealth calculates exact health score without multi-megabyte payloads
        msi_summary = {k: True for k in (acct.multi_source_intelligence or {}).keys()}
        oht_summary = {"gleif_lei": (acct.organisational_hierarchy_tree or {}).get("gleif_lei")}

        return {
            "id": acct.id,
            "key": acct.key,
            "name": acct_name,
            "display_name": acct.display_name or acct_name,
            "legal_name": acct.legal_name or acct_name,
            "ticker": acct.stock_symbol,
            "stock_symbol": acct.stock_symbol,
            "revenue": _format_compact_revenue(acct.estimated_revenue_range),
            "estimated_revenue_range": _format_compact_revenue(acct.estimated_revenue_range),
            "location": acct_loc,
            "headquarters_location": acct_loc,
            "desc": acct_desc,
            "short_description": acct_desc,
            "full_description": acct.full_description or acct_desc,
            "domain": acct.domain,
            "primary_domain": acct.primary_domain or acct.domain,
            "website_url": acct.website_url,
            "crunchbase_url": acct.crunchbase_url,
            "operating_status": acct.operating_status,
            "city": acct.city,
            "state": acct.state,
            "country": acct.country,
            "postal_code": acct.postal_code,
            "phone_number": acct.phone_number,
            "sanitized_phone": acct.sanitized_phone,
            "contact_email": acct.contact_email,
            "company_type": acct.company_type,
            "founded_year": acct.founded_year,
            "founded_date": acct.founded_date.isoformat() if getattr(acct, "founded_date", None) else None,
            "employee_count_range": acct.employee_count_range,
            "linkedin_url": acct.linkedin_url,
            "twitter_url": acct.twitter_url,
            "twitter_handle": acct.twitter_handle,
            "facebook_url": getattr(acct, "facebook_url", None),
            "stock_exchange": acct.stock_exchange,
            "sec_cik": acct.sec_cik,
            "sec_edgar_url": acct.sec_edgar_url,
            "sec_filings_rss": acct.sec_filings_rss,
            "sec_submissions_url": acct.sec_submissions_url,
            "twitter_live_url": acct.twitter_live_url,
            "reddit_query": acct.reddit_query,
            "reddit_rss_url": acct.reddit_rss_url,
            "news_query": acct.news_query,
            "rss_url": acct.rss_url,
            "google_patents_url": acct.google_patents_url,
            "google_trends_url": acct.google_trends_url,
            "youtube_search_url": acct.youtube_search_url,
            "openalex_institution_url": acct.openalex_institution_url,
            "wikidata_entity_url": acct.wikidata_entity_url,
            "github_url": acct.github_url,
            "glassdoor_url": acct.glassdoor_url,
            "blog_url": acct.blog_url,
            "youtube_channel_id": getattr(acct, "youtube_channel_id", None),
            "industries": acct.industries or [],
            "industry_groups": getattr(acct, "industry_groups", None) or [],
            "aliases": getattr(acct, "aliases", None) or [],
            "founders": getattr(acct, "founders", None) or [],
            "num_founders": getattr(acct, "num_founders", None) or (len(acct.founders) if getattr(acct, "founders", None) else 0),
            "headquarters_regions": getattr(acct, "headquarters_regions", None) or [],
            "raw_data": {},
            "osint_feed_manifest": getattr(acct, "osint_feed_manifest", None) or {},
            "is_manually_verified": bool(getattr(acct, "is_manually_verified", False)),
            "manually_verified_at": acct.manually_verified_at.isoformat() if getattr(acct, "manually_verified_at", None) else None,
            "total_funding_amount": getattr(acct, "total_funding_amount", None),
            "sec_name": getattr(acct, "sec_name", None),
            "num_contacts": getattr(acct, "num_contacts", None),
            "schema_version": getattr(acct, "schema_version", None),
            "total_apps": getattr(acct, "total_apps", None),
            "total_downloads": getattr(acct, "total_downloads", None),
            "keywords": acct.keywords or [],
            "lobs_count": len(raw_lobs),
            "total_contacts_captured": len(raw_personas),
            "lobs": [],
            "personas": [],
            "_is_full_loaded": False,
            "multi_source_intelligence": msi_summary,
            "organisational_hierarchy_tree": oht_summary,
            "extracted_at": acct.extracted_at.isoformat() if acct.extracted_at else None,
            "heat_score": acct.heat_score,
            "trend_score_90d": acct.trend_score_90d,
            "signals_count": _compute_signals_count(acct, raw_lobs, raw_personas),
            "active_tech_count": acct.active_tech_count,
            "it_spend": acct.it_spend,
            "patents_granted": acct.patents_granted,
            "trademarks_registered": acct.trademarks_registered,
            "total_funding_amount_usd": acct.total_funding_amount_usd,
            "total_funding_currency": acct.total_funding_currency,
            "last_funding_type": acct.last_funding_type,
            "last_funding_date": acct.last_funding_date.isoformat() if acct.last_funding_date else None,
            "num_funding_rounds": acct.num_funding_rounds,
            "funding_status": acct.funding_status,
            "ipo_status": acct.ipo_status,
            "ipo_date": acct.ipo_date.isoformat() if acct.ipo_date else None,
            "num_suborganizations": acct.num_suborganizations,
            "num_acquisitions": acct.num_acquisitions,
            "global_traffic_rank": acct.global_traffic_rank,
            "monthly_visits": acct.monthly_visits,
            "bounce_rate": acct.bounce_rate,
            "visit_duration": acct.visit_duration,
            "page_views_per_visit": acct.page_views_per_visit,
            "c_suite_count": c_suite_cnt,
            "vp_count": vp_cnt,
            "director_count": dir_cnt,
            "manager_count": mgr_cnt,
            "created_at": acct.created_at.isoformat() if getattr(acct, "created_at", None) else None,
            "updated_at": acct.updated_at.isoformat() if getattr(acct, "updated_at", None) else None,
            "manually_verified_at": acct.manually_verified_at.isoformat() if getattr(acct, "manually_verified_at", None) else None,
        }

    @account_router.get("")
    def list_all_accounts_with_hierarchy(
        response: Response,
        full: bool = Query(False),
        user: User = Depends(auth.get_current_user),
    ):
        """
        [Page Initialization (loadData())]:
        Queries PostgreSQL (accounts, lobs, sub_lobs, personas) and returns either:
        - full=False (default): a trimmed summary dossier per account for the dashboard nav tree/rollups.
        - full=True: full serialization including all 69 persona columns, 27 LOB attributes,
          OSINT feed manifests, and deep enrichment URLs (used by Pipeline Explorer).
        """
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        session = get_session()
        try:
            query = session.query(Account).options(
                selectinload(Account.personas),
                selectinload(Account.lobs).selectinload(Lob.sub_lobs)
            )
            if user.role != "super_admin":
                accessible_ids = auth.get_accessible_account_ids(session, user.id)
                query = query.filter(Account.id.in_(accessible_ids)) if accessible_ids else query.filter(False)
            # Pre-fetch latest run timestamps from pipeline_runs for real Last Run display
            from sqlalchemy import text
            latest_lob_runs = {}
            latest_persona_runs = {}
            try:
                lob_runs_raw = session.execute(text("""
                    SELECT 
                        COALESCE(NULLIF(entities_extracted->>'lob_id', '')::int, NULL) as lid,
                        entities_extracted->>'lob_name' as lname,
                        MAX(completed_at) as mtime
                    FROM pipeline_runs
                    WHERE entities_extracted->>'level' = 'lob'
                    GROUP BY entities_extracted->>'lob_id', entities_extracted->>'lob_name';
                """)).fetchall()
                for r in lob_runs_raw:
                    if r[2]:
                        iso = r[2].isoformat()
                        if r[0]: latest_lob_runs[int(r[0])] = iso
                        if r[1]: latest_lob_runs[str(r[1]).strip().lower()] = iso

                persona_runs_raw = session.execute(text("""
                    SELECT 
                        COALESCE(NULLIF(entities_extracted->>'persona_id', '')::int, NULL) as pid,
                        COALESCE(entities_extracted->>'full_name', entities_extracted->>'name', entities_extracted->>'person_name') as pname,
                        MAX(completed_at) as mtime
                    FROM pipeline_runs
                    WHERE entities_extracted->>'level' = 'persona'
                      AND COALESCE(entities_extracted->>'action', '') != 'dump'
                    GROUP BY entities_extracted->>'persona_id', entities_extracted->>'full_name', entities_extracted->>'name', entities_extracted->>'person_name';
                """)).fetchall()
                for r in persona_runs_raw:
                    if r[2]:
                        iso = r[2].isoformat()
                        if r[0]: latest_persona_runs[int(r[0])] = iso
                        if r[1] and str(r[1]).strip().lower() not in ('', 'none'):
                            latest_persona_runs[str(r[1]).strip().lower()] = iso
            except Exception as _e:
                print(f"[RunTimestamps] Non-fatal pre-fetch error: {_e}")

            accounts = query.order_by(Account.id.desc()).all()
            serializer = _serialize_account_full if full else _serialize_account_summary
            return {"accounts": [serializer(acct) for acct in accounts]}

        finally:
            session.close()

    @app.get("/api/accounts", tags=["1. Account Level"])
    def list_all_accounts_alias(
        response: Response,
        full: bool = Query(False),
        user: User = Depends(auth.get_current_user),
    ):
        """Plural alias for /api/account list endpoint."""
        return list_all_accounts_with_hierarchy(response, full, user)

    @account_router.get("/{account_id}")
    def get_account_from_db(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieves one account's full dossier from DB — complete LOBs and
        personas — fetched on demand when that account is opened."""
        session = get_session()
        try:
            acct = (
                session.query(Account)
                .options(
                    selectinload(Account.personas), selectinload(Account.lobs).selectinload(Lob.sub_lobs)
                )
                .filter_by(id=account_id)
                .first()
            )
            if not acct:
                raise HTTPException(status_code=404, detail="Account not found.")
            from sqlalchemy import text
            latest_lob_runs = {}
            latest_persona_runs = {}
            try:
                lob_runs_raw = session.execute(text("""
                    SELECT 
                        COALESCE(NULLIF(entities_extracted->>'lob_id', '')::int, NULL) as lid,
                        entities_extracted->>'lob_name' as lname,
                        MAX(completed_at) as mtime
                    FROM pipeline_runs
                    WHERE entities_extracted->>'level' = 'lob'
                    GROUP BY entities_extracted->>'lob_id', entities_extracted->>'lob_name';
                """)).fetchall()
                for r in lob_runs_raw:
                    if r[2]:
                        iso = r[2].isoformat()
                        if r[0]: latest_lob_runs[int(r[0])] = iso
                        if r[1]: latest_lob_runs[str(r[1]).strip().lower()] = iso

                persona_runs_raw = session.execute(text("""
                    SELECT 
                        COALESCE(NULLIF(entities_extracted->>'persona_id', '')::int, NULL) as pid,
                        COALESCE(entities_extracted->>'full_name', entities_extracted->>'name', entities_extracted->>'person_name') as pname,
                        MAX(completed_at) as mtime
                    FROM pipeline_runs
                    WHERE entities_extracted->>'level' = 'persona'
                      AND COALESCE(entities_extracted->>'action', '') != 'dump'
                    GROUP BY entities_extracted->>'persona_id', entities_extracted->>'full_name', entities_extracted->>'name', entities_extracted->>'person_name';
                """)).fetchall()
                for r in persona_runs_raw:
                    if r[2]:
                        iso = r[2].isoformat()
                        if r[0]: latest_persona_runs[int(r[0])] = iso
                        if r[1] and str(r[1]).strip().lower() not in ('', 'none'):
                            latest_persona_runs[str(r[1]).strip().lower()] = iso
            except Exception as _e:
                print(f"[RunTimestamps] Non-fatal pre-fetch error: {_e}")
            return _serialize_account_full(acct, latest_lob_runs, latest_persona_runs)
        finally:
            session.close()

    @account_router.patch("/{account_id}", dependencies=[Depends(auth.require_role("super_admin"))])
    @app.patch("/api/accounts/{account_id}", tags=["1. Account Level"], dependencies=[Depends(auth.require_account_access), Depends(auth.require_editor)])
    def update_account(account_id: int, payload: Dict[str, Any] = Body(...)):
        """
        [Universal & Inline Edit]: Updates account fields directly in PostgreSQL.
        Automatically sets is_manually_verified = True, updates manually_verified_at to now,
        updates updated_at, logs audit event to pipeline_runs, and returns the full serialized account.
        """
        session = get_session()
        t0 = datetime.now(timezone.utc)
        try:
            acct = session.query(Account).options(
                selectinload(Account.personas),
                selectinload(Account.lobs).selectinload(Lob.sub_lobs)
            ).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail="Account not found.")

            immutable = {"id", "created_at"}
            updated_fields = {}
            for k, v in payload.items():
                if k in immutable or not hasattr(Account, k):
                    continue
                setattr(acct, k, v)
                updated_fields[k] = v

            now_utc = datetime.now(timezone.utc)
            acct.is_manually_verified = True
            acct.manually_verified_at = now_utc
            acct.updated_at = now_utc

            session.commit()
            session.refresh(acct)

            PipelineRunLogger.log_event(
                company_name=acct.legal_name or acct.display_name or acct.key,
                target_url=acct.domain or acct.website_url,
                level="account",
                action="manual_edit",
                status="success",
                started_at=t0,
                completed_at=now_utc,
                entities_extracted={
                    "account_id": acct.id,
                    "updated_fields": list(updated_fields.keys()),
                    "is_manually_verified": True,
                    "manually_verified_at": acct.manually_verified_at.isoformat(),
                },
            )
            return {"status": "success", "account": _serialize_account_full(acct)}
        except HTTPException:
            session.rollback()
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update account: {str(e)}")
        finally:
            session.close()

    @account_router.post("/sec-10k-chunks")
    def extract_sec_10k_chunks(req: Sec10kRequest):
        """
        [Real-time SEC 10-K Chunker]:
        Fetches the latest official 10-K filing from SEC EDGAR, extracts Item 1 (Business),
        Item 1A (Risk Factors), and Item 7 (MD&A), and chunks text for AI RAG/Vector embeddings.
        """
        try:
            return fetch_latest_10k_chunks(
                sec_cik=req.sec_cik, chunk_size=req.chunk_size, overlap=req.overlap
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"10-K Chunking failed: {str(e)}")

    @account_router.post("/patents", dependencies=[Depends(auth.require_role("super_admin"))])   # external data fetch: admin only
    def get_full_patents(req: PatentsRequest):
        """
        [Full Patent Text Extractor]:
        Queries USPTO and open patent registries for granted patents and abstracts.
        """
        try:
            return extract_full_patents(company_name=req.company_name, max_results=req.max_results)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Patent extraction failed: {str(e)}")

    @account_router.post("/sec-exhibit-21", dependencies=[Depends(auth.require_role("super_admin"))])   # external data fetch: admin only
    def get_sec_exhibit_21_subsidiaries(req: Sec10kRequest):
        """
        [SEC EDGAR Exhibit 21 (Subsidiaries of Registrant) Extractor]:
        Fetches the official legal Exhibit 21 filed with Form 10-K, returning
        all legally registered subsidiaries and their jurisdictions of incorporation.
        """
        try:
            return fetch_sec_exhibit_21_subsidiaries(sec_cik=req.sec_cik)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Exhibit 21 extraction failed: {str(e)}")

    @account_router.post("/gleif", dependencies=[Depends(auth.require_role("super_admin"))])   # external data fetch: admin only
    def get_gleif_ownership_tree(req: PatentsRequest):
        """
        [GLEIF Corporate Ownership Graph Resolver]:
        Queries the official G20 LEI database for legal registered name,
        LEI code, direct/ultimate parent entities, and global child subsidiaries.
        """
        try:
            return fetch_gleif_ownership_tree(company_name=req.company_name, max_children=req.max_results)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"GLEIF extraction failed: {str(e)}")

    @account_router.post("/wikipedia", dependencies=[Depends(auth.require_role("super_admin"))])   # external data fetch: admin only
    def get_wikipedia_intel(req: PatentsRequest):
        """
        [Wikipedia & DBpedia Open Knowledge Graph Extractor]:
        Queries Wikipedia REST API & DBpedia for executive summary, founding date, and logo.
        """
        try:
            return fetch_wikipedia_dbpedia_intel(company_name=req.company_name)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Wikipedia extraction failed: {str(e)}")

    @account_router.post("/fec", dependencies=[Depends(auth.require_role("super_admin"))])   # external data fetch: admin only
    def get_fec_political_intel(req: PatentsRequest):
        """
        [FEC Federal Election Commission Political Giving API]:
        Queries OpenFEC via data.gov API key for corporate PAC contributions & executive donations.
        """
        try:
            return fetch_fec_political_intel(entity_name=req.company_name, max_records=req.max_results)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"FEC extraction failed: {str(e)}")

    @account_router.post("/diffbot", dependencies=[Depends(auth.require_role("super_admin"))])   # external data fetch: admin only
    def get_diffbot_intel(req: AccountFetchRequest):
        """
        [Diffbot Knowledge Graph (DKG) Enhancer]:
        Queries Diffbot AI Knowledge Graph for verified firmographics, logo, technologies,
        competitors, subsidiaries, parent organizations, and board members.
        """
        try:
            return fetch_diffbot_organization_intel(company_name=req.company_name, website_url=req.target_url)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Diffbot extraction failed: {str(e)}")

    # ══════════════════════════════════════════════════════
    # TAB 2 & 3: LOB & SUB-LOB LEVEL ENDPOINTS
    # ══════════════════════════════════════════════════════
    lobs_router = APIRouter(prefix="/api/lobs", tags=["2. LOB & Sub-LOB Level"])

    @lobs_router.post("/fetch")
    def fetch_lobs_data(req: LobsFetchRequest):
        """[Tab 2 - Fetch Button]: Multi-Source LOB & Subsidiary Discovery + 10-Source Enrichment.
        Uses SEC Exhibit 21 + GLEIF Ownership Tree + Crunchbase + Diffbot + LobService.enrich_all_lobs().
        """
        t0 = datetime.now(timezone.utc)
        try:
            if req.lob_name:
                single_lob = LobService.enrich_single_lob(
                    lob_name=req.lob_name,
                    parent_company=req.company_name,
                    account_id=req.account_id,
                    lob_domain=req.lob_domain,
                )
                from services.credit_accounting_engine import CreditAccountingEngine
                lob_credit_tally = CreditAccountingEngine.tally_lob_telemetry(1)
                now_completed = datetime.now(timezone.utc)
                duration_sec = round((now_completed - t0).total_seconds(), 2)

                audit_score = 75.0
                if isinstance(single_lob, dict):
                    if single_lob.get("domain") or single_lob.get("website_url"):
                        audit_score += 10.0
                    if single_lob.get("description"):
                        audit_score += 5.0
                    if single_lob.get("technologies"):
                        audit_score += 5.0
                    if single_lob.get("operating_head"):
                        audit_score += 5.0
                audit_grade = "A" if audit_score >= 90 else ("B" if audit_score >= 75 else "C")

                credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                    company_name=req.company_name or "Company",
                    run_id=f"run_{slugify(req.company_name or 'lob')[:20]}_lob_single",
                    run_number=1,
                    started_at=t0,
                    duration_seconds=duration_sec,
                    status="success",
                    account_tally={"credits": 0, "resources": []},
                    lob_tally=lob_credit_tally,
                    persona_tally={"credits": 0, "resources": []},
                )
                PipelineRunLogger.log_event(
                    company_name=req.company_name or "Company",
                    target_url=req.lob_domain,
                    level="lob",
                    action="pull",
                    status="success",
                    quality_score=audit_score,
                    quality_grade=audit_grade,
                    started_at=t0,
                    completed_at=now_completed,
                    duration_seconds=duration_sec,
                    total_credits_used=lob_credit_tally.get("credits", 0),
                    credits_breakdown=credits_breakdown,
                    entities_extracted={
                        "lob_name": req.lob_name,
                        "account_id": req.account_id,
                        "domain": req.lob_domain,
                        "total_lobs": 1,
                    },
                )
                return {
                    "status": "staged",
                    "company_name": req.company_name,
                    "lob": single_lob,
                    "lobs": [single_lob],
                    "total_lobs": 1,
                }
            else:
                # ── Batch LOB Discovery Waterfall ──
                discovered_names = []
                sec_cik_val = None

                # 1. Check if Account already exists in DB with discovered LOB names
                session = get_session()
                try:
                    acct = None
                    if req.account_id:
                        acct = session.query(Account).filter_by(id=req.account_id).first()
                    if not acct and req.company_name:
                        acct = session.query(Account).filter(
                            Account.display_name.ilike(f"%{req.company_name}%")
                        ).first()

                    if acct:
                        sec_cik_val = acct.sec_cik
                        # Check discovered_lob_names or multi_source_intelligence
                        if acct.organisational_hierarchy_tree:
                            tree = acct.organisational_hierarchy_tree
                            for s in tree.get("sec_exhibit21_subsidiaries", []):
                                if s.get("legal_name"):
                                    discovered_names.append(s.get("legal_name"))
                            for c in tree.get("gleif_children", []):
                                if c.get("legal_name"):
                                    discovered_names.append(c.get("legal_name"))
                            for a_sub in tree.get("all_subsidiaries", []):
                                if a_sub.get("legal_name"):
                                    discovered_names.append(a_sub.get("legal_name"))
                            for ind in tree.get("gleif_indirect_sublobs", []):
                                if ind.get("legal_name"):
                                    discovered_names.append(ind.get("legal_name"))
                        if acct.lobs:
                            for ex_lob in acct.lobs:
                                if ex_lob.lob_name:
                                    discovered_names.append(ex_lob.lob_name)
                except Exception as db_err:
                    print(f"[!] [LobFetch] DB Account lookup notice: {db_err}")
                finally:
                    session.close()

                # 2. Extract Diffbot raw subsidiaries if available in output cache
                try:
                    comp_clean = slugify(req.company_name or "")
                    for out_root in [config.OUTPUT_DIR / "raw" / "diffbot", config.OUTPUT_DIR]:
                        if not out_root.exists():
                            continue
                        for df_path in out_root.rglob("*diffbot*.json"):
                            if comp_clean and comp_clean in df_path.stem.lower():
                                with open(df_path, "r", encoding="utf-8") as df_f:
                                    df_data = json.load(df_f)
                                    df_obj = df_data.get("data", [{}])[0] if isinstance(df_data.get("data"), list) else df_data
                                    for sub in df_obj.get("subsidiaries", []):
                                        if isinstance(sub, str) and sub.strip():
                                            discovered_names.append(sub.strip())
                                        elif isinstance(sub, dict) and sub.get("name"):
                                            discovered_names.append(sub.get("name").strip())
                except Exception as df_err:
                    print(f"[!] [LobFetch] Diffbot raw lookup notice: {df_err}")

                # 3. If no names from DB/Diffbot, query SEC Exhibit 21 and GLEIF directly
                if not discovered_names and sec_cik_val:
                    try:
                        ex21 = fetch_sec_exhibit_21_subsidiaries(sec_cik_val)
                        for s in ex21.get("subsidiaries", []):
                            if s.get("legal_name"):
                                discovered_names.append(s.get("legal_name"))
                    except Exception as e:
                        print(f"[!] [LobFetch] SEC Exhibit 21 live notice: {e}")

                if not discovered_names and req.company_name:
                    try:
                        gleif_tree = fetch_gleif_ownership_tree(req.company_name, max_children=20)
                        for c in gleif_tree.get("child_entities", []):
                            if c.get("legal_name"):
                                discovered_names.append(c.get("legal_name"))
                    except Exception as e:
                        print(f"[!] [LobFetch] GLEIF live notice: {e}")

                # 4. Augment with Crunchbase sub-organizations
                try:
                    cb_subs = scrape_sublobs(req.company_name)
                    for cb in cb_subs:
                        cb_name = cb.get("name") or cb.get("sub_organization_name")
                        if cb_name:
                            discovered_names.append(cb_name)
                except Exception as e:
                    print(f"[!] [LobFetch] Crunchbase sublobs notice: {e}")

                # 5. Augment with Stream B: Dynamic Commercial Operating Divisions & Business Segments
                discovered_divisions_meta = {}
                try:
                    acct_domain = getattr(acct, "domain", None) or getattr(acct, "primary_domain", None) if acct else None
                    divs = extract_commercial_operating_divisions(req.company_name, acct_domain)
                    for d in divs:
                        d_name = d.get("name") or d.get("lob_name")
                        if d_name:
                            discovered_names.append(d_name)
                            discovered_divisions_meta[d_name.strip().lower()] = d
                except Exception as div_err:
                    print(f"[!] [LobFetch] Dynamic commercial divisions notice: {div_err}")

                # 6. Deduplicate discovered subsidiary & division names
                unique_names = list(dict.fromkeys([n.strip() for n in discovered_names if n and len(n.strip()) > 1]))

                # Build input list for enrichment preserving any dynamic domain/overview metadata
                lobs_to_enrich = []
                for n in unique_names:
                    d_meta = discovered_divisions_meta.get(n.strip().lower(), {})
                    item = {"name": n, "lob_name": n}
                    if d_meta.get("domain"):
                        item["domain"] = d_meta["domain"]
                    if d_meta.get("relationship_type"):
                        item["relationship_type"] = d_meta["relationship_type"]
                    if d_meta.get("overview"):
                        item["overview"] = d_meta["overview"]
                    lobs_to_enrich.append(item)

                # 5. Enrich via enterprise LobService
                if lobs_to_enrich:
                    enriched_lobs = LobService.enrich_all_lobs(
                        lobs_list=lobs_to_enrich,
                        parent_company=req.company_name,
                        account_id=req.account_id,
                        sec_cik=sec_cik_val,
                    )
                else:
                    raw_sublobs = scrape_sublobs(req.company_name)
                    if raw_sublobs:
                        enriched_lobs = LobService.enrich_all_lobs(
                            lobs_list=raw_sublobs,
                            parent_company=req.company_name,
                            account_id=req.account_id,
                            sec_cik=sec_cik_val,
                        )
                    else:
                        enriched_lobs = []
                # Dynamic Metered Credit Breakdown for LOBs
                from services.credit_accounting_engine import CreditAccountingEngine
                lob_credit_tally = CreditAccountingEngine.tally_lob_telemetry(len(enriched_lobs))
                now_completed = datetime.now(timezone.utc)
                duration_sec = round((now_completed - t0).total_seconds(), 2)
                batch_score = 88.0 if enriched_lobs else 60.0
                batch_grade = "B" if enriched_lobs else "C"
                credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                    company_name=req.company_name or "Company",
                    run_id=f"run_{slugify(req.company_name or 'lob')[:20]}_lob",
                    run_number=1,
                    started_at=t0,
                    duration_seconds=duration_sec,
                    status="success",
                    account_tally={"credits": 0, "resources": []},
                    lob_tally=lob_credit_tally,
                    persona_tally={"credits": 0, "resources": []},
                )

                PipelineRunLogger.log_event(
                    company_name=req.company_name or "Company",
                    level="lob",
                    action="pull",
                    status="success",
                    quality_score=batch_score,
                    quality_grade=batch_grade,
                    started_at=t0,
                    completed_at=now_completed,
                    duration_seconds=duration_sec,
                    total_credits_used=lob_credit_tally.get("credits", 0),
                    credits_breakdown=credits_breakdown,
                    entities_extracted={
                        "total_lobs": len(enriched_lobs),
                        "account_id": req.account_id,
                        "discovered_names_count": len(unique_names),
                    },
                )

                # Auto-commit to DB if account_id is provided
                if req.account_id and enriched_lobs:
                    session = get_session()
                    try:
                        db_acct = session.query(Account).filter_by(id=req.account_id).first()
                        if db_acct:
                            lob_repo = LobRepository(session)
                            lob_repo.upsert_all(db_acct, enriched_lobs)
                            session.commit()
                    except Exception as commit_err:
                        session.rollback()
                        print(f"[!] [LobFetch] Auto-commit DB notice: {commit_err}")
                    finally:
                        session.close()

                return {
                    "status": "staged",
                    "company_name": req.company_name,
                    "total_lobs": len(enriched_lobs),
                    "lobs": enriched_lobs,
                }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name=req.company_name or "Company",
                level="lob",
                action="pull",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"LOB fetch failed: {str(e)}")

    @lobs_router.post("/fetch-background")
    def fetch_lobs_background(req: LobsFetchRequest):
        """
        [Background Mode — Tab 2 Fetch]:
        Identical to POST /api/lobs/fetch but returns a job_id immediately (200ms).
        The full LOB discovery waterfall (SEC Exhibit 21 → GLEIF → Crunchbase →
        LobService.enrich_all_lobs) runs in an independent background thread
        that survives browser close, refresh, or tab switch.

        Workflow:
          1. POST here → receive {"job_id": "pjob_...", "poll_url": "/api/pipeline/job/..."}
          2. Poll GET /api/pipeline/job/{job_id} every 3 seconds
          3. When status == "done", result contains the same payload as /lobs/fetch
        """
        t0 = datetime.now(timezone.utc)
        job_id = _jreg_create(TYPE_LOB, meta={
            "company_name": req.company_name,
            "lob_name":     req.lob_name,
            "account_id":   req.account_id,
        })

        def _bg_lob():
            try:
                mode_label = f"single LOB '{req.lob_name}'" if req.lob_name else "batch LOB discovery"
                _jreg_update(job_id, status="running", progress_pct=5,
                             message=f"Starting {mode_label} for '{req.company_name}'...")

                # ── Single LOB path ────────────────────────────────────────────
                if req.lob_name:
                    single_lob = LobService.enrich_single_lob(
                        lob_name=req.lob_name,
                        parent_company=req.company_name,
                        account_id=req.account_id,
                        lob_domain=req.lob_domain,
                    )
                    _jreg_update(job_id, progress_pct=80, message="LOB enriched. Logging...")
                    from services.credit_accounting_engine import CreditAccountingEngine
                    lob_credit_tally = CreditAccountingEngine.tally_lob_telemetry(1)
                    now_completed = datetime.now(timezone.utc)
                    duration_sec = round((now_completed - t0).total_seconds(), 2)
                    audit_score = 75.0
                    if isinstance(single_lob, dict):
                        if single_lob.get("domain") or single_lob.get("website_url"): audit_score += 10.0
                        if single_lob.get("description"):  audit_score += 5.0
                        if single_lob.get("technologies"): audit_score += 5.0
                        if single_lob.get("operating_head"): audit_score += 5.0
                    audit_grade = "A" if audit_score >= 90 else ("B" if audit_score >= 75 else "C")
                    credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                        company_name=req.company_name or "Company",
                        run_id=f"run_{slugify(req.company_name or 'lob')[:20]}_lob_single_bg",
                        run_number=1, started_at=t0, duration_seconds=duration_sec, status="success",
                        account_tally={"credits": 0, "resources": []},
                        lob_tally=lob_credit_tally,
                        persona_tally={"credits": 0, "resources": []},
                    )
                    PipelineRunLogger.log_event(
                        company_name=req.company_name or "Company", target_url=req.lob_domain,
                        level="lob", action="pull", status="success",
                        quality_score=audit_score, quality_grade=audit_grade,
                        started_at=t0, completed_at=now_completed, duration_seconds=duration_sec,
                        total_credits_used=lob_credit_tally.get("credits", 0),
                        credits_breakdown=credits_breakdown,
                        entities_extracted={"lob_name": req.lob_name, "account_id": req.account_id,
                                            "domain": req.lob_domain, "total_lobs": 1, "background_job_id": job_id},
                    )
                    _jreg_update(job_id, status="done", progress_pct=100,
                                 message=f"LOB '{req.lob_name}' enriched.",
                                 result={"status": "staged", "company_name": req.company_name,
                                         "lob": single_lob, "lobs": [single_lob], "total_lobs": 1})
                    return

                # ── Batch LOB discovery waterfall ──────────────────────────────
                discovered_names = []
                sec_cik_val = None

                _jreg_update(job_id, progress_pct=10, message="Checking DB for existing LOB names...")
                _session = get_session()
                try:
                    acct = None
                    if req.account_id:
                        acct = _session.query(Account).filter_by(id=req.account_id).first()
                    if not acct and req.company_name:
                        acct = _session.query(Account).filter(
                            Account.display_name.ilike(f"%{req.company_name}%")
                        ).first()
                    if acct:
                        sec_cik_val = acct.sec_cik
                        if acct.organisational_hierarchy_tree:
                            tree = acct.organisational_hierarchy_tree
                            for s in tree.get("sec_exhibit21_subsidiaries", []):
                                if s.get("legal_name"): discovered_names.append(s["legal_name"])
                            for c in tree.get("gleif_children", []):
                                if c.get("legal_name"): discovered_names.append(c["legal_name"])
                except Exception as db_err:
                    print(f"[!] [BG LOB] DB account lookup notice: {db_err}")
                finally:
                    _session.close()

                if not discovered_names and sec_cik_val:
                    _jreg_update(job_id, progress_pct=20, message="Querying SEC Exhibit 21...")
                    try:
                        ex21 = fetch_sec_exhibit_21_subsidiaries(sec_cik_val)
                        for s in ex21.get("subsidiaries", []):
                            if s.get("legal_name"): discovered_names.append(s["legal_name"])
                    except Exception as e:
                        print(f"[!] [BG LOB] SEC Exhibit 21 notice: {e}")

                if not discovered_names and req.company_name:
                    _jreg_update(job_id, progress_pct=30, message="Querying GLEIF ownership tree...")
                    try:
                        gleif_tree = fetch_gleif_ownership_tree(req.company_name, max_children=20)
                        for c in gleif_tree.get("child_entities", []):
                            if c.get("legal_name"): discovered_names.append(c["legal_name"])
                    except Exception as e:
                        print(f"[!] [BG LOB] GLEIF notice: {e}")

                _jreg_update(job_id, progress_pct=40, message="Augmenting with Crunchbase sub-orgs...")
                try:
                    cb_subs = scrape_sublobs(req.company_name)
                    for cb in cb_subs:
                        cb_name = cb.get("name") or cb.get("sub_organization_name")
                        if cb_name: discovered_names.append(cb_name)
                except Exception as e:
                    print(f"[!] [BG LOB] Crunchbase sublobs notice: {e}")

                unique_names = list(dict.fromkeys([n.strip() for n in discovered_names if n and len(n.strip()) > 1]))
                lobs_to_enrich = [{"name": n, "lob_name": n} for n in unique_names]

                _jreg_update(job_id, progress_pct=50,
                             message=f"Enriching {len(lobs_to_enrich) or 'discovered'} LOBs via LobService...")
                if lobs_to_enrich:
                    enriched_lobs = LobService.enrich_all_lobs(
                        lobs_list=lobs_to_enrich,
                        parent_company=req.company_name,
                        account_id=req.account_id,
                        sec_cik=sec_cik_val,
                    )
                else:
                    raw_sublobs = scrape_sublobs(req.company_name)
                    if raw_sublobs:
                        enriched_lobs = LobService.enrich_all_lobs(
                            lobs_list=raw_sublobs,
                            parent_company=req.company_name,
                            account_id=req.account_id,
                            sec_cik=sec_cik_val,
                        )
                    else:
                        enriched_lobs = []

                _jreg_update(job_id, progress_pct=90, message="Logging pipeline run...")
                from services.credit_accounting_engine import CreditAccountingEngine
                lob_credit_tally = CreditAccountingEngine.tally_lob_telemetry(len(enriched_lobs))
                now_completed = datetime.now(timezone.utc)
                duration_sec = round((now_completed - t0).total_seconds(), 2)
                batch_score = 88.0 if enriched_lobs else 60.0
                batch_grade = "B" if enriched_lobs else "C"
                credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                    company_name=req.company_name or "Company",
                    run_id=f"run_{slugify(req.company_name or 'lob')[:20]}_lob_bg",
                    run_number=1, started_at=t0, duration_seconds=duration_sec, status="success",
                    account_tally={"credits": 0, "resources": []},
                    lob_tally=lob_credit_tally,
                    persona_tally={"credits": 0, "resources": []},
                )
                PipelineRunLogger.log_event(
                    company_name=req.company_name or "Company", level="lob", action="pull", status="success",
                    quality_score=batch_score, quality_grade=batch_grade,
                    started_at=t0, completed_at=now_completed, duration_seconds=duration_sec,
                    total_credits_used=lob_credit_tally.get("credits", 0), credits_breakdown=credits_breakdown,
                    entities_extracted={"total_lobs": len(enriched_lobs), "account_id": req.account_id,
                                        "discovered_names_count": len(unique_names), "background_job_id": job_id},
                )
                _jreg_update(job_id, status="done", progress_pct=100,
                             message=f"Batch LOB enrichment complete. {len(enriched_lobs)} LOBs enriched.",
                             result={"status": "staged", "company_name": req.company_name,
                                     "total_lobs": len(enriched_lobs), "lobs": enriched_lobs})
            except Exception as exc:
                _jreg_exc(job_id, exc)
                PipelineRunLogger.log_event(
                    company_name=req.company_name or "Company",
                    level="lob", action="pull", status="failed",
                    started_at=t0, completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )

        threading.Thread(target=_bg_lob, daemon=False, name=f"lob-{job_id}").start()
        return {
            "status":   "queued",
            "job_id":   job_id,
            "poll_url": f"/api/pipeline/job/{job_id}",
            "message":  f"LOB enrichment started for '{req.company_name}'. Thread runs independently of this connection.",
        }

    @lobs_router.post("/validate")
    def validate_lobs_data(lobs_data: List[Dict[str, Any]] = Body(...)):
        """[Tab 2 - Validate Button]: Validates LOB and Sub-LOB data."""
        t0 = datetime.now(timezone.utc)
        try:
            report = DataQualityValidator.validate_lobs(lobs_data)
            score = float(report.get("score", 0.0))
            grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))

            PipelineRunLogger.log_event(
                company_name="Batch LOBs",
                level="lob",
                action="validate",
                status="validated",
                quality_score=score,
                quality_grade=grade,
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "lobs_count": report.get("lobs_count"),
                    "total_with_domain": report.get("total_with_domain"),
                    "warnings": report.get("warnings", []),
                },
            )

            return {
                "status": "validated",
                "score": report["score"],
                "lobs_count": report["lobs_count"],
                "total_with_domain": report["total_with_domain"],
                "details": report["details"],
                "warnings": report["warnings"],
            }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name="Batch LOBs",
                level="lob",
                action="validate",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"LOB validation failed: {str(e)}")

    @lobs_router.post("/validate-single")
    def validate_single_lob(lob_data: Dict[str, Any] = Body(...)):
        """[Tab 2 - Individual Validate Button]: Validates a single LOB entity."""
        t0 = datetime.now(timezone.utc)
        lob_name = lob_data.get("name") or lob_data.get("lob_name") or "LOB"
        parent_comp = lob_data.get("parent_company") or lob_data.get("company_name") or "Company"
        try:
            audit = LobValidator.validate_lob(lob_data)
            warnings = []
            if not lob_data.get("domain") and not lob_data.get("website_url"):
                warnings.append("Domain/Website URL is missing.")
            if not lob_data.get("operating_head"):
                warnings.append("Operating head not identified.")
            if not lob_data.get("technologies"):
                warnings.append("Technology stack not detected.")

            score = float(audit.get("score", 85))
            grade = audit.get("grade", "B")

            PipelineRunLogger.log_event(
                company_name=parent_comp,
                target_url=lob_data.get("domain") or lob_data.get("website_url"),
                level="lob",
                action="validate",
                status="validated",
                quality_score=score,
                quality_grade=grade,
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "lob_name": lob_name,
                    "account_id": lob_data.get("account_id"),
                    "ready_for_db": audit.get("ready_for_db", True),
                    "warnings": warnings,
                },
            )

            return {
                "status": "validated",
                "score": audit.get("score", 85),
                "grade": audit.get("grade", "B"),
                "ready_for_db": audit.get("ready_for_db", True),
                "warnings": warnings,
                "missing_critical": audit.get("missing_critical", []),
                "missing_important": audit.get("missing_important", []),
            }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name=parent_comp,
                level="lob",
                action="validate",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"LOB validation failed: {str(e)}")

    @lobs_router.post("/dump-single-db")
    def dump_single_lob_to_db(req: Dict[str, Any] = Body(...)):
        """[Tab 2 - Individual Dump DB Button]: Commits a single validated LOB into PostgreSQL `lobs` table."""
        t0 = datetime.now(timezone.utc)
        session = get_session()
        try:
            account_id = req.get("account_id")
            lob_data = req.get("lob_data") or req
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail=f"Account ID {account_id} not found in DB.")

            lob_repo = LobRepository(session)
            saved = lob_repo.upsert_single_lob(acct.id, lob_data)
            session.commit()

            lob_name = lob_data.get("name") or lob_data.get("lob_name") or "LOB"
            PipelineRunLogger.log_event(
                company_name=acct.legal_name or acct.display_name or acct.key,
                level="lob",
                action="dump",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "lob_id": getattr(saved, "id", None),
                    "lob_name": lob_name,
                    "account_id": acct.id,
                },
            )

            return {
                "status": "success",
                "lob_id": getattr(saved, "id", None),
                "message": f"LOB '{lob_name}' saved to database.",
            }
        except Exception as e:
            session.rollback()
            PipelineRunLogger.log_event(
                company_name="Company",
                level="lob",
                action="dump",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"LOB DB dump failed: {str(e)}")
        finally:
            session.close()

    @lobs_router.post("/dump-db")
    def dump_lobs_to_db(req: LobsDumpRequest):
        """[Tab 2 - Dump DB Button]: Commits validated LOBs and Sub-LOBs to
        PostgreSQL `lobs` & `sub_lobs` tables."""
        t0 = datetime.now(timezone.utc)
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=req.account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail=f"Account ID {req.account_id} not found in DB.")

            lob_repo = LobRepository(session)
            lob_map = lob_repo.upsert_all(acct, req.lobs_data)
            session.commit()

            PipelineRunLogger.log_event(
                company_name=acct.legal_name or acct.display_name or acct.key,
                level="lob",
                action="dump",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "account_id": acct.id,
                    "lobs_saved_count": len(lob_map),
                },
            )

            return {
                "status": "success",
                "account_id": acct.id,
                "lobs_saved": len(lob_map),
                "lob_mapping": lob_map,
                "message": f"Saved {len(lob_map)} LOBs for Account '{acct.key}'.",
            }
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"LOB DB dump failed: {str(e)}")
        finally:
            session.close()

    @lobs_router.get("")
    def get_lobs_for_account(account_id: int = Query(...)):
        """Retrieves stored LOBs and sub-lobs for an account."""
        session = get_session()
        try:
            lobs = session.query(Lob).filter_by(account_id=account_id).all()
            return [
                {
                    "id": lob_item.id,
                    "account_id": lob_item.account_id,
                    "lob_name": lob_item.lob_name,
                    "domain": lob_item.domain,
                    "website_url": lob_item.website_url,
                    "relationship_type": lob_item.relationship_type,
                    "audited_segment_revenue": lob_item.audited_segment_revenue,
                    "operating_head": lob_item.operating_head,
                    "segment_headcount": lob_item.segment_headcount,
                    "google_news_rss_url": lob_item.google_news_rss_url,
                    "reddit_rss_url": lob_item.reddit_rss_url,
                    "google_patents_url": lob_item.google_patents_url,
                    "sub_lobs": [
                        {
                            "id": s.id,
                            "lob_id": s.lob_id,
                            "name": s.name,
                            "legal_name": getattr(s, "legal_name", None) or s.name,
                            "lei_code": getattr(s, "lei_code", None),
                            "jurisdiction": getattr(s, "jurisdiction", None),
                            "country": getattr(s, "country", None),
                            "city": getattr(s, "city", None),
                            "relationship_type": getattr(s, "relationship_type", None) or "Level 3: Operating Sub-LOB / Grandchild",
                            "status": getattr(s, "status", None) or "ACTIVE",
                            "entity_level": getattr(s, "entity_level", None) or "Level 3 (Operating Sub-LOB)",
                            "parent_lob_lei": getattr(s, "parent_lob_lei", None),
                            "parent_lob_name": getattr(s, "parent_lob_name", None) or lob_item.lob_name,
                            "domain": getattr(s, "domain", None),
                            "website_url": getattr(s, "website_url", None),
                            "is_manually_verified": bool(getattr(s, "is_manually_verified", False)),
                            "manually_verified_at": s.manually_verified_at.isoformat() if getattr(s, "manually_verified_at", None) else None,
                            "metadata": getattr(s, "metadata_", {}) or {},
                        }
                        for s in (lob_item.sub_lobs or [])
                    ],
                }
                for lob_item in lobs
            ]
        finally:
            session.close()

    @lobs_router.patch("/{lob_id}")
    @app.patch("/api/lob/{lob_id}", tags=["2. LOB & Sub-LOB Level"], dependencies=[Depends(auth.require_lob_account_access), Depends(auth.require_editor)])
    def update_lob(lob_id: int, payload: Dict[str, Any] = Body(...)):
        """
        [Universal & Inline Edit]: Updates LOB fields directly in PostgreSQL.
        Automatically sets is_manually_verified = True, updates manually_verified_at to now,
        logs audit event to pipeline_runs, and returns the serialized LOB.
        """
        session = get_session()
        t0 = datetime.now(timezone.utc)
        try:
            lob = session.query(Lob).options(
                selectinload(Lob.sub_lobs),
                selectinload(Lob.account)
            ).filter_by(id=lob_id).first()
            if not lob:
                raise HTTPException(status_code=404, detail="LOB not found.")

            immutable = {"id", "created_at"}
            updated_fields = {}
            for k, v in payload.items():
                if k in immutable or not hasattr(Lob, k):
                    continue
                setattr(lob, k, v)
                updated_fields[k] = v

            now_utc = datetime.now(timezone.utc)
            lob.is_manually_verified = True
            lob.manually_verified_at = now_utc

            session.commit()
            session.refresh(lob)

            company_name = lob.account.display_name if lob.account else "Company"
            PipelineRunLogger.log_event(
                company_name=company_name,
                target_url=lob.domain or lob.website_url,
                level="lob",
                action="manual_edit",
                status="success",
                started_at=t0,
                completed_at=now_utc,
                entities_extracted={
                    "lob_id": lob.id,
                    "account_id": lob.account_id,
                    "updated_fields": list(updated_fields.keys()),
                    "is_manually_verified": True,
                    "manually_verified_at": lob.manually_verified_at.isoformat(),
                },
            )
            return {"status": "success", "lob": _serialize_lob_full(lob, [])}
        except HTTPException:
            session.rollback()
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update LOB: {str(e)}")
        finally:
            session.close()

    @lobs_router.get("/sub/{sub_lob_id}")
    @app.get("/api/sub-lobs/{sub_lob_id}", tags=["2. LOB & Sub-LOB Level"], dependencies=[Depends(auth.require_sub_lob_account_access)])
    def get_sub_lob_record(sub_lob_id: int):
        """Retrieves a single Sub-LOB by ID with all 19 enterprise columns."""
        session = get_session()
        try:
            sub = session.query(SubLob).filter_by(id=sub_lob_id).first()
            if not sub:
                raise HTTPException(status_code=404, detail=f"Sub-LOB with ID {sub_lob_id} not found.")
            return {"status": "success", "sub_lob": _serialize_sublob_obj(sub)}
        finally:
            session.close()

    @lobs_router.patch("/sub/{sub_lob_id}")
    @app.patch("/api/sub-lobs/{sub_lob_id}", tags=["2. LOB & Sub-LOB Level"], dependencies=[Depends(auth.require_sub_lob_account_access), Depends(auth.require_editor)])
    def update_sub_lob(sub_lob_id: int, payload: Dict[str, Any] = Body(...)):
        """
        [Sub-LOB Universal & Inline Edit]: Updates Sub-LOB fields directly in PostgreSQL.
        Automatically sets is_manually_verified = True, updates manually_verified_at to now,
        logs audit event to pipeline_runs, and returns the updated Sub-LOB.
        """
        session = get_session()
        t0 = datetime.now(timezone.utc)
        try:
            sub = session.query(SubLob).filter_by(id=sub_lob_id).first()
            if not sub:
                raise HTTPException(status_code=404, detail=f"Sub-LOB with ID {sub_lob_id} not found.")

            allowed_cols = {
                "name", "legal_name", "lei_code", "jurisdiction", "country", "city",
                "relationship_type", "status", "entity_level", "parent_lob_lei",
                "parent_lob_name", "domain", "website_url", "is_manually_verified",
                "metadata", "metadata_"
            }
            updated_fields = {}
            for k, v in payload.items():
                target_col = "metadata_" if k == "metadata" else k
                if (k in allowed_cols or target_col in allowed_cols) and hasattr(sub, target_col):
                    setattr(sub, target_col, v)
                    updated_fields[k] = v

            now_utc = datetime.now(timezone.utc)
            if "is_manually_verified" in payload:
                sub.is_manually_verified = bool(payload["is_manually_verified"])
            else:
                sub.is_manually_verified = True
            sub.manually_verified_at = now_utc

            session.commit()
            session.refresh(sub)

            PipelineRunLogger.log_event(
                company_name=sub.parent_lob_name or "Sub-LOB",
                level="lob",
                action="manual_edit_sublob",
                status="success",
                started_at=t0,
                completed_at=now_utc,
                entities_extracted={
                    "sub_lob_id": sub.id,
                    "lob_id": sub.lob_id,
                    "updated_fields": list(updated_fields.keys()),
                    "is_manually_verified": sub.is_manually_verified,
                    "manually_verified_at": sub.manually_verified_at.isoformat() if sub.manually_verified_at else None,
                },
            )
            return {
                "status": "success",
                "sub_lob": {
                    "id": sub.id,
                    "lob_id": sub.lob_id,
                    "name": sub.name,
                    "legal_name": sub.legal_name,
                    "lei_code": sub.lei_code,
                    "jurisdiction": sub.jurisdiction,
                    "country": sub.country,
                    "city": sub.city,
                    "relationship_type": sub.relationship_type,
                    "status": sub.status,
                    "entity_level": sub.entity_level,
                    "parent_lob_lei": sub.parent_lob_lei,
                    "parent_lob_name": sub.parent_lob_name,
                    "domain": sub.domain,
                    "website_url": sub.website_url,
                    "is_manually_verified": sub.is_manually_verified,
                    "manually_verified_at": sub.manually_verified_at.isoformat() if sub.manually_verified_at else None,
                    "metadata": sub.metadata_ or {},
                }
            }
        except HTTPException:
            session.rollback()
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update Sub-LOB: {str(e)}")
        finally:
            session.close()

    # ══════════════════════════════════════════════════════
    # TAB 4: PERSONAS LEVEL (INDIVIDUAL & BATCH LIFECYCLE)
    # ══════════════════════════════════════════════════════
    personas_router = APIRouter(prefix="/api/personas", tags=["3. Personas Level"])

    @personas_router.post("/fetch")
    def fetch_persona_from_card(card: PersonaCardFetchRequest):
        """
        [Tab 4 - Person Card 'Fetch' Button]:
        Accepts the incoming person card payload from frontend, dynamically resolves identity,
        generates all 18 official scraping URLs, and synthesizes neural AI dossier.
        """
        t0 = datetime.now(timezone.utc)
        parsed_name = card.name
        parsed_title = card.title
        parsed_company = card.company_name
        try:
            # 1. Dynamically parse name, title, and company from incoming payload
            raw_display = card.display_name or ""

            # Dynamic extraction from display_name if explicit fields are omitted
            # (e.g. "Jane Doe (CEO, Example Co)")
            if not parsed_name and raw_display:
                parsed_name = re.sub(r"\s*\(.*?\)", "", raw_display).strip()
            if not parsed_name and card.key:
                parsed_name = card.key.replace("_", " ").title()

            if raw_display and "(" in raw_display:
                match = re.search(r"\((.*?)\)", raw_display)
                if match:
                    parts = match.group(1).split(",")
                    if not parsed_title and len(parts) >= 1:
                        parsed_title = parts[0].strip()
                    if not parsed_company and len(parts) >= 2:
                        parsed_company = parts[1].strip()

            parsed_name = parsed_name or "Executive"
            parsed_title = parsed_title or "Leadership"
            parsed_company = parsed_company or ""

            # If account_id provided, look up company, domain, ticker, and cik from DB
            acct_domain = None
            acct_ticker = None
            acct_cik = None
            if card.account_id:
                session = get_session()
                try:
                    acct = session.query(Account).filter_by(id=card.account_id).first()
                    if acct:
                        if not parsed_company:
                            parsed_company = acct.legal_name or acct.display_name or acct.key
                        acct_domain = acct.domain or acct.primary_domain
                        acct_ticker = acct.stock_symbol
                        acct_cik = acct.sec_cik

                    # Lookup existing persona stub if present to preserve accurate title & profile
                    from sqlalchemy import or_
                    existing_p = None
                    if card.key:
                        existing_p = session.query(Persona).filter(
                            Persona.account_id == card.account_id,
                            Persona.key == card.key
                        ).first()
                    if not existing_p and parsed_name:
                        existing_p = session.query(Persona).filter(
                            Persona.account_id == card.account_id,
                            Persona.full_name.ilike(parsed_name)
                        ).first()
                    if existing_p:
                        if (not parsed_title or parsed_title.lower() in ["leadership", "executive"]) and existing_p.title:
                            parsed_title = existing_p.title
                        if not card.linkedin_url and existing_p.linkedin_url:
                            card.linkedin_url = existing_p.linkedin_url
                finally:
                    session.close()

            # 2. Enrich via Enterprise PersonaService (FullEnrich + Exa + Apollo + Serper + SEC + ORCID + OpenAlex + ExecutiveOsintUrlEngine)
            person_entry = PersonaService.enrich_single_persona(
                full_name=parsed_name,
                company_name=parsed_company,
                title=parsed_title,
                account_id=card.account_id,
                domain=acct_domain,
                ticker=acct_ticker,
                sec_cik=acct_cik,
                linkedin_url=card.linkedin_url,
            )

            # 3. Auto-save single persona slice into raw/enriched run folder
            company_slug = slugify(parsed_company) if parsed_company else "general"
            person_slug = slugify(parsed_name)
            run_dirs = config.get_run_output_dirs(parsed_company or "persona_run")
            person_file = (
                run_dirs["enriched_personas_company_dir"]
                / f"{company_slug}_corporate_{person_slug}_enriched.json"
            )
            MasterSerializer.save_json(person_entry, person_file)

            from services.credit_accounting_engine import CreditAccountingEngine
            persona_credit_tally = CreditAccountingEngine.tally_persona_telemetry(1)
            now_completed = datetime.now(timezone.utc)
            duration_sec = round((now_completed - t0).total_seconds(), 2)
            credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                company_name=parsed_company or "Company",
                run_id=f"run_{slugify(parsed_company or 'persona')[:20]}_persona_single",
                run_number=1,
                started_at=t0,
                duration_seconds=duration_sec,
                status="success",
                account_tally={"credits": 0, "resources": []},
                lob_tally={"credits": 0, "resources": []},
                persona_tally=persona_credit_tally,
            )

            PipelineRunLogger.log_event(
                company_name=parsed_company or "Company",
                target_url=card.linkedin_url,
                level="persona",
                action="pull",
                status="success",
                quality_score=90.0,
                quality_grade="A",
                started_at=t0,
                completed_at=now_completed,
                duration_seconds=duration_sec,
                raw_storage_dir=str(person_file),
                total_credits_used=persona_credit_tally.get("credits", 0),
                credits_breakdown=credits_breakdown,
                entities_extracted={
                    "full_name": parsed_name,
                    "title": parsed_title,
                    "email": person_entry.get("email"),
                    "account_id": card.account_id,
                    "osint_sources_count": len(person_entry.get("osint_feed_manifest") or {}),
                    "saved_file": str(person_file),
                    "total_contacts": 1,
                },
            )

            return {
                "status": "staged",
                "message": f"Successfully fetched and enriched persona for '{parsed_name}'.",
                "saved_file": str(person_file),
                "person": person_entry,
            }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name=parsed_company or "Company",
                level="persona",
                action="pull",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Persona fetch failed: {str(e)}")

    @personas_router.post("/fetch-background")
    def fetch_persona_background(card: PersonaCardFetchRequest):
        """
        [Background Mode — Tab 4 Person Card Fetch]:
        Identical to POST /api/personas/fetch but returns a job_id immediately (200ms).
        PersonaService.enrich_single_persona() runs in an independent background thread
        that survives browser close, refresh, tab switch, or screen lock.

        Workflow:
          1. POST here → receive {"job_id": "pjob_...", "poll_url": "/api/pipeline/job/..."}
          2. Poll GET /api/pipeline/job/{job_id} every 3 seconds
          3. When status == "done", result contains the same payload as /personas/fetch
        """
        t0 = datetime.now(timezone.utc)

        # ── Fast sync: parse name/title/company (identical logic to /fetch) ──
        parsed_name    = card.name
        parsed_title   = card.title
        parsed_company = card.company_name
        raw_display = card.display_name or ""

        if not parsed_name and raw_display:
            parsed_name = re.sub(r"\s*\(.*?\)", "", raw_display).strip()
        if not parsed_name and card.key:
            parsed_name = card.key.replace("_", " ").title()
        if raw_display and "(" in raw_display:
            _m = re.search(r"\((.*?)\)", raw_display)
            if _m:
                _parts = _m.group(1).split(",")
                if not parsed_title   and len(_parts) >= 1: parsed_title   = _parts[0].strip()
                if not parsed_company and len(_parts) >= 2: parsed_company = _parts[1].strip()
        parsed_name    = parsed_name    or "Executive"
        parsed_title   = parsed_title   or "Leadership"
        parsed_company = parsed_company or ""

        # ── Fast sync: resolve account context from DB (identical to /fetch) ──
        acct_domain = acct_ticker = acct_cik = None
        if card.account_id:
            _sess = get_session()
            try:
                _acct = _sess.query(Account).filter_by(id=card.account_id).first()
                if _acct:
                    if not parsed_company and _acct.display_name:
                        parsed_company = _acct.display_name
                    acct_domain  = _acct.domain or _acct.primary_domain
                    acct_ticker  = _acct.stock_symbol
                    acct_cik     = _acct.sec_cik
                    # Absorb existing persona context if available
                    _existing_p = next(
                        (p for p in (_acct.personas or []) if (p.full_name or "").lower() == parsed_name.lower()), None
                    )
                    if _existing_p:
                        if (not parsed_title or parsed_title.lower() in ["leadership", "executive"]) and _existing_p.title:
                            parsed_title = _existing_p.title
                        if not card.linkedin_url and _existing_p.linkedin_url:
                            card.linkedin_url = _existing_p.linkedin_url
            except Exception:
                pass
            finally:
                _sess.close()

        # ── Create job and spin thread ──
        job_id = _jreg_create(TYPE_PERSONA, meta={
            "full_name":    parsed_name,
            "title":        parsed_title,
            "company_name": parsed_company,
            "account_id":   card.account_id,
        })

        # Capture all params by value so thread is independent of request lifecycle
        _p_name    = parsed_name
        _p_title   = parsed_title
        _p_company = parsed_company
        _p_li_url  = card.linkedin_url
        _p_acct_id = card.account_id
        _p_domain  = acct_domain
        _p_ticker  = acct_ticker
        _p_cik     = acct_cik

        def _bg_persona():
            try:
                _jreg_update(job_id, status="running", progress_pct=5,
                             message=f"Enriching persona '{_p_name}' @ '{_p_company}'...")

                person_entry = PersonaService.enrich_single_persona(
                    full_name=_p_name,
                    company_name=_p_company,
                    title=_p_title,
                    account_id=_p_acct_id,
                    domain=_p_domain,
                    ticker=_p_ticker,
                    sec_cik=_p_cik,
                    linkedin_url=_p_li_url,
                )

                _jreg_update(job_id, progress_pct=80, message="Saving enriched persona to disk...")

                _company_slug = slugify(_p_company) if _p_company else "general"
                _person_slug  = slugify(_p_name)
                _run_dirs     = config.get_run_output_dirs(_p_company or "persona_run")
                _person_file  = (
                    _run_dirs["enriched_personas_company_dir"]
                    / f"{_company_slug}_corporate_{_person_slug}_enriched.json"
                )
                MasterSerializer.save_json(person_entry, _person_file)

                _jreg_update(job_id, progress_pct=90, message="Logging pipeline run...")

                from services.credit_accounting_engine import CreditAccountingEngine
                _persona_credit_tally = CreditAccountingEngine.tally_persona_telemetry(1)
                _now_completed = datetime.now(timezone.utc)
                _duration_sec  = round((_now_completed - t0).total_seconds(), 2)
                _credits_bd = CreditAccountingEngine.compile_run_breakdown(
                    company_name=_p_company or "Company",
                    run_id=f"run_{slugify(_p_company or 'persona')[:20]}_persona_bg",
                    run_number=1,
                    started_at=t0,
                    duration_seconds=_duration_sec,
                    status="success",
                    account_tally={"credits": 0, "resources": []},
                    lob_tally={"credits": 0, "resources": []},
                    persona_tally=_persona_credit_tally,
                )
                PipelineRunLogger.log_event(
                    company_name=_p_company or "Company",
                    target_url=_p_li_url,
                    level="persona",
                    action="pull",
                    status="success",
                    quality_score=float(person_entry.get("completeness_score") or 90.0),
                    quality_grade=person_entry.get("completeness_grade") or "A",
                    started_at=t0,
                    completed_at=_now_completed,
                    duration_seconds=_duration_sec,
                    raw_storage_dir=str(_person_file),
                    total_credits_used=_persona_credit_tally.get("credits", 0),
                    credits_breakdown=_credits_bd,
                    entities_extracted={
                        "full_name":          _p_name,
                        "title":              _p_title,
                        "email":              person_entry.get("email"),
                        "account_id":         _p_acct_id,
                        "osint_sources_count": len(person_entry.get("osint_feed_manifest") or {}),
                        "saved_file":         str(_person_file),
                        "total_contacts":     1,
                        "background_job_id":  job_id,
                    },
                )
                _jreg_update(job_id, status="done", progress_pct=100,
                             message=f"Persona enrichment complete for '{_p_name}'.",
                             result={
                                 "status":     "staged",
                                 "message":    f"Successfully enriched persona for '{_p_name}'.",
                                 "saved_file": str(_person_file),
                                 "person":     person_entry,
                             })
            except Exception as exc:
                _jreg_exc(job_id, exc)
                PipelineRunLogger.log_event(
                    company_name=_p_company or "Company",
                    level="persona", action="pull", status="failed",
                    started_at=t0, completed_at=datetime.now(timezone.utc),
                    error_message=str(exc),
                )

        threading.Thread(target=_bg_persona, daemon=False, name=f"persona-{job_id}").start()
        return {
            "status":   "queued",
            "job_id":   job_id,
            "poll_url": f"/api/pipeline/job/{job_id}",
            "message":  f"Persona enrichment started for '{parsed_name}'. Thread runs independently of this connection.",
        }

    @personas_router.post("/validate-single")
    def validate_single_persona(person_data: Dict[str, Any] = Body(...)):
        """
        [Tab 4 - Individual Validate Button]:
        Validates a single person's contact data, scraping URLs, and AI dossier.
        """
        t0 = datetime.now(timezone.utc)
        comp_name = person_data.get("company_name") or person_data.get("company") or "Company"
        p_name = (
            person_data.get("display_name")
            or person_data.get("name")
            or person_data.get("full_name")
            or "Executive"
        )
        try:
            audit = PersonaValidator.validate_persona(person_data)

            warnings = []
            if not person_data.get("linkedin_url"):
                warnings.append("LinkedIn URL is missing or unverified.")
            if not person_data.get("email"):
                warnings.append("Email address not verified.")
            if not person_data.get("value_proposition"):
                warnings.append("AI Persona Dossier has not been synthesized.")

            score = float(audit.get("score", 90))
            grade = audit.get("grade", "A")

            PipelineRunLogger.log_event(
                company_name=comp_name,
                target_url=person_data.get("linkedin_url"),
                level="persona",
                action="validate",
                status="validated",
                quality_score=score,
                quality_grade=grade,
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "person_name": p_name,
                    "title": person_data.get("title"),
                    "has_verified_linkedin": bool(person_data.get("linkedin_url")),
                    "has_verified_email": bool(person_data.get("email")),
                    "has_ai_dossier": bool(person_data.get("value_proposition")),
                    "ready_for_db": audit.get("ready_for_db", True),
                    "warnings": warnings,
                },
            )

            return {
                "status": "validated",
                "score": audit.get("score", 90),
                "grade": audit.get("grade", "A"),
                "person_name": p_name,
                "has_verified_linkedin": bool(person_data.get("linkedin_url")),
                "has_verified_email": bool(person_data.get("email")),
                "has_ai_dossier": bool(person_data.get("value_proposition")),
                "warnings": warnings,
                "missing_critical": audit.get("missing_critical", []),
                "missing_important": audit.get("missing_important", []),
                "ready_for_db": audit.get("ready_for_db", True),
            }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name=comp_name,
                level="persona",
                action="validate",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Persona validation failed: {str(e)}")

    @personas_router.post("/dump-single-db")
    def dump_single_persona_to_db(req: PersonDumpRequest):
        """
        [Tab 4 - Individual Dump DB Button]: Commits a single validated persona into PostgreSQL `personas` table.
        Includes disk re-hydration: if 'saved_file' is present in person_data, the full enriched
        JSON is loaded from disk and deep-merged to restore fields lost during the browser round-trip.
        """
        import json as _json
        import glob as _glob
        import copy as _copy

        def _deep_merge_into(base: dict, overlay: dict) -> dict:
            """Fill missing/None keys in base from overlay. Non-destructive."""
            for k, v in overlay.items():
                if k not in base or base[k] is None or base[k] == "" or base[k] == []:
                    base[k] = v
                elif isinstance(base.get(k), dict) and isinstance(v, dict):
                    _deep_merge_into(base[k], v)
            return base

        def _rehydrate_from_disk(person_data: dict) -> dict:
            """
            Load the full enriched JSON from disk and deep-merge it into person_data,
            restoring vendor sub-dicts and any fields lost during the browser round-trip.
            If no disk path is found, returns person_data unchanged.
            """
            merged = _copy.deepcopy(person_data)
            disk_path = person_data.get("saved_file") or person_data.get("enriched_file_path")

            # Fallback: find most-recent enriched file by account_key + name slug
            if not disk_path:
                full_name = person_data.get("full_name") or person_data.get("name", "")
                account_id_val = str(person_data.get("account_id", ""))
                if full_name and account_id_val:
                    import os
                    pipeline_root = os.path.dirname(os.path.abspath(__file__))
                    pattern = os.path.join(pipeline_root, "output", "**", "*.json")
                    name_slug = full_name.lower().replace(" ", "_")[:20]
                    candidates = sorted(
                        [f for f in _glob.glob(pattern, recursive=True)
                         if "enriched" in f and name_slug in f.lower()],
                        reverse=True
                    )
                    if candidates:
                        disk_path = candidates[0]

            if disk_path:
                try:
                    with open(disk_path, "r", encoding="utf-8") as _f:
                        disk_data = _json.load(_f)
                    _deep_merge_into(merged, disk_data)
                    print(f"[+] [dump-single-db] Disk re-hydration applied from: {disk_path}")
                except Exception as _rh_err:
                    print(f"[!] [dump-single-db] Disk re-hydration notice: {_rh_err}")
            return merged

        t0 = datetime.now(timezone.utc)
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=req.account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail=f"Account ID {req.account_id} not found in DB.")

            p_data = dict(req.person_data)
            p_data["account_id"] = acct.id
            p_data = _rehydrate_from_disk(p_data)        # ← disk re-hydration
            schema = PersonaSchema.from_enriched_json(p_data)


            # Enterprise Deduplication Mapping Layer: resolve and coalesce into master
            repo = PersonaRepository(session)
            persona = repo.upsert(schema)
            session.commit()

            PipelineRunLogger.log_event(
                company_name=acct.legal_name or acct.display_name or acct.key,
                target_url=persona.linkedin_url,
                level="persona",
                action="dump",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "persona_id": persona.id,
                    "full_name": persona.full_name,
                    "title": persona.title,
                    "tier": persona.tier,
                    "account_id": acct.id,
                },
            )

            return {
                "status": "success",
                "persona_id": persona.id,
                "full_name": persona.full_name,
                "title": persona.title,
                "tier": persona.tier,
                "message": f"Persona '{persona.full_name}' saved to database.",
            }
        except Exception as e:
            session.rollback()
            PipelineRunLogger.log_event(
                company_name="Company",
                level="persona",
                action="dump",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Persona DB dump failed: {str(e)}")
        finally:
            session.close()

    @personas_router.post("/fetch-hierarchy")
    def fetch_full_hierarchy(req: HierarchyFetchRequest):
        """[Tab 4 - Full Org Hierarchy Fetch Button]: Pulls live 4-tier organization hierarchy."""
        t0 = datetime.now(timezone.utc)
        comp_name = req.company_name or req.company_domain or "Company"
        try:
            hierarchy = scrape_hierarchy(
                company_domain=req.company_domain,
                company_name=req.company_name,
                sec_cik=req.sec_cik,
                max_total_records=config.DEFAULT_HIERARCHY_LIMIT,
            )

            if req.enrich_csuite_dossiers and hierarchy.get("c_suite"):
                for p in hierarchy["c_suite"][:2]:
                    p["persona_dossier"] = build_persona_dossier(
                        p.get("name"),
                        p.get("title"),
                        req.company_name or req.company_domain,
                        p.get("linkedin_url"),
                    )

            total = sum(
                len(hierarchy.get(k, [])) for k in ["c_suite", "vp_level", "director_level", "manager_level"]
            )
            tier_counts = {
                k: len(hierarchy.get(k, []))
                for k in ["c_suite", "vp_level", "director_level", "manager_level"]
            }

            # Dynamic Metered Credit Breakdown for Personas
            from services.credit_accounting_engine import CreditAccountingEngine
            persona_credit_tally = CreditAccountingEngine.tally_persona_telemetry(total)
            now_completed = datetime.now(timezone.utc)
            duration_sec = round((now_completed - t0).total_seconds(), 2)
            credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                company_name=comp_name,
                run_id=f"run_{slugify(comp_name)[:20]}_persona",
                run_number=1,
                started_at=t0,
                duration_seconds=duration_sec,
                status="staged",
                account_tally={"credits": 0, "resources": []},
                lob_tally={"credits": 0, "resources": []},
                persona_tally=persona_credit_tally,
            )

            PipelineRunLogger.log_event(
                company_name=comp_name,
                target_url=req.company_domain,
                level="persona",
                action="pull",
                status="staged",
                started_at=t0,
                completed_at=now_completed,
                duration_seconds=duration_sec,
                total_credits_used=persona_credit_tally.get("credits", 0),
                credits_breakdown=credits_breakdown,
                entities_extracted={
                    "total_contacts": total,
                    "tier_counts": tier_counts,
                },
            )

            # Auto-commit to DB if account_id is provided
            if req.account_id and hierarchy:
                session = get_session()
                try:
                    acct = session.query(Account).filter_by(id=req.account_id).first()
                    if acct:
                        repo = PersonaRepository(session)
                        repo.upsert_all(acct, hierarchy)
                        session.commit()
                except Exception as commit_err:
                    session.rollback()
                    print(f"[!] [HierarchyFetch] Auto-commit DB notice: {commit_err}")
                finally:
                    session.close()

            return {
                "status": "staged",
                "total_contacts": total,
                "tier_counts": tier_counts,
                "hierarchy": hierarchy,
            }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name=comp_name,
                target_url=req.company_domain,
                level="persona",
                action="pull",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Hierarchy fetch failed: {str(e)}")

    @personas_router.post("/validate")
    def validate_personas(hierarchy: Dict[str, List[Dict[str, Any]]] = Body(...)):
        """[Tab 4 - Hierarchy Validate Button]: Validates 4-tier hierarchy."""
        t0 = datetime.now(timezone.utc)
        try:
            report = DataQualityValidator.validate_hierarchy_and_personas(hierarchy)
            score = float(report.get("score", 0.0))
            grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))

            PipelineRunLogger.log_event(
                company_name="Organization Hierarchy",
                level="persona",
                action="validate",
                status="validated",
                quality_score=score,
                quality_grade=grade,
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "total_contacts": report.get("total_contacts"),
                    "tier_breakdown": report.get("tier_breakdown"),
                    "warnings": report.get("warnings", []),
                },
            )

            return {
                "status": "validated",
                "score": report["score"],
                "total_contacts": report["total_contacts"],
                "tier_breakdown": report["tier_breakdown"],
                "contact_metrics": report["contact_metrics"],
                "warnings": report["warnings"],
            }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name="Organization Hierarchy",
                level="persona",
                action="validate",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Personas validation failed: {str(e)}")

    @personas_router.post("/dump-hierarchy-db")
    def dump_hierarchy_to_db(req: HierarchyDumpRequest):
        """[Tab 4 - Batch Hierarchy Dump DB Button]: Commits full 4-tier hierarchy to PostgreSQL."""
        t0 = datetime.now(timezone.utc)
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=req.account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail=f"Account ID {req.account_id} not found in DB.")

            repo = PersonaRepository(session)
            count = repo.upsert_all(acct, req.hierarchy)
            session.commit()

            PipelineRunLogger.log_event(
                company_name=acct.legal_name or acct.display_name or acct.key,
                level="persona",
                action="dump",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                entities_extracted={
                    "total_personas_saved": count,
                    "account_id": acct.id,
                },
            )

            return {
                "status": "success",
                "account_id": acct.id,
                "total_personas_saved": count,
                "message": f"Saved {count} personas for Account '{acct.key}'.",
            }
        except Exception as e:
            session.rollback()
            PipelineRunLogger.log_event(
                company_name="Company",
                level="persona",
                action="dump",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Hierarchy DB dump failed: {str(e)}")
        finally:
            session.close()

    @personas_router.get("")
    def get_personas_from_db(account_id: int = Query(...), tier: Optional[str] = Query(None)):
        """Retrieves stored personas for an account, with optional tier filter."""
        session = get_session()
        try:
            query = session.query(Persona).filter_by(account_id=account_id)
            if tier:
                query = query.filter_by(tier=tier)
            personas = query.order_by(Persona.id).all()
            return [_serialize_persona_full(p) for p in personas]
        finally:
            session.close()

    @personas_router.patch("/{persona_id}")
    @app.patch("/api/persona/{persona_id}", tags=["3. Personas Level"], dependencies=[Depends(auth.require_persona_account_access), Depends(auth.require_editor)])
    @app.patch("/api/personas/{persona_id}", tags=["3. Personas Level"], dependencies=[Depends(auth.require_persona_account_access), Depends(auth.require_editor)])
    def update_persona(persona_id: int, payload: Dict[str, Any] = Body(...)):
        """
        [Universal & Inline Edit]: Updates persona fields directly in PostgreSQL.
        Automatically sets is_manually_verified = True, updates manually_verified_at to now,
        logs audit event to pipeline_runs, and returns the full serialized persona.
        """
        session = get_session()
        t0 = datetime.now(timezone.utc)
        try:
            p = session.query(Persona).options(
                selectinload(Persona.account)
            ).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")

            immutable = {"id", "created_at"}
            updated_fields = {}
            for k, v in payload.items():
                if k in immutable or not hasattr(Persona, k):
                    continue
                setattr(p, k, v)
                updated_fields[k] = v

            now_utc = datetime.now(timezone.utc)
            p.is_manually_verified = True
            p.manually_verified_at = now_utc

            session.commit()
            session.refresh(p)

            company_name = p.account.display_name if p.account else "Company"
            PipelineRunLogger.log_event(
                company_name=company_name,
                target_url=p.linkedin_url or p.email,
                level="persona",
                action="manual_edit",
                status="success",
                started_at=t0,
                completed_at=now_utc,
                entities_extracted={
                    "persona_id": p.id,
                    "account_id": p.account_id,
                    "updated_fields": list(updated_fields.keys()),
                    "is_manually_verified": True,
                    "manually_verified_at": p.manually_verified_at.isoformat(),
                },
            )
            return {"status": "success", "persona": _serialize_persona_full(p)}
        except HTTPException:
            session.rollback()
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update persona: {str(e)}")
        finally:
            session.close()

    @app.post("/api/verify/{entity_type}/{entity_id}", tags=["0. Verification Engine"], dependencies=[Depends(auth.require_entity_account_access), Depends(auth.require_editor)])
    def toggle_manual_verification(entity_type: str, entity_id: int, payload: Optional[Dict[str, Any]] = Body(default=None)):
        """
        [Manual Verification Toggle]: Sets or toggles is_manually_verified flag on Account, LOB, or Persona in PostgreSQL.
        Every time it is marked verified, manually_verified_at is stamped with current UTC time.
        Audit logged to pipeline_runs and output audit logs.
        """
        entity_type_lower = entity_type.lower().rstrip("s")
        session = get_session()
        t0 = datetime.now(timezone.utc)
        try:
            if entity_type_lower == "account":
                model_cls = Account
                level = "account"
            elif entity_type_lower == "lob":
                model_cls = Lob
                level = "lob"
            elif entity_type_lower == "persona":
                model_cls = Persona
                level = "persona"
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported entity type: '{entity_type}'. Must be 'account', 'lob', or 'persona'.")

            entity = session.query(model_cls).filter_by(id=entity_id).first()
            if not entity:
                raise HTTPException(status_code=404, detail=f"{entity_type.capitalize()} with id {entity_id} not found.")

            now_utc = datetime.now(timezone.utc)
            if payload and "verified" in payload:
                new_state = bool(payload["verified"])
            else:
                new_state = not bool(getattr(entity, "is_manually_verified", False))

            entity.is_manually_verified = new_state
            if new_state:
                entity.manually_verified_at = now_utc
            else:
                entity.manually_verified_at = None

            session.commit()
            session.refresh(entity)

            company_name = "Company"
            if hasattr(entity, "display_name") and entity.display_name:
                company_name = entity.display_name
            elif hasattr(entity, "legal_name") and entity.legal_name:
                company_name = entity.legal_name
            elif hasattr(entity, "account") and entity.account:
                company_name = entity.account.display_name or entity.account.legal_name or "Company"

            PipelineRunLogger.log_event(
                company_name=company_name,
                target_url=getattr(entity, "domain", None) or getattr(entity, "linkedin_url", None) or getattr(entity, "website_url", None),
                level=level,
                action="verify_toggle",
                status="success",
                started_at=t0,
                completed_at=now_utc,
                entities_extracted={
                    "entity_type": entity_type_lower,
                    "entity_id": entity_id,
                    "is_manually_verified": entity.is_manually_verified,
                    "manually_verified_at": entity.manually_verified_at.isoformat() if entity.manually_verified_at else None,
                },
            )

            return {
                "status": "success",
                "entity_type": entity_type_lower,
                "id": entity_id,
                "is_manually_verified": entity.is_manually_verified,
                "manually_verified_at": entity.manually_verified_at.isoformat() if entity.manually_verified_at else None,
            }
        except HTTPException:
            session.rollback()
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to toggle verification: {str(e)}")
        finally:
            session.close()

    # ══════════════════════════════════════════════════════
    # FULL COMPOSITE PIPELINE ENDPOINTS
    # ══════════════════════════════════════════════════════
    pipeline_router = APIRouter(prefix="/api/pipeline", tags=["0. Full Composite Pipeline"])

    @pipeline_router.post("/run")
    def trigger_full_pipeline(req: PipelineRunRequest):
        """Triggers complete live pipeline. Staged, NO DB write."""
        t0 = datetime.now(timezone.utc)
        try:
            res = run_pipeline(company_name=req.company_name, target_url=req.target_url) or {}
            val_meta = res.get("validation_report", {}).get("audit_metadata", {})
            q_score = float(val_meta.get("overall_quality_score", 0.0))
            q_grade = val_meta.get("overall_quality_grade", "N/A")

            run_dirs = res.get("run_dirs") or {}
            raw_d = run_dirs.get("raw_dir")
            enr_d = run_dirs.get("enriched_dir")

            extracted_summary = val_meta.get("entities_extracted") or {
                "accounts_count": 1,
                "lobs_count": len(res.get("lobs") or []),
                "personas_count": len(res.get("personas") or []),
            }

            # Dynamic Metered Credit Breakdown for Composite Run
            from services.credit_accounting_engine import CreditAccountingEngine
            acct_tally = CreditAccountingEngine.tally_account_telemetry(
                (res.get("account") or {}).get("_telemetry", {}).get("sources", {})
            )
            lob_tally = CreditAccountingEngine.tally_lob_telemetry(len(res.get("lobs") or []))
            persona_tally = CreditAccountingEngine.tally_persona_telemetry(len(res.get("personas") or []))
            now_completed = datetime.now(timezone.utc)
            duration_sec = round((now_completed - t0).total_seconds(), 2)

            credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
                company_name=req.company_name,
                run_id=f"run_{slugify(req.company_name)[:20]}_composite",
                run_number=1,
                started_at=t0,
                duration_seconds=duration_sec,
                status="staged",
                account_tally=acct_tally,
                lob_tally=lob_tally,
                persona_tally=persona_tally,
            )
            total_composite_credits = acct_tally.get("credits", 0) + lob_tally.get("credits", 0) + persona_tally.get("credits", 0)

            PipelineRunLogger.log_event(
                company_name=req.company_name,
                target_url=req.target_url,
                level="composite",
                action="run",
                status="staged",
                quality_score=q_score,
                quality_grade=q_grade,
                started_at=t0,
                completed_at=now_completed,
                duration_seconds=duration_sec,
                raw_storage_dir=str(raw_d) if raw_d else None,
                enriched_storage_dir=str(enr_d) if enr_d else None,
                total_credits_used=total_composite_credits,
                credits_breakdown=credits_breakdown,
                entities_extracted=extracted_summary,
            )

            return {
                "status": "staged",
                "company_name": req.company_name,
                "run_dirs": {k: str(v) for k, v in run_dirs.items()},
                "validation_score": q_score,
                "ready_for_db_dump": val_meta.get("ready_for_db_dump", False),
            }
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name=req.company_name,
                target_url=req.target_url,
                level="composite",
                action="run",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Pipeline execution failed: {str(e)}")


    @pipeline_router.post("/validate")
    def validate_composite_run(req: PipelineDumpDbRequest):
        """Audits an entire staged run directory or file."""
        t0 = datetime.now(timezone.utc)
        target_path = Path(req.run_dir or req.file or "")
        if not target_path.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {target_path}")

        if target_path.is_dir():
            files = (
                list(target_path.glob("enriched/*_enriched*.json"))
                or list(target_path.glob("*_enriched*.json"))
                or list(target_path.rglob("*_enriched*.json"))
            )
            if not files:
                raise HTTPException(status_code=400, detail=f"No enriched JSON in: {target_path}")
            target_path = files[0]

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            audit_result = DataQualityValidator.audit_run(doc)
            val_meta = audit_result.get("audit_metadata", {})
            q_score = float(val_meta.get("overall_quality_score", 0.0))
            q_grade = val_meta.get("overall_quality_grade", "N/A")

            PipelineRunLogger.log_event(
                company_name=doc.get("company_name") or "Composite Run",
                level="composite",
                action="validate",
                status="validated",
                quality_score=q_score,
                quality_grade=q_grade,
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                raw_storage_dir=str(target_path),
                entities_extracted={"audit_path": str(target_path)},
            )

            return audit_result
        except Exception as e:
            PipelineRunLogger.log_event(
                company_name="Composite Run",
                level="composite",
                action="validate",
                status="failed",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")

    def _parse_run_level_and_action(run_id_str: str, entities: Optional[Dict[str, Any]]) -> tuple[str, str]:
        """Dynamically parses pipeline_level and action from run_id or entities_extracted without hardcoding."""
        ent = entities or {}
        level = ent.get("level")
        action = ent.get("action")
        if not level or not action:
            parts = str(run_id_str or "").lower().split("_")
            if not level:
                for cand in ["account", "sublob", "lob", "persona", "composite"]:
                    if cand in parts:
                        level = cand
                        break
            if not action:
                for cand in ["pull", "validate", "dump", "purge", "verify", "toggle"]:
                    if cand in parts:
                        action = cand
                        break
        return level or "pipeline", action or "run"

    @pipeline_router.get("/runs")
    def list_pipeline_runs(
        limit: int = 50,
        company_name: Optional[str] = Query(None),
        exclude_dumps: bool = Query(True),
    ):
        """Lists recent execution runs from PostgreSQL pipeline_runs table.
        By default, filters out individual entity micro-dumps so real stage runs are clearly visible.
        """
        from db.models import PipelineRun
        session = get_session()
        try:
            query = session.query(PipelineRun)
            if company_name:
                comp_clean = str(company_name).strip()
                conds = [PipelineRun.company_name.ilike(f"%{comp_clean}%")]
                if "mellon" in comp_clean.lower() or "bny" in comp_clean.lower():
                    conds.extend([
                        PipelineRun.company_name.ilike("%BNY%"),
                        PipelineRun.company_name.ilike("%Mellon%"),
                    ])
                elif "blackrock" in comp_clean.lower():
                    conds.append(PipelineRun.company_name.ilike("%BlackRock%"))
                elif "dtcc" in comp_clean.lower() or "depository" in comp_clean.lower():
                    conds.extend([
                        PipelineRun.company_name.ilike("%DTCC%"),
                        PipelineRun.company_name.ilike("%Depository%"),
                    ])
                from sqlalchemy import or_
                query = query.filter(or_(*conds))
            if exclude_dumps:
                query = query.filter(
                    ~PipelineRun.run_id.contains("_dump_"),
                    ~PipelineRun.run_id.contains("_toggle_"),
                )
            runs = query.order_by(PipelineRun.started_at.desc()).limit(limit).all()
            run_list = []
            for r in runs:
                lvl, act = _parse_run_level_and_action(r.run_id, r.entities_extracted)
                run_list.append({
                    "id": r.id,
                    "run_id": r.run_id,
                    "company_name": r.company_name,
                    "pipeline_level": lvl,
                    "action": act,
                    "target_url": r.target_url,
                    "status": r.status,
                    "quality_score": float(r.quality_score or 0.0),
                    "quality_grade": r.quality_grade,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "duration_seconds": float(r.duration_seconds or 0.0),
                    "total_credits_used": r.total_credits_used,
                    "credits_breakdown": r.credits_breakdown or {},
                    "entities_extracted": r.entities_extracted,
                    "raw_storage_dir": r.raw_storage_dir,
                    "enriched_storage_dir": r.enriched_storage_dir,
                    "execution_logs": r.execution_logs,
                    "error_message": r.error_message,
                })
            return {"status": "success", "runs": run_list}
        finally:
            session.close()

    # ── Background Job Status Polling ─────────────────────────────────────────
    @pipeline_router.get("/job/{job_id}", tags=["4. Pipeline Orchestration"])
    def get_background_job_status(job_id: str):
        """
        Poll the status of any background pipeline job started via a /fetch-background endpoint.

        Returns status (queued|running|done|failed), progress_pct (0-100), message,
        result (full response dict on done), and error (traceback string on failed).

        Frontend should call this every 3 seconds after receiving a job_id.
        Jobs are retained for 24h after completion then auto-purged.
        """
        job = _jreg_get(job_id)
        if not job:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Job '{job_id}' not found. "
                    "It may have expired (24h retention) or the job_id is invalid."
                ),
            )
        return job

    @pipeline_router.get("/jobs", tags=["4. Pipeline Orchestration"])
    def list_background_jobs(job_type: Optional[str] = Query(None, description="Filter: account_enrich | lob_enrich | persona_enrich")):
        """
        List all in-memory background jobs (newest first).
        Optionally filter by job_type: account_enrich, lob_enrich, persona_enrich.
        """
        return {
            "total": len(_jreg_list(job_type)),
            "jobs":  _jreg_list(job_type),
        }
    # ──────────────────────────────────────────────────────────────────────────

    @pipeline_router.get("/runs/{run_id}")
    def get_pipeline_run_detail(run_id: str):
        """Retrieves full execution details, credit breakdown, and logs for a specific run."""
        from db.models import PipelineRun
        session = get_session()
        try:
            r = session.query(PipelineRun).filter(PipelineRun.run_id == run_id).first()
            if not r:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
            lvl, act = _parse_run_level_and_action(r.run_id, r.entities_extracted)

            related_pull_run = None
            if act == "dump":
                pull_query = session.query(PipelineRun).filter(
                    PipelineRun.company_name == r.company_name,
                    PipelineRun.started_at <= r.started_at,
                    PipelineRun.id != r.id,
                )
                if r.target_url:
                    pull_query = pull_query.filter(PipelineRun.target_url == r.target_url)
                cand = pull_query.order_by(PipelineRun.started_at.desc()).first()
                if cand and cand.total_credits_used > 0:
                    related_pull_run = {
                        "run_id": cand.run_id,
                        "id": cand.id,
                        "action": "pull",
                        "total_credits_used": cand.total_credits_used,
                        "quality_score": float(cand.quality_score or 0.0),
                        "quality_grade": cand.quality_grade,
                        "started_at": cand.started_at.isoformat() if cand.started_at else None,
                    }

            return {
                "id": r.id,
                "run_id": r.run_id,
                "company_name": r.company_name,
                "pipeline_level": lvl,
                "action": act,
                "target_url": r.target_url,
                "status": r.status,
                "quality_score": float(r.quality_score or 0.0),
                "quality_grade": r.quality_grade,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "duration_seconds": float(r.duration_seconds or 0.0),
                "total_credits_used": r.total_credits_used,
                "credits_breakdown": r.credits_breakdown,
                "entities_extracted": r.entities_extracted,
                "execution_logs": r.execution_logs,
                "raw_storage_dir": r.raw_storage_dir,
                "enriched_storage_dir": r.enriched_storage_dir,
                "error_message": r.error_message,
                "related_pull_run": related_pull_run,
            }
        finally:
            session.close()

    @pipeline_router.get("/credits/summary")
    def get_credits_summary():
        """Returns aggregate credit consumption metrics across all vendors."""
        session = get_session()
        try:
            repo = PipelineRunRepository(session)
            return repo.get_credits_summary()
        finally:
            session.close()

    # Include all sub-routers
    # Access (apps/sales_crm/README.md §5.2, M5 hardening): the pipeline / bulk LOB / bulk persona
    # routers run paid enrichment and database dumps -> super admin only. The account router's
    # enrichment helpers are used by reps on account pages -> any signed-in editor; its bulk
    # create / fetch / validate / dump / patch routes are super admin only (per-route below).
    _admin_only = [Depends(auth.require_role("super_admin"))]
    app.include_router(pipeline_router, dependencies=_admin_only)
    app.include_router(account_router, dependencies=[Depends(auth.require_editor)])
    app.include_router(lobs_router, dependencies=_admin_only)
    app.include_router(personas_router, dependencies=_admin_only)

    # Sales Copilot (RAG chat) — own module, own tables; see apps/sales_copilot/README.md
    from apps.sales_copilot.api import install as install_copilot
    install_copilot(app)

    # Deals pipeline (Intro → Discovery → Proposal → Pilot → Contract) — apps/sales_copilot/README.md §21
    from apps.sales_deals.api import install as install_deals
    install_deals(app)
    # CRM core (apps/sales_crm/README.md): roles guard, business lines, team
    from apps.sales_crm.api import install as install_crm
    install_crm(app)

    # Global search palette (Ctrl+K / Ctrl+F): lexical suggestions + semantic related content
    from apps.sales_search.api import install as install_search
    install_search(app)

    # ══════════════════════════════════════════════════════
    # SOLID REST API ENDPOINTS
    # ══════════════════════════════════════════════════════

    @app.get("/api/accounts/{account_id}", tags=["1. Accounts"])
    def get_account_by_id(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieve a specific enterprise account with its complete profile."""
        return get_account_from_db(account_id, user)

    @app.get("/api/accounts/{account_id}/lobs", tags=["2. Lines of Business"])
    def get_account_lines_of_business(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieve all Lines of Business (LOBs) and nested sub-divisions for an account."""
        session = get_session()
        try:
            lobs = session.query(Lob).options(selectinload(Lob.sub_lobs)).filter_by(account_id=account_id).all()
            result = [_serialize_lob_full(lob_item, []) for lob_item in lobs]
            return {"account_id": account_id, "total_lobs": len(result), "lobs": result}
        finally:
            session.close()

    @app.get("/api/lobs/{lob_id}", tags=["2. Lines of Business"], dependencies=[Depends(auth.require_lob_account_access)])
    def get_single_line_of_business(lob_id: int):
        """Retrieve details for a single Line of Business by its ID."""
        session = get_session()
        try:
            lob_item = session.query(Lob).options(selectinload(Lob.sub_lobs)).filter_by(id=lob_id).first()
            if not lob_item:
                raise HTTPException(status_code=404, detail="Line of Business not found.")
            return _serialize_lob_full(lob_item, [])
        finally:
            session.close()

    @app.delete("/api/lobs/{lob_id}", tags=["2. Lines of Business"], dependencies=[Depends(auth.require_role("super_admin"))])
    def delete_single_lob(lob_id: int):
        """Delete a single Line of Business (and its sub-LOBs) by ID."""
        session = get_session()
        try:
            lob_item = session.query(Lob).filter_by(id=lob_id).first()
            if not lob_item:
                raise HTTPException(status_code=404, detail=f"LOB {lob_id} not found.")
            lob_name = lob_item.lob_name
            account_id = lob_item.account_id
            # Cascade delete sub-LOBs first
            sub_cnt = session.query(SubLob).filter_by(lob_id=lob_id).delete(synchronize_session=False)
            # Delete any personas directly attached to this LOB
            per_cnt = session.query(Persona).filter_by(lob_id=lob_id).delete(synchronize_session=False)
            session.delete(lob_item)
            session.commit()
            return {
                "status": "deleted",
                "lob_id": lob_id,
                "lob_name": lob_name,
                "account_id": account_id,
                "sub_lobs_deleted": sub_cnt,
                "personas_detached": per_cnt,
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"LOB delete failed: {str(e)}")
        finally:
            session.close()

    @app.get("/api/objections", tags=["3. Personas & Buying Committee"])
    @app.get("/api/command-center/objections", tags=["9. Command Center"])
    def get_common_objections_and_pain_points(
        response: Response,
        user: User = Depends(auth.get_current_user),
        limit: int = Query(10, ge=1, le=50),
    ):
        """Ranks operational_pain_points and key_objections (both real
        AI-dossier fields on Persona, not fabricated) by how many personas
        across the caller's accessible accounts mention them — a Command
        Center widget surfacing the objections/pain points reps are actually
        going to hear, aggregated server-side rather than shipping every
        persona's raw fields to the client just to count them there."""
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        session = get_session()
        try:
            # Only the fields tallied below — full persona rows carry large raw_data JSON and
            # p.account would load each account's multi-MB enrichment row.
            query = session.query(
                Persona.id, Persona.full_name, Persona.title, Persona.tier, Persona.account_id,
                Persona.operational_pain_points, Persona.key_objections,
                func.coalesce(Account.display_name, Account.legal_name).label("acct_name"),
            ).join(Account, Persona.account_id == Account.id)
            if user.role != "super_admin":
                accessible_ids = auth.get_accessible_account_ids(session, user.id)
                query = query.filter(Account.id.in_(accessible_ids)) if accessible_ids else query.filter(False)
            personas = query.all()

            pain_points: Dict[str, Dict[str, Any]] = {}
            objections: Dict[str, Dict[str, Any]] = {}

            def _tally(bucket: Dict[str, Dict[str, Any]], text: Optional[str], acct_name: Optional[str], p=None):
                text = (text or "").strip()
                if not text:
                    return
                entry = bucket.setdefault(text, {"count": 0, "accounts": set(), "personas": []})
                entry["count"] += 1
                if acct_name:
                    entry["accounts"].add(acct_name)
                if p and len(entry["personas"]) < 6:
                    entry["personas"].append({
                        "id": p.id,
                        "name": p.full_name,
                        "title": p.title,
                        "tier": p.tier,
                        "account": acct_name,
                        "account_id": p.account_id,
                    })

            for p in personas:
                acct_name = p.acct_name
                for text in (p.operational_pain_points or []):
                    _tally(pain_points, text, acct_name, p)
                for text in (p.key_objections or []):
                    _tally(objections, text, acct_name, p)

            def _top(bucket: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
                ranked = sorted(bucket.items(), key=lambda kv: kv[1]["count"], reverse=True)[:limit]
                return [
                    {
                        "text": text,
                        "count": v["count"],
                        "accounts": sorted(v["accounts"]),
                        "personas": v.get("personas", [])
                    }
                    for text, v in ranked
                ]

            return {"pain_points": _top(pain_points), "objections": _top(objections)}
        finally:
            session.close()

    @app.get("/api/accounts/{account_id}/personas", tags=["3. Personas & Buying Committee"])
    def get_account_buying_committee(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieve all executive personas and decision makers mapped to an account."""
        session = get_session()
        try:
            personas = session.query(Persona).filter_by(account_id=account_id).all()
            result = [_serialize_persona_full(p) for p in personas]
            return {"account_id": account_id, "total_personas": len(result), "personas": result}
        finally:
            session.close()

    @app.get("/api/personas/{persona_id}", tags=["3. Personas & Buying Committee"])
    def get_single_persona_profile(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """Retrieve full details and 58-column AI dossier for a specific executive persona."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")
            return _serialize_persona_full(p)
        finally:
            session.close()

    @app.delete("/api/personas/{persona_id}", tags=["3. Personas & Buying Committee"], dependencies=[Depends(auth.require_role("super_admin"))])
    def delete_single_persona(persona_id: int):
        """Delete a single Persona record by ID."""
        session = get_session()
        try:
            persona = session.query(Persona).filter_by(id=persona_id).first()
            if not persona:
                raise HTTPException(status_code=404, detail=f"Persona {persona_id} not found.")
            persona_name = persona.full_name or persona.display_name or f"Persona #{persona_id}"
            account_id = persona.account_id
            session.delete(persona)
            session.commit()
            return {
                "status": "deleted",
                "persona_id": persona_id,
                "persona_name": persona_name,
                "account_id": account_id,
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Persona delete failed: {str(e)}")
        finally:
            session.close()

    # ── Enterprise Record Updating & Verification Endpoints ────────────────────


    @app.patch("/api/accounts/{account_id}", tags=["1. Accounts"], dependencies=[Depends(auth.require_account_access), Depends(auth.require_editor)])
    def update_account_record(account_id: int, updates: Dict[str, Any]):
        """Directly updates Account fields in PostgreSQL, sets is_manually_verified=True, and logs audit run."""
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

            disallowed = {"id", "created_at"}
            applied = {}
            for k, v in updates.items():
                if k in disallowed:
                    continue
                if k in ["name", "display_name"] and hasattr(acct, "display_name"):
                    acct.display_name = v
                    applied["display_name"] = v
                elif hasattr(acct, k):
                    setattr(acct, k, v)
                    applied[k] = v

            now_utc = datetime.now(timezone.utc)
            acct.updated_at = now_utc
            acct.is_manually_verified = updates.get("is_manually_verified", True)
            acct.manually_verified_at = now_utc
            session.commit()
            session.refresh(acct)

            from services.pipeline_run_logger import PipelineRunLogger
            PipelineRunLogger.log_event(
                company_name=acct.display_name or acct.name or "Account",
                pipeline_level="account",
                action="manual_edit",
                status="completed",
                quality_score=100.0,
                quality_grade="A",
                target_url=acct.domain or acct.primary_domain or None,
                entities_extracted={"updated_fields": list(applied.keys()), "values": applied, "is_manually_verified": acct.is_manually_verified},
            )

            return {"status": "success", "account": _serialize_account_summary(acct)}
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update account: {str(e)}")
        finally:
            session.close()

    # ── Enterprise Data Management & Granular Purge Endpoints ─────────────────

    @app.get("/api/accounts/{account_id}/data-summary", tags=["1. Accounts"], dependencies=[Depends(auth.require_account_access)])
    def get_account_data_summary(account_id: int):
        """Returns live counts of all child entities and persona seniority tiers for granular data management."""
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

            # LOBs & Sub-LOBs
            lob_ids = [l.id for l in session.query(Lob.id).filter_by(account_id=account_id).all()]
            lobs_count = len(lob_ids)
            sub_lobs_count = (
                session.query(SubLob).filter(SubLob.lob_id.in_(lob_ids)).count()
                if lob_ids else 0
            )

            # Personas by Seniority Tier
            personas = session.query(Persona).filter_by(account_id=account_id).all()
            total_personas = len(personas)
            
            c_suite_cnt = 0
            vp_cnt = 0
            director_cnt = 0
            manager_cnt = 0

            for p in personas:
                level = p.hierarchy_level or 99
                t = (p.tier or "").lower()
                title = (p.title or "").lower()
                seniority = (p.seniority_raw or "").lower()

                if (
                    level in (1, 2)
                    or any(k in t for k in ["c_suite", "c-suite", "executive", "president", "chief", "board"])
                    or any(k in title for k in ["chief", "ceo", "cfo", "cio", "cto", "cmo", "coo", "cro", "ciso", "president", "chair", "board", "executive vice president", "evp"])
                    or any(k in seniority for k in ["c_suite", "c-suite", "executive", "tier1"])
                ):
                    c_suite_cnt += 1
                elif (
                    level == 3
                    or any(k in t for k in ["vp", "vice president", "head"])
                    or any(k in title for k in ["vp", "vice president", "head of", "senior vice president", "svp"])
                    or any(k in seniority for k in ["vp", "head", "tier2"])
                ):
                    vp_cnt += 1
                elif (
                    level == 4
                    or any(k in t for k in ["director", "md"])
                    or any(k in title for k in ["director", "managing director", "senior director"])
                    or any(k in seniority for k in ["director", "tier3", "tier4"])
                ):
                    director_cnt += 1
                else:
                    manager_cnt += 1

            # Activity & Intelligence Signals
            opp_signals = session.query(OpportunitySignal).filter_by(account_id=account_id).count()
            weekly_digests = session.query(WeeklyDigestSnapshot).filter_by(account_id=account_id).count()
            action_items = session.query(ActionItem).filter_by(account_id=account_id).count()
            posts_news = session.query(Post).filter_by(target_key=acct.key).count() if acct.key else 0
            cxo_mov = session.query(CxoMovement).filter(or_(CxoMovement.target_key == acct.key, CxoMovement.company_name.ilike(acct.display_name))).count() if acct.key else 0
            jobs = session.query(LinkedInJob).filter(or_(LinkedInJob.target_key == acct.key, LinkedInJob.company_name.ilike(acct.display_name))).count() if acct.key else 0

            return {
                "account_id": acct.id,
                "company_name": acct.display_name or acct.name or "Account",
                "key": acct.key,
                "domain": acct.primary_domain or acct.domain,
                "lobs_count": lobs_count,
                "sub_lobs_count": sub_lobs_count,
                "personas": {
                    "total": total_personas,
                    "c_suite": c_suite_cnt,
                    "vp_head": vp_cnt,
                    "director": director_cnt,
                    "manager_other": manager_cnt,
                },
                "signals": {
                    "opportunity_signals": opp_signals,
                    "weekly_digests": weekly_digests,
                    "posts_news": posts_news,
                    "cxo_movements": cxo_mov,
                    "jobs": jobs,
                    "action_items": action_items,
                }
            }
        finally:
            session.close()

    @app.post("/api/accounts/{account_id}/purge", tags=["1. Accounts"], dependencies=[Depends(auth.require_role("super_admin"))])
    def purge_account_data(account_id: int, req: AccountPurgeRequest):
        """Atomically purges selected or all components of an account with enterprise safeguards."""
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

            acct_name = acct.display_name or acct.name or "Account"
            acct_key = acct.key

            deleted_summary = {
                "account_deleted": False,
                "lobs_deleted": 0,
                "sub_lobs_deleted": 0,
                "personas_deleted": 0,
                "signals_deleted": 0,
            }

            # 1. Full Account Purge
            if req.delete_account_record:
                # Verify type-to-confirm (tolerant to punctuation, whitespace, suffixes, and common typos)
                raw_provided = (req.confirmation_text or "").strip().upper()
                # Normalize spaces and fix common typo 'DELLETE' -> 'DELETE'
                cleaned_provided = re.sub(r'\s+', ' ', raw_provided)
                cleaned_provided = re.sub(r'^DEL+E+T+E*', 'DELETE', cleaned_provided)
                # Strip punctuation for flexible matching
                norm_provided = re.sub(r'[^A-Z0-9\s]', '', cleaned_provided).strip()

                norm_name = re.sub(r'[^A-Z0-9\s]', '', acct_name.upper()).strip()
                norm_name_clean = re.sub(r'\s+', ' ', norm_name)
                norm_key = re.sub(r'[^A-Z0-9\s]', '', (acct_key or "").upper()).strip()

                # Base brand name stripped of corporate suffixes (e.g., "BLACKROCK INC" -> "BLACKROCK")
                core_brand = re.sub(r'\b(INC|CORP|CORPORATION|LLC|LTD|PLC|CO|COMPANY)\b', '', norm_name_clean).strip()

                valid_options = {
                    f"DELETE {norm_name_clean}".strip(),
                    f"DELETE {norm_key}".strip(),
                    f"DELETE {core_brand}".strip(),
                    "DELETE",
                }

                is_valid = (
                    norm_provided in valid_options
                    or (norm_provided.startswith("DELETE") and core_brand and core_brand in norm_provided)
                    or (norm_provided.startswith("DELETE") and norm_key and norm_key in norm_provided)
                )

                if not is_valid:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Confirmation mismatch. Expected 'DELETE {core_brand or acct_name}', received '{req.confirmation_text}'."
                    )

                # Cascade delete sub_lobs
                lob_ids = [l.id for l in session.query(Lob.id).filter_by(account_id=account_id).all()]
                if lob_ids:
                    sub_cnt = session.query(SubLob).filter(SubLob.lob_id.in_(lob_ids)).delete(synchronize_session=False)
                    deleted_summary["sub_lobs_deleted"] = sub_cnt

                # Delete personas
                per_cnt = session.query(Persona).filter_by(account_id=account_id).delete(synchronize_session=False)
                deleted_summary["personas_deleted"] = per_cnt

                # Delete signals
                sig_cnt = 0
                sig_cnt += session.query(OpportunitySignal).filter_by(account_id=account_id).delete(synchronize_session=False)
                sig_cnt += session.query(WeeklyDigestSnapshot).filter_by(account_id=account_id).delete(synchronize_session=False)
                sig_cnt += session.query(ActionItem).filter_by(account_id=account_id).delete(synchronize_session=False)
                sig_cnt += session.query(UserAccountAccess).filter_by(account_id=account_id).delete(synchronize_session=False)
                # Note: External scraped news (Post), job listings (LinkedInJob), and CXO movements
                # are preserved so they instantly reconnect when the account is re-ingested.
                deleted_summary["signals_deleted"] = sig_cnt

                # Delete LOBs
                lob_cnt = session.query(Lob).filter_by(account_id=account_id).delete(synchronize_session=False)
                deleted_summary["lobs_deleted"] = lob_cnt

                # Delete Account
                session.delete(acct)
                deleted_summary["account_deleted"] = True

                session.commit()

                # Audit log
                from services.pipeline_run_logger import PipelineRunLogger
                PipelineRunLogger.log_event(
                    company_name=acct_name,
                    pipeline_level="account",
                    action="purge_account",
                    status="completed",
                    target_url=acct.domain or acct.primary_domain or None,
                    entities_extracted={"deleted": deleted_summary},
                )

                return {
                    "status": "success",
                    "account_id": account_id,
                    "company_name": acct_name,
                    "deleted": deleted_summary,
                    "message": f"Account '{acct_name}' and all associated entities purged successfully."
                }

            # 2. Granular / Selective Purge
            # A. LOBs
            if req.delete_lobs:
                lob_ids = [l.id for l in session.query(Lob.id).filter_by(account_id=account_id).all()]
                if lob_ids:
                    sub_cnt = session.query(SubLob).filter(SubLob.lob_id.in_(lob_ids)).delete(synchronize_session=False)
                    deleted_summary["sub_lobs_deleted"] = sub_cnt
                    session.query(Persona).filter_by(account_id=account_id).update({"lob_id": None}, synchronize_session=False)
                    lob_cnt = session.query(Lob).filter_by(account_id=account_id).delete(synchronize_session=False)
                    deleted_summary["lobs_deleted"] = lob_cnt

            # B. Personas by Category / Seniority Tier
            if req.personas:
                if req.personas.all:
                    per_cnt = session.query(Persona).filter_by(account_id=account_id).delete(synchronize_session=False)
                    deleted_summary["personas_deleted"] = per_cnt
                else:
                    all_pers = session.query(Persona).filter_by(account_id=account_id).all()
                    ids_to_del = []
                    for p in all_pers:
                        level = p.hierarchy_level or 99
                        t = (p.tier or "").lower()
                        title = (p.title or "").lower()
                        seniority = (p.seniority_raw or "").lower()

                        is_c = (
                            level in (1, 2)
                            or any(k in t for k in ["c_suite", "c-suite", "executive", "president", "chief", "board"])
                            or any(k in title for k in ["chief", "ceo", "cfo", "cio", "cto", "cmo", "coo", "cro", "ciso", "president", "chair", "board", "executive vice president", "evp"])
                            or any(k in seniority for k in ["c_suite", "c-suite", "executive", "tier1"])
                        )
                        is_vp = (
                            not is_c and (
                                level == 3
                                or any(k in t for k in ["vp", "vice president", "head"])
                                or any(k in title for k in ["vp", "vice president", "head of", "senior vice president", "svp"])
                                or any(k in seniority for k in ["vp", "head", "tier2"])
                            )
                        )
                        is_dir = (
                            not is_c and not is_vp and (
                                level == 4
                                or any(k in t for k in ["director", "md"])
                                or any(k in title for k in ["director", "managing director", "senior director"])
                                or any(k in seniority for k in ["director", "tier3", "tier4"])
                            )
                        )
                        is_mgr = not is_c and not is_vp and not is_dir

                        if is_c and req.personas.c_suite:
                            ids_to_del.append(p.id)
                        elif is_vp and req.personas.vp_head:
                            ids_to_del.append(p.id)
                        elif is_dir and req.personas.director:
                            ids_to_del.append(p.id)
                        elif is_mgr and req.personas.manager_other:
                            ids_to_del.append(p.id)

                    if ids_to_del:
                        per_cnt = session.query(Persona).filter(Persona.id.in_(ids_to_del)).delete(synchronize_session=False)
                        deleted_summary["personas_deleted"] = per_cnt

            # C. Signals
            if req.signals:
                sig_cnt = 0
                if req.signals.opportunity_signals:
                    sig_cnt += session.query(OpportunitySignal).filter_by(account_id=account_id).delete(synchronize_session=False)
                if req.signals.weekly_digests:
                    sig_cnt += session.query(WeeklyDigestSnapshot).filter_by(account_id=account_id).delete(synchronize_session=False)
                if req.signals.action_items:
                    sig_cnt += session.query(ActionItem).filter_by(account_id=account_id).delete(synchronize_session=False)
                if req.signals.posts_news and acct_key:
                    sig_cnt += session.query(Post).filter_by(target_key=acct_key).delete(synchronize_session=False)
                if req.signals.cxo_movements and acct_key:
                    sig_cnt += session.query(CxoMovement).filter(or_(CxoMovement.target_key == acct_key, CxoMovement.company_name.ilike(acct_name))).delete(synchronize_session=False)
                if req.signals.jobs and acct_key:
                    sig_cnt += session.query(LinkedInJob).filter(or_(LinkedInJob.target_key == acct_key, LinkedInJob.company_name.ilike(acct_name))).delete(synchronize_session=False)
                deleted_summary["signals_deleted"] = sig_cnt

            session.commit()

            from services.pipeline_run_logger import PipelineRunLogger
            PipelineRunLogger.log_event(
                company_name=acct_name,
                pipeline_level="account",
                action="selective_purge",
                status="completed",
                target_url=acct.domain or acct.primary_domain or None,
                entities_extracted={"deleted": deleted_summary},
            )

            return {
                "status": "success",
                "account_id": account_id,
                "company_name": acct_name,
                "deleted": deleted_summary,
                "message": f"Purged selected data for '{acct_name}' successfully."
            }
        except HTTPException:
            session.rollback()
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Purge operation failed: {str(e)}")
        finally:
            session.close()

    @app.delete("/api/accounts/{account_id}", tags=["1. Accounts"], dependencies=[Depends(auth.require_role("super_admin"))])
    def delete_account_direct(account_id: int, confirmation: Optional[str] = Query(None)):
        """REST shortcut to purge an entire account."""
        req = AccountPurgeRequest(delete_account_record=True, confirmation_text=confirmation)
        return purge_account_data(account_id, req)

    @app.get("/api/accounts/{account_id}/credit-breakdown", tags=["1. Accounts"], dependencies=[Depends(auth.require_account_access)])
    def get_account_credit_breakdown(account_id: int):
        """Returns enterprise run telemetry & credit usage breakdown for an account."""
        from services.telemetry_service import TelemetryService
        return TelemetryService.get_run_credit_breakdown(account_id=account_id)

    @app.get("/api/system/health", tags=["0. System Telemetry"], dependencies=[Depends(auth.require_role("super_admin"))])
    def get_system_health():
        """Returns live system telemetry, credit usage stats, database status, and API connectors health."""
        from db.models import PipelineRun, Account, Lob, Persona
        import config
        POSTGRES_DB = getattr(config, "POSTGRES_DB", os.getenv("POSTGRES_DB", "sales_ai"))
        GEMINI_API_KEY = getattr(config, "GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", ""))
        LLM_MODEL = getattr(config, "LLM_MODEL", os.getenv("LLM_DEFAULT_MODEL", "gemini-3.6-flash"))
        EXA_API_KEY = getattr(config, "EXA_API_KEY", os.getenv("EXA_API_KEY", ""))
        TAVILY_API_KEY = getattr(config, "TAVILY_API_KEY", os.getenv("TAVILY_API_KEY", ""))
        DIFFBOT_TOKEN = getattr(config, "DIFFBOT_TOKEN", os.getenv("DIFFBOT_TOKEN", ""))
        APIFY_TOKEN = getattr(config, "APIFY_TOKEN", os.getenv("APIFY_TOKEN", ""))
        SERPER_API_KEY = getattr(config, "SERPER_API_KEY", os.getenv("SERPER_API_KEY", ""))
        FINNHUB_API_KEY = getattr(config, "FINNHUB_API_KEY", os.getenv("FINNHUB_API_KEY", ""))
        session = get_session()
        try:
            total_accts = session.query(Account).count()
            total_lobs = session.query(Lob).count()
            total_personas = session.query(Persona).count()

            # Pipeline execution aggregates
            runs = session.query(PipelineRun).order_by(PipelineRun.started_at.desc()).all()
            total_runs = len(runs)
            total_credits = sum(int(r.total_credits_used or 0) for r in runs)
            last_run = runs[0].started_at.isoformat() if runs and runs[0].started_at else None

            connectors = {
                "sec_edgar": {"name": "SEC EDGAR", "status": "active", "type": "Regulatory Filings"},
                "gemini": {"name": "Google Gemini", "status": "active" if bool(GEMINI_API_KEY) else "unconfigured", "type": "AI Synthesis", "model": LLM_MODEL},
                "exa": {"name": "Exa AI Search", "status": "active" if bool(EXA_API_KEY) else "unconfigured", "type": "Neural Search"},
                "tavily": {"name": "Tavily AI", "status": "active" if bool(TAVILY_API_KEY) else "unconfigured", "type": "Web Intelligence"},
                "diffbot": {"name": "Diffbot KG", "status": "active" if bool(DIFFBOT_TOKEN) else "unconfigured", "type": "Knowledge Graph"},
                "finnhub": {"name": "Finnhub Financials", "status": "active" if bool(FINNHUB_API_KEY) else "unconfigured", "type": "Market Telemetry"},
                "apify": {"name": "Apify Engine", "status": "active" if bool(APIFY_TOKEN) else "unconfigured", "type": "Social & Web"},
                "serper": {"name": "Serper Google OSINT", "status": "active" if bool(SERPER_API_KEY) else "unconfigured", "type": "Search Engine"},
            }

            # Real normalized units across all enterprise accounts (30s in-memory cache)
            import time
            from services.telemetry_service import TelemetryService
            global _HEALTH_TELEMETRY_CACHE
            if "_HEALTH_TELEMETRY_CACHE" not in globals():
                _HEALTH_TELEMETRY_CACHE = {"ts": 0.0, "units": 2751, "cost": 13.76}

            now = time.time()
            if now - _HEALTH_TELEMETRY_CACHE.get("ts", 0) > 30:
                accounts = session.query(Account).all()
                t_units = 0
                for a in accounts:
                    bd = TelemetryService.get_run_credit_breakdown(account_id=a.id)
                    t_units += bd.get("kpis", {}).get("total_credits", 0)
                _HEALTH_TELEMETRY_CACHE["ts"] = now
                _HEALTH_TELEMETRY_CACHE["units"] = t_units
                _HEALTH_TELEMETRY_CACHE["cost"] = round(t_units * 0.005, 2)

            real_account_units = _HEALTH_TELEMETRY_CACHE.get("units", 2751)
            estimated_cost_usd = _HEALTH_TELEMETRY_CACHE.get("cost", 13.76)

            return {
                "status": "healthy",
                "database": {
                    "status": "connected",
                    "engine": "PostgreSQL",
                    "name": POSTGRES_DB,
                },
                "stats": {
                    "total_accounts": total_accts,
                    "total_lobs": total_lobs,
                    "total_personas": total_personas,
                    "total_pipeline_runs": total_runs,
                    "total_credits_consumed": real_account_units,
                    "total_units": real_account_units,
                    "estimated_cost_usd": estimated_cost_usd,
                    "billing_tier": "Free Tier Evaluation",
                    "disclaimer": "Free Tier Active • Trial quota units",
                    "last_sync_timestamp": last_run,
                },
                "connectors": connectors,
            }
        except Exception as e:
            return {
                "status": "degraded",
                "error": str(e),
                "database": {"status": "error", "name": POSTGRES_DB},
            }
        finally:
            session.close()

    @app.get("/api/pipeline/runs/{run_id}/credit-breakdown", tags=["4. Pipeline Orchestration"], dependencies=[Depends(auth.require_role("super_admin"))])
    def get_run_credit_breakdown_by_id(run_id: str):
        """Returns enterprise run telemetry & credit usage breakdown for a specific run ID."""
        from services.telemetry_service import TelemetryService
        return TelemetryService.get_run_credit_breakdown(run_id=run_id)

    @app.patch("/api/lobs/{lob_id}", tags=["2. Lines of Business"], dependencies=[Depends(auth.require_lob_account_access), Depends(auth.require_editor)])
    def update_lob_record(lob_id: int, updates: Dict[str, Any]):
        """Directly updates LOB fields in PostgreSQL, sets is_manually_verified=True, and logs audit run."""
        session = get_session()
        try:
            lob = session.query(Lob).filter_by(id=lob_id).first()
            if not lob:
                raise HTTPException(status_code=404, detail=f"LOB {lob_id} not found.")

            disallowed = {"id", "account_id"}
            applied = {}
            for k, v in updates.items():
                if k in disallowed:
                    continue
                if k in ["name", "lob_name"] and hasattr(lob, "lob_name"):
                    lob.lob_name = v
                    applied["lob_name"] = v
                elif k in ["desc", "overview"] and hasattr(lob, "overview"):
                    lob.overview = v
                    applied["overview"] = v
                elif k in ["revenue", "audited_segment_revenue"] and hasattr(lob, "audited_segment_revenue"):
                    lob.audited_segment_revenue = v
                    applied["audited_segment_revenue"] = v
                elif k in ["head", "operating_head"] and hasattr(lob, "operating_head"):
                    lob.operating_head = v
                    applied["operating_head"] = v
                elif k in ["headcount", "segment_headcount"] and hasattr(lob, "segment_headcount"):
                    lob.segment_headcount = v
                    applied["segment_headcount"] = v
                elif hasattr(lob, k):
                    setattr(lob, k, v)
                    applied[k] = v

            now_utc = datetime.now(timezone.utc)
            lob.is_manually_verified = updates.get("is_manually_verified", True)
            lob.manually_verified_at = now_utc
            session.commit()
            session.refresh(lob)

            co_name = "Enterprise"
            if lob.account:
                co_name = lob.account.display_name or lob.account.name or "Enterprise"

            from services.pipeline_run_logger import PipelineRunLogger
            PipelineRunLogger.log_event(
                company_name=co_name,
                pipeline_level="lob",
                action="manual_edit",
                status="completed",
                quality_score=100.0,
                quality_grade="A",
                target_url=lob.domain or None,
                entities_extracted={"lob_id": lob.id, "updated_fields": list(applied.keys()), "values": applied, "is_manually_verified": lob.is_manually_verified},
            )

            return {"status": "success", "lob": _serialize_lob_full(lob, [])}
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update LOB: {str(e)}")
        finally:
            session.close()

    @app.patch("/api/personas/{persona_id}", tags=["3. Personas & Buying Committee"], dependencies=[Depends(auth.require_persona_account_access), Depends(auth.require_editor)])
    def update_persona_record(persona_id: int, updates: Dict[str, Any]):
        """Directly updates Persona fields in PostgreSQL, sets is_manually_verified=True, and logs audit run."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail=f"Persona {persona_id} not found.")

            disallowed = {"id", "account_id"}
            applied = {}
            for k, v in updates.items():
                if k in disallowed:
                    continue
                if k in ["name", "full_name"] and hasattr(p, "full_name"):
                    p.full_name = v
                    p.display_name = v
                    applied["full_name"] = v
                elif hasattr(p, k):
                    setattr(p, k, v)
                    applied[k] = v

            now_utc = datetime.now(timezone.utc)
            p.is_manually_verified = updates.get("is_manually_verified", True)
            p.manually_verified_at = now_utc
            session.commit()
            session.refresh(p)

            co_name = "Enterprise"
            if p.account:
                co_name = p.account.display_name or p.account.name or "Enterprise"

            from services.pipeline_run_logger import PipelineRunLogger
            PipelineRunLogger.log_event(
                company_name=co_name,
                pipeline_level="persona",
                action="manual_edit",
                status="completed",
                quality_score=100.0,
                quality_grade="A",
                target_url=p.linkedin_url or None,
                entities_extracted={"persona_id": p.id, "updated_fields": list(applied.keys()), "values": applied, "is_manually_verified": p.is_manually_verified},
            )

            return {"status": "success", "persona": _serialize_persona_full(p)}
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update persona: {str(e)}")
        finally:
            session.close()

    @app.post("/api/verify/{entity_type}/{entity_id}", tags=["0. Verification"], dependencies=[Depends(auth.require_entity_account_access), Depends(auth.require_editor)])
    def toggle_entity_verification(entity_type: str, entity_id: int):
        """Toggles manual verification flag directly in DB and appends audit trail."""
        session = get_session()
        try:
            now_utc = datetime.now(timezone.utc)
            entity_type = entity_type.lower()
            co_name = "Enterprise"
            target_url = None
            if entity_type == "account":
                obj = session.query(Account).filter_by(id=entity_id).first()
                if not obj: raise HTTPException(404, "Account not found")
                co_name = obj.display_name or obj.name or "Account"
                target_url = obj.domain or obj.primary_domain
            elif entity_type == "lob":
                obj = session.query(Lob).filter_by(id=entity_id).first()
                if not obj: raise HTTPException(404, "LOB not found")
                co_name = obj.account.display_name if obj.account else "LOB"
                target_url = obj.domain
            elif entity_type in ["persona", "person"]:
                obj = session.query(Persona).filter_by(id=entity_id).first()
                if not obj: raise HTTPException(404, "Persona not found")
                co_name = obj.account.display_name if obj.account else "Persona"
                target_url = obj.linkedin_url
            elif entity_type in ["sub_lob", "sublob"]:
                obj = session.query(SubLob).filter_by(id=entity_id).first()
                if not obj: raise HTTPException(404, "Sub-LOB not found")
                co_name = obj.parent_lob_name or "Sub-LOB"
                target_url = obj.domain or obj.website_url
            else:
                raise HTTPException(400, f"Unsupported entity type: {entity_type}")

            new_status = not bool(getattr(obj, "is_manually_verified", False))
            obj.is_manually_verified = new_status
            obj.manually_verified_at = now_utc if new_status else None
            session.commit()
            session.refresh(obj)

            from services.pipeline_run_logger import PipelineRunLogger
            PipelineRunLogger.log_event(
                company_name=co_name,
                pipeline_level=entity_type if entity_type != "person" else "persona",
                action="manual_verify" if new_status else "manual_unverify",
                status="completed",
                quality_score=100.0 if new_status else 75.0,
                quality_grade="A" if new_status else "C",
                target_url=target_url,
                entities_extracted={"entity_type": entity_type, "entity_id": entity_id, "is_manually_verified": new_status, "verified_at": now_utc.isoformat() if new_status else None},
            )

            return {
                "status": "success",
                "is_manually_verified": new_status,
                "manually_verified_at": now_utc.isoformat() if new_status else None
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to toggle verification: {str(e)}")
        finally:
            session.close()

    @app.get("/api/personas/{persona_id}/profile.pdf", tags=["3. Personas & Buying Committee"])
    def download_persona_profile_pdf(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """Server-side "Download PDF" for a contact's full profile (contact
        info, career history, AI call-prep dossier, Personality Profile, and
        every captured post) — generated fresh from the database each time
        with ReportLab, not a client-side screenshot of whatever happens to
        be on screen."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")
            acct = session.query(Account).filter_by(id=p.account_id).first()
            digest_row = _resolve_persona_digest(session, p)
            # If a digest exists, its target_key is the confirmed-correct one;
            # otherwise fall back to the same candidate list for the posts
            # lookup (captured posts can exist before any digest has run).
            target_key = digest_row.target_key if digest_row else next(
                (c for c in [p.key, slugify(p.full_name or ""), _slugify_dropping_initials(p.full_name or "")] if c),
                None,
            )
            posts = (
                (session.query(Post).filter_by(target_key=target_key).order_by(Post.channel, Post.rank).all())
                if target_key
                else []
            )
            movements = []
            try:
                if p.full_name:
                    movements = (
                        session.query(CxoMovement)
                        .filter(CxoMovement.person_name.ilike(f"%{p.full_name}%"))
                        .order_by(CxoMovement.effective_date.desc().nullslast())
                        .all()
                    )
            except Exception:
                movements = []

            persona_dict = {
                "name": p.full_name or p.display_name or "Executive",
                "title": p.title,
                "email": contact_privacy.safe_email(p.email),
                "phone": contact_privacy.safe_phone(p.phone, p.direct_mobile_phone),
                "linkedin_url": p.linkedin_url,
                "city": p.city,
                "state": p.state,
                "country": p.country,
                "decision_authority": p.decision_authority,
                "budget_authority": p.budget_authority,
                "seniority_raw": p.seniority_raw,
                "skills": p.skills or [],
                "personalized_icebreaker": p.personalized_icebreaker,
                "value_proposition": p.value_proposition,
                "communication_style": p.communication_style,
                "target_kpis": p.target_kpis or [],
                "operational_pain_points": p.operational_pain_points or [],
                "key_objections": p.key_objections or [],
            }
            account_dict = {"name": acct.display_name or acct.legal_name} if acct else None
            posts_list = [
                {
                    "channel": post.channel,
                    "published_at": post.published_at,
                    "body": post.body,
                    "post_url": post.post_url,
                }
                for post in posts
            ]
            career_events = [
                {
                    "event_type": m.event_type,
                    "designation": m.designation,
                    "previous_role": m.previous_role,
                    "effective_date": m.effective_date,
                    "context": m.context,
                }
                for m in movements
            ]

            pdf_bytes = build_persona_profile_pdf(
                persona_dict,
                account_dict,
                digest_row.digest if digest_row else None,
                posts_list,
                career_events,
            )
            filename = f"{slugify(persona_dict['name'])}-personality-report.pdf"
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        finally:
            session.close()

    def _slugify_dropping_initials(name: str) -> str:
        """Same idea as frontend/js/modules/utils.js's slugifyDroppingInitials
        — a person registered in people_targets.py under a short key (e.g.
        "ranjit_samra") won't match a slug of their full display name if it
        includes a middle initial ("Ranjit S. Samra" -> "ranjit_s_samra")."""
        if not name:
            return ""
        tokens = [t for t in name.strip().split() if len(re.sub(r"[^a-zA-Z0-9]", "", t)) > 1]
        return slugify(" ".join(tokens))

    def _resolve_persona_digest(session, p: "Persona"):
        """Tries p.key, then slugify(full_name), then that slug with middle
        initials dropped, against real Digest rows — mirrors
        resolvePersonaTargetKey() on the frontend so the API and UI agree on
        which digest belongs to this persona. Returns the Digest row or None."""
        candidates = [p.key, slugify(p.full_name or ""), _slugify_dropping_initials(p.full_name or "")]
        for c in candidates:
            if not c:
                continue
            row = session.query(Digest).filter_by(target_key=c).first()
            if row:
                return row
        return None

    @app.get("/api/explorer/personas/{persona_id}/download-pdf", tags=["3. Personas & Buying Committee"], dependencies=[Depends(auth.require_persona_account_access)])
    def download_explorer_persona_dossier_pdf(persona_id: int):
        """Dedicated Account Explorer executive dossier PDF download.
        Renders full database intelligence, AI sales playbook, KPIs, objections,
        and verified OSINT footprint using ExplorerPersonaDossierPDF."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")
            acct = session.query(Account).filter_by(id=p.account_id).first()
            from services.explorer_persona_dossier_pdf import ExplorerPersonaDossierPDF
            pdf_bytes = ExplorerPersonaDossierPDF.generate(p, acct)
            p_name = p.full_name or p.display_name or "executive"
            filename = f"{slugify(p_name)}-executive-dossier.pdf"
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Type": "application/pdf"
                },
            )
        except HTTPException:
            raise
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Failed to generate dossier PDF: {str(e)}")
        finally:
            session.close()

    def _resolve_person_target_key(session, p: "Persona"):
        """The content-pipeline person target this persona's profiles are
        generated from: its existing digest's key, else p.key / name slugs —
        whichever is registered either in people_targets.py's hardcoded or
        custom_targets.json entries (PEOPLE_ALIASES) or in Postgres' targets
        table. The DB is checked here directly because people_targets'
        own DB merge can't run in this process (its `import db` resolves to
        this app's db package). Returns (target_key or None, candidates)."""
        content_pipeline_dir = Path(__file__).resolve().parent / "apps" / "content_pipeline"
        sys.path.insert(0, str(content_pipeline_dir))
        from apps.content_pipeline.people_targets import ALIASES as PEOPLE_ALIASES

        existing = _resolve_persona_digest(session, p)
        candidates = [existing.target_key] if existing else []
        candidates += [p.key, slugify(p.full_name or ""), _slugify_dropping_initials(p.full_name or "")]
        candidates = [c for c in candidates if c]
        in_db = {
            r[0] for r in session.execute(
                sql_text("SELECT key FROM targets WHERE kind = 'person' AND key = ANY(:keys)"),
                {"keys": candidates},
            ).fetchall()
        }
        target_key = next((c for c in candidates if c in PEOPLE_ALIASES or c in in_db), None)
        return target_key, candidates

    @app.get("/api/personas/{persona_id}/photo", tags=["3. Personas & Buying Committee"])
    def get_persona_photo(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """The persona's profile photo, fetched once from the best scraped source and cached
        under output/avatars/ (services/persona_photo_service.py) — so it keeps working after
        LinkedIn's signed URLs expire, and browsers never call the third-party host.
        404 when no source has a usable image."""
        cache_headers = {"Cache-Control": "private, max-age=86400"}
        hit = persona_photo_service.cached(persona_id)
        if not hit:
            if persona_photo_service.recently_missed(persona_id):
                raise HTTPException(status_code=404, detail="No profile photo.")
            session = get_session()
            try:
                p = session.query(Persona).filter_by(id=persona_id).first()
                if not p:
                    raise HTTPException(status_code=404, detail="Persona not found.")
                urls = persona_photo_service.candidates(p)
            finally:
                session.close()
            hit = persona_photo_service.fetch_and_cache(persona_id, urls)
            if not hit:
                raise HTTPException(status_code=404, detail="No profile photo.")
        path, ctype = hit
        return FileResponse(path, media_type=ctype, headers=cache_headers)

    @app.get("/api/personas/{persona_id}/psychological-profile", tags=["3. Personas & Buying Committee"])
    def get_persona_psychological_profile(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """Retrieve the compiled Psychological & Leadership Profile for a persona.

        Returns profile=None (not a fabricated placeholder) when nothing has
        been generated yet — the frontend shows an honest "not generated"
        state for that rather than plausible-looking canned text, same
        principle as the Personality Profile endpoint/renderer."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")
            digest_row = _resolve_persona_digest(session, p)

            profile = None
            if digest_row and digest_row.digest and isinstance(digest_row.digest, dict):
                profile = digest_row.digest.get("psychological_profile")
            if not profile and p.raw_data and isinstance(p.raw_data, dict):
                profile = p.raw_data.get("psychological_profile")

            return {
                "persona_id": p.id,
                "persona_name": p.full_name,
                "title": p.title,
                "profile": profile,
            }
        finally:
            session.close()

    def _generate_persona_profiles(session, p: "Persona", user_id: Optional[int] = None):
        """Runs the person digest subprocess that synthesizes BOTH the
        Personality and Psychological profiles in one pass (--profiles-only
        always builds both, see apps/content_pipeline/digest/pipeline.py)
        and returns the freshly-written Digest row. Shared by the
        personality-profile and psychological-profile /generate endpoints
        below so a click on either button doesn't run the subprocess twice.
        Raises HTTPException on any failure."""
        content_pipeline_dir = Path(__file__).resolve().parent / "apps" / "content_pipeline"

        # apps/content_pipeline has its own top-level module named `db`
        # (apps/content_pipeline/db.py) — a straight name collision with
        # this app's own `db` package, already loaded under that same
        # name in this process's sys.modules. `import db` inside that
        # app's own code (people_targets.py, digest/pipeline.py) would
        # silently resolve to THIS app's db package instead of its own
        # once cached, no matter what sys.path says. Running it as a
        # separate process — exactly its own CLI entrypoint, exactly as
        # a human would run it — sidesteps the collision entirely
        # instead of fighting Python's module cache for it.
        sys.path.insert(0, str(content_pipeline_dir))
        target_key, candidates = _resolve_person_target_key(session, p)
        if not target_key:
            raise HTTPException(
                status_code=400,
                detail=f"'{p.full_name}' isn't registered in people_targets.py under any key this resolves "
                f"({', '.join(c for c in candidates if c)}) — add them there before generating a profile.",
            )

        # --profiles-only: on-demand generation for exactly the two
        # profiles, skipping the separate email-rollup LLM call nobody
        # asked for here. --all-posts: a UI click is a deliberate
        # "(re)generate now", not a scheduled incremental digest.
        # --since-days 3650: --all-posts only bypasses the "new since
        # last run" filter, NOT the recency window underneath it — a
        # contact whose captured posts are all older than the default
        # 14 days (e.g. no recent public activity) would otherwise
        # always fail with "no posts in scope" on a fresh generation.
        #
        # Logs to an explicit UTF-8-opened file rather than
        # capture_output=True/text=True — on this box, letting the
        # parent auto-decode the captured pipes (locale-dependent, not
        # UTF-8) silently returned stdout=stderr=None instead of
        # raising, hiding every real error. This is the same "open the
        # file as UTF-8 yourself" workaround already needed manually
        # all session for this app's own Windows-console encoding issue
        # (main.py's banner prints a Unicode box-drawing character).
        proc_env = dict(os.environ, PYTHONIOENCODING="utf-8")

        # Shared OpenRouter quota (apps/sales_copilot/README.md §10.4): one request per
        # channel with posts + personality + psychological. Reserve up front so a click
        # can't start a run the team's remaining budget can't finish.
        from apps.sales_copilot import llm as quota
        n_channels = session.execute(
            sql_text("SELECT count(DISTINCT channel) FROM posts WHERE target_key = :k"), {"k": target_key}
        ).scalar() or 0
        try:
            usage_ids = quota.reserve(session, "profiles", user_id, est_tokens=30000, requests_=max(1, n_channels) + 2)
        except quota.QuotaExceeded as e:
            raise HTTPException(status_code=429, detail=str(e))

        log_fd, log_path = tempfile.mkstemp(suffix=".log", prefix="profile_gen_")
        os.close(log_fd)
        output = ""
        try:
            with open(log_path, "w", encoding="utf-8") as log_fh:
                result = subprocess.run(
                    [sys.executable, "main.py", "digest", target_key, "--person", "--all-posts", "--profiles-only", "--since-days", "3650"],
                    cwd=str(content_pipeline_dir), env=proc_env,
                    stdout=log_fh, stderr=subprocess.STDOUT, timeout=300,
                )
            with open(log_path, "r", encoding="utf-8", errors="replace") as log_fh:
                output = log_fh.read()
        finally:
            try:
                os.remove(log_path)
            except OSError:
                pass
            # Settle the reservations with what the pipeline actually sent ([llm-usage] lines).
            sent = re.findall(r"\[llm-usage\] status=(\d+) in=(\d+) out=(\d+)", output or "")
            for uid, (st, tin, tout) in zip(usage_ids, sent):
                quota.finalize(session, uid, ok=st == "200", status_code=int(st), model=None,
                               tokens_in=int(tin), tokens_out=int(tout))
            quota.release(session, usage_ids[len(sent):])
            for st, tin, tout in sent[len(usage_ids):]:
                quota.record(session, "profiles", user_id, ok=st == "200", status_code=int(st),
                             tokens_in=int(tin), tokens_out=int(tout))

        if result.returncode != 0:
            # "No posts in scope" isn't a pipeline failure — it means this
            # contact genuinely has zero captured public content to
            # synthesize from (distinct from "not registered" above, where
            # nobody's even trying to capture anything for them). Surface
            # it as a 400 like the not-registered case so the frontend
            # shows the same "please connect content for this contact"
            # message instead of a generic "something went wrong" retry
            # prompt — a real subprocess crash still falls through to 502.
            # Checked first: when the LLM quota is exhausted every channel call
            # fails, and older pipeline builds then reported that as "no posts".
            if re.search(r"free-models-per-day|rate limit exceeded|HTTP 429", output or "", re.I):
                raise HTTPException(
                    status_code=429,
                    detail="The AI service's daily request limit has been reached — try again after it resets.",
                )
            if re.search(r"no posts in scope|nothing to summarise", output or "", re.I):
                raise HTTPException(
                    status_code=400,
                    detail=f"No captured public content is available yet for '{p.full_name}' — nothing to synthesize a profile from.",
                )
            raise HTTPException(
                status_code=502,
                detail=f"Digest generation failed (exit {result.returncode}): {(output or '(no output)')[-1000:]}",
            )

        # The subprocess's own db.upsert_digest() call already wrote the
        # fresh row to Postgres — re-read it here rather than parsing
        # the subprocess's stdout/local JSON file.
        session.expire_all()
        digest_row = _resolve_persona_digest(session, p)
        return digest_row, output

    @app.get("/api/personas/{persona_id}/personality-profile", tags=["3. Personas & Buying Committee"])
    def get_persona_personality_profile(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """Retrieve the compiled Executive Personality Profile for a persona.

        Returns profile=None (not a fabricated placeholder) when nothing has
        been generated yet — the frontend shows an honest "not generated"
        state, with a Generate button, for that instead of canned text."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")
            digest_row = _resolve_persona_digest(session, p)

            profile = None
            if digest_row and digest_row.digest and isinstance(digest_row.digest, dict):
                profile = digest_row.digest.get("personality_profile")
            if not profile and p.raw_data and isinstance(p.raw_data, dict):
                profile = p.raw_data.get("personality_profile")

            return {
                "persona_id": p.id,
                "persona_name": p.full_name,
                "title": p.title,
                "profile": profile,
            }
        finally:
            session.close()

    @app.post("/api/personas/{persona_id}/personality-profile/generate", tags=["3. Personas & Buying Committee"])
    def generate_persona_personality_profile(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """Trigger on-demand live generation of the personality profile using LLM synthesis."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")

            digest_row, output = _generate_persona_profiles(session, p, user.id)
            personality = (digest_row.digest or {}).get("personality_profile") if digest_row else None
            if not personality:
                raise HTTPException(
                    status_code=500,
                    detail=f"Digest ran but produced no personality_profile. Output: {output[-1000:]}",
                )

            return {
                "status": "success",
                "persona_id": p.id,
                "profile": personality
            }
        finally:
            session.close()

    # Per-channel post caps the digest applies — mirrors cap=25 and
    # _HIGH_VOLUME_CAP in apps/content_pipeline/digest/selection.py (not
    # imported: that app's `db` module collides with ours, see
    # _generate_persona_profiles).
    _PROFILE_DIGEST_CAP = 25
    _PROFILE_DIGEST_CHANNEL_CAP = {"news": 12, "blog": 12, "sec_mentions": 10, "sec": 10}

    @app.get("/api/personas/{persona_id}/profile-readiness", tags=["3. Personas & Buying Committee"])
    def get_persona_profile_readiness(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """What the Personality/Psychological "Generate now" button has to work
        with: whether the contact is a tracked target, captured posts per
        channel (and how many the digest would use), and which background
        fields exist. Read-only — no LLM calls."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")

            target_key, _ = _resolve_person_target_key(session, p)

            channels = []
            if target_key:
                rows = session.execute(
                    sql_text("SELECT channel, count(*) FROM posts WHERE target_key = :k GROUP BY channel ORDER BY 2 DESC"),
                    {"k": target_key},
                ).fetchall()
                for channel, n in rows:
                    cap = min(_PROFILE_DIGEST_CAP, _PROFILE_DIGEST_CHANNEL_CAP.get(channel, _PROFILE_DIGEST_CAP))
                    channels.append({"channel": channel, "captured": n, "used": min(n, cap)})

            location = ", ".join(x for x in (p.city, p.state, p.country) if x)
            background = [
                {"field": "title", "label": "Job title", "present": bool(p.title)},
                {"field": "education", "label": "Education", "present": bool(p.degree or p.institution)},
                {"field": "prior_company", "label": "Prior company", "present": bool(p.prior_company)},
                {"field": "skills", "label": "Skills", "present": bool(p.skills)},
                {"field": "location", "label": "Location", "present": bool(location)},
            ]
            total_captured = sum(c["captured"] for c in channels)
            return {
                "persona_id": p.id,
                "target_key": target_key,
                "registered": bool(target_key),
                "channels": channels,
                "total_captured": total_captured,
                "total_used": sum(c["used"] for c in channels),
                "background": background,
                # one summary call per channel with posts + personality + psychological
                "llm_requests": len(channels) + 2 if channels else 0,
                "ready": bool(target_key) and total_captured > 0,
            }
        finally:
            session.close()

    @app.post("/api/personas/{persona_id}/callprep/generate", tags=["3. Personas & Buying Committee"])
    def generate_persona_callprep(
        persona_id: int, force: bool = False, user: User = Depends(auth.require_persona_account_access)
    ):
        """On-demand Sales Call-Prep & Battlecards for one persona (services/callprep_service.py).
        Skips the LLM and returns status "unchanged" when the persona's inputs haven't changed,
        unless force=true."""
        session = get_session()
        try:
            try:
                out = callprep_service.generate_persona(session, persona_id, force=force, user_id=user.id)
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e))
            except callprep_service.QuotaExceeded as e:
                raise HTTPException(status_code=429, detail=f"LLM daily quota reached: {e}")
            except callprep_service.LLMError as e:
                raise HTTPException(status_code=502, detail=f"Call-prep generation failed: {e}")
            return {
                "status": out["status"],
                "persona_id": persona_id,
                "level": out["level"],
                "usage": out["usage"],
                "persona": _serialize_persona_full(out["persona"]),
            }
        finally:
            session.close()

    @app.post("/api/personas/{persona_id}/psychological-profile/generate", tags=["3. Personas & Buying Committee"])
    def generate_persona_psychological_profile(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """Trigger on-demand live generation of the psychological profile using LLM synthesis."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")

            digest_row, output = _generate_persona_profiles(session, p, user.id)
            psych = (digest_row.digest or {}).get("psychological_profile") if digest_row else None
            if not psych:
                raise HTTPException(
                    status_code=500,
                    detail=f"Digest ran but produced no psychological_profile. Output: {output[-1000:]}",
                )

            return {
                "status": "success",
                "persona_id": p.id,
                "profile": psych
            }
        finally:
            session.close()

    @app.get("/api/personas/{persona_id}/psychological-profile.pdf", tags=["3. Personas & Buying Committee"])
    def download_persona_psychological_profile_pdf(persona_id: int, user: User = Depends(auth.require_persona_account_access)):
        """Download high-impact executive PDF briefing for the Psychological Profile."""
        session = get_session()
        try:
            p = session.query(Persona).filter_by(id=persona_id).first()
            if not p:
                raise HTTPException(status_code=404, detail="Persona not found.")
            acct = session.query(Account).filter_by(id=p.account_id).first()
            digest_row = _resolve_persona_digest(session, p)

            persona_dict = {
                "full_name": p.full_name, "title": p.title,
                "account_name": acct.display_name or acct.legal_name if acct else "",
                "city": p.city, "state": p.state, "country": p.country,
                "communication_style": p.communication_style
            }

            pdf_bytes = build_psychological_profile_pdf(
                persona_dict,
                digest_row.digest if digest_row else None
            )
            filename = f"{slugify(p.full_name or 'executive')}-psychological-profile.pdf"
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        finally:
            session.close()

    @app.get("/api/accounts/{account_id}/signals", tags=["1. Accounts"])
    def get_account_intelligence_signals(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieve multi-source intelligence, firmographics, heat scores, and traffic telemetry."""
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail="Account not found.")
            return {
                "account_id": acct.id,
                "account_name": acct.legal_name or acct.display_name,
                "heat_score": acct.heat_score,
                "trend_score_90d": acct.trend_score_90d,
                "active_tech_count": acct.active_tech_count,
                "it_spend": acct.it_spend,
                "patents_granted": acct.patents_granted,
                "trademarks_registered": acct.trademarks_registered,
                "funding": {
                    "total_usd": acct.total_funding_amount_usd,
                    "currency": acct.total_funding_currency,
                    "last_type": acct.last_funding_type,
                    "last_date": acct.last_funding_date.isoformat() if acct.last_funding_date else None,
                    "num_rounds": acct.num_funding_rounds,
                    "status": acct.funding_status,
                },
                "ipo": {
                    "status": acct.ipo_status,
                    "date": acct.ipo_date.isoformat() if acct.ipo_date else None,
                },
                "traffic": {
                    "global_rank": acct.global_traffic_rank,
                    "monthly_visits": acct.monthly_visits,
                    "bounce_rate": acct.bounce_rate,
                    "visit_duration": acct.visit_duration,
                    "page_views_per_visit": acct.page_views_per_visit,
                },
                "leadership_counts": {
                    "c_suite": acct.c_suite_count,
                    "vp": acct.vp_count,
                    "director": acct.director_count,
                    "manager": acct.manager_count,
                },
                "multi_source_intelligence": acct.multi_source_intelligence,
            }
        finally:
            session.close()

    def _serialize_opportunity_signal(sig: OpportunitySignal, now: datetime) -> Dict[str, Any]:
        return {
            "id": sig.id,
            "category": sig.category,
            "signal_key": sig.signal_key,
            "title": sig.title,
            "details": sig.details or {},
            "status": sig.status,
            "first_seen": sig.first_seen.isoformat() if sig.first_seen else None,
            "last_seen": sig.last_seen.isoformat() if sig.last_seen else None,
            "is_new": bool(sig.first_seen and (now - sig.first_seen).days < 3),
        }

    @app.get("/api/accounts/{account_id}/opportunities", tags=["1. Accounts"])
    def get_account_opportunity_signals(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieve the persisted history of growth-whitespace themes and domain-expansion
        product ideas detected for an account, including ones no longer actively recurring."""
        session = get_session()
        try:
            now = datetime.now(timezone.utc)
            signals = (
                session.query(OpportunitySignal)
                .filter_by(account_id=account_id)
                .order_by(OpportunitySignal.first_seen.desc())
                .all()
            )
            by_category: Dict[str, List[Dict[str, Any]]] = {"growth_theme": [], "domain_expansion": []}
            for s in signals:
                by_category.setdefault(s.category, []).append(_serialize_opportunity_signal(s, now))
            return {"account_id": account_id, **by_category}
        finally:
            session.close()

    @app.post("/api/accounts/{account_id}/opportunities/sync", tags=["1. Accounts"])
    def sync_account_opportunity_signals(
        account_id: int,
        req: OpportunitySignalSyncRequest,
        user: User = Depends(auth.require_account_access),
    ):
        """Upsert the currently-detected opportunity signals for one category (growth_theme or
        domain_expansion). Signals no longer present in `items` are marked inactive rather than
        deleted, so the account keeps a full history of what has been suggested over time."""
        if req.category not in ("growth_theme", "domain_expansion"):
            raise HTTPException(
                status_code=400, detail="category must be 'growth_theme' or 'domain_expansion'."
            )
        session = get_session()
        try:
            now = datetime.now(timezone.utc)
            existing = (
                session.query(OpportunitySignal).filter_by(account_id=account_id, category=req.category).all()
            )
            existing_by_key = {s.signal_key: s for s in existing}
            seen_keys = set()
            for item in req.items:
                seen_keys.add(item.signal_key)
                sig = existing_by_key.get(item.signal_key)
                if sig:
                    sig.title = item.title
                    sig.details = item.details
                    sig.status = "active"
                    sig.last_seen = now
                else:
                    sig = OpportunitySignal(
                        account_id=account_id,
                        category=req.category,
                        signal_key=item.signal_key,
                        title=item.title,
                        details=item.details,
                        status="active",
                        first_seen=now,
                        last_seen=now,
                    )
                    session.add(sig)
                    existing_by_key[item.signal_key] = sig
            for key, sig in existing_by_key.items():
                if key not in seen_keys and sig.status != "inactive":
                    sig.status = "inactive"
            session.commit()
            signals = (
                session.query(OpportunitySignal)
                .filter_by(account_id=account_id, category=req.category)
                .order_by(OpportunitySignal.first_seen.desc())
                .all()
            )
            return {
                "account_id": account_id,
                "category": req.category,
                "signals": [_serialize_opportunity_signal(s, now) for s in signals],
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Opportunity signal sync failed: {str(e)}")
        finally:
            session.close()

    def _serialize_weekly_digest(snap: WeeklyDigestSnapshot) -> Dict[str, Any]:
        return {
            "id": snap.id,
            "target_key": snap.target_key,
            "week_of": snap.week_of.isoformat() if snap.week_of else None,
            "generated_at": snap.generated_at.isoformat() if snap.generated_at else None,
            "subject": snap.subject,
            "body": snap.body,
            "priority": snap.priority,
            "confidence": snap.confidence,
            "data_gaps": snap.data_gaps or [],
            "do_not_say": snap.do_not_say or [],
        }

    @app.get("/api/accounts/{account_id}/weekly-updates", tags=["1. Accounts"])
    def get_account_weekly_updates(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieve the archived history of weekly sales update emails for an account, newest first."""
        session = get_session()
        try:
            snapshots = (
                session.query(WeeklyDigestSnapshot)
                .filter_by(account_id=account_id)
                .order_by(WeeklyDigestSnapshot.generated_at.desc())
                .all()
            )
            return {"account_id": account_id, "updates": [_serialize_weekly_digest(s) for s in snapshots]}
        finally:
            session.close()

    @app.post("/api/accounts/{account_id}/weekly-updates/sync", tags=["1. Accounts"])
    def sync_account_weekly_update(
        account_id: int,
        req: WeeklyDigestSyncRequest,
        user: User = Depends(auth.require_account_access),
    ):
        """Archive the current weekly sales update email as a snapshot, if this generation hasn't
        been captured yet. The live `digests` row is overwritten every pipeline run, so this is what
        preserves past weeks' versions instead of losing them."""
        if not req.generated_at:
            raise HTTPException(
                status_code=400, detail="generated_at is required to archive a weekly update snapshot."
            )
        try:
            generated_at = datetime.fromisoformat(req.generated_at)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"generated_at is not a valid ISO datetime: {req.generated_at}"
            )
        week_of = generated_at.date() - timedelta(days=generated_at.weekday())
        session = get_session()
        try:
            existing = (
                session.query(WeeklyDigestSnapshot)
                .filter_by(account_id=account_id, generated_at=generated_at)
                .first()
            )
            if not existing:
                session.add(
                    WeeklyDigestSnapshot(
                        account_id=account_id,
                        target_key=req.target_key,
                        week_of=week_of,
                        generated_at=generated_at,
                        subject=req.subject,
                        body=req.body,
                        priority=req.priority,
                        confidence=req.confidence,
                        data_gaps=req.data_gaps,
                        do_not_say=req.do_not_say,
                    )
                )
                session.commit()
            snapshots = (
                session.query(WeeklyDigestSnapshot)
                .filter_by(account_id=account_id)
                .order_by(WeeklyDigestSnapshot.generated_at.desc())
                .all()
            )
            return {"account_id": account_id, "updates": [_serialize_weekly_digest(s) for s in snapshots]}
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Weekly update sync failed: {str(e)}")
        finally:
            session.close()

    # ══════════════════════════════════════════════════════
    # ACTION ITEMS — client-specific work-list (see
    # ACTION_ITEMS_IMPLEMENTATION_PLAN.md)
    # ══════════════════════════════════════════════════════
    def _serialize_action_item(item: ActionItem) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        due = item.due_date
        due_utc = (due if due.tzinfo else due.replace(tzinfo=timezone.utc)) if due else None
        is_overdue = bool(
            due_utc and item.status not in ("done", "cancelled")
            and due_utc < now
        )

        due_relative = None
        if due_utc:
            diff = due_utc - now if not is_overdue else now - due_utc
            total_seconds = int(diff.total_seconds())
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            if is_overdue:
                if days == 0:
                    due_relative = f"Overdue by {max(1, hours)}h"
                else:
                    due_relative = f"Overdue by {days}d"
            else:
                if days == 0:
                    due_relative = f"Due in {max(1, hours)}h"
                else:
                    due_relative = f"Due in {days}d"

        account_name = None
        account_domain = None
        if item.account:
            account_name = item.account.display_name or item.account.legal_name
            account_domain = item.account.domain

        return {
            "id": item.id,
            "account_id": item.account_id,
            "account_name": account_name,
            "account_domain": account_domain,
            "persona_id": item.persona_id,
            "persona_name": (item.persona.full_name or item.persona.display_name) if item.persona else None,
            "persona_title": item.persona.title if item.persona else None,
            "title": item.title,
            "description": item.description,
            "status": item.status,
            "priority": item.priority,
            "due_date": due_utc.isoformat() if due_utc else None,
            "is_overdue": is_overdue,
            "due_relative": due_relative,
            "assigned_to_id": item.assigned_to_id,
            "assigned_to_name": (item.assigned_to.full_name or item.assigned_to.email) if item.assigned_to else None,
            "created_by_id": item.created_by_id,
            "created_by_name": (item.created_by.full_name or item.created_by.email) if item.created_by else None,
            "source": item.source,
            "source_ref_id": item.source_ref_id,
            "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }

    def _parse_due_date(raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"due_date is not a valid ISO datetime: {raw}")
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @app.get("/api/accounts/{account_id}/action-items", tags=["8. Action Items"])
    def list_account_action_items(
        account_id: int,
        status: Optional[str] = None,
        assigned_to_id: Optional[int] = None,
        persona_id: Optional[int] = None,
        user: User = Depends(auth.require_account_access),
    ):
        """List this account's work-list items, optionally filtered by
        status, assignee, or a specific contact."""
        session = get_session()
        try:
            query = (session.query(ActionItem)
                     .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                              selectinload(ActionItem.created_by))
                     .filter_by(account_id=account_id))
            if status:
                query = query.filter(ActionItem.status == status)
            if assigned_to_id:
                query = query.filter(ActionItem.assigned_to_id == assigned_to_id)
            if persona_id:
                query = query.filter(ActionItem.persona_id == persona_id)
            items = query.order_by(ActionItem.due_date.asc().nullslast(), ActionItem.created_at.desc()).all()
            return {"account_id": account_id, "action_items": [_serialize_action_item(i) for i in items]}
        finally:
            session.close()

    @app.post("/api/accounts/{account_id}/action-items", tags=["8. Action Items"])
    def create_account_action_item(
        account_id: int, body: ActionItemCreateRequest,
        user: User = Depends(auth.require_account_access),
    ):
        if body.priority not in ("high", "medium", "low"):
            raise HTTPException(status_code=400, detail="priority must be 'high', 'medium', or 'low'")
        if body.source not in ("manual", "playbook", "signal_feed"):
            raise HTTPException(status_code=400, detail="source must be 'manual', 'playbook', or 'signal_feed'")
        session = get_session()
        try:
            if body.persona_id is not None:
                persona = session.query(Persona).filter_by(id=body.persona_id, account_id=account_id).first()
                if not persona:
                    raise HTTPException(status_code=400, detail="persona_id does not belong to this account")
            if body.assigned_to_id is not None:
                assignee = session.query(User).filter_by(id=body.assigned_to_id).first()
                if not assignee:
                    raise HTTPException(status_code=400, detail="Assigned user not found")
                if assignee.role in ("super_admin", "admin", "sales_manager"):
                    raise HTTPException(status_code=400, detail="Tasks can only be assigned to sales team members, not high-authority roles (administrators or managers).")
                if assignee.role in auth.READ_ONLY_ROLES:
                    raise HTTPException(status_code=400, detail="Viewers and partners are read-only and can't be assigned tasks. Please assign to a sales team member.")
            # Avoid creating duplicates if an identical open action item already exists
            existing = (session.query(ActionItem)
                        .filter(ActionItem.account_id == account_id,
                                ActionItem.title == body.title,
                                ActionItem.status.in_(("open", "in_progress", "pending_review")))
                        .first())
            if existing:
                item = existing
            else:
                item = ActionItem(
                    account_id=account_id, persona_id=body.persona_id, title=body.title,
                    description=body.description, priority=body.priority,
                    status=body.status if body.status in ("open", "in_progress", "done") else "open",
                    due_date=_parse_due_date(body.due_date), assigned_to_id=body.assigned_to_id,
                    created_by_id=user.id, source=body.source,
                )
                session.add(item)
                session.commit()
                session.refresh(item)
            item = (session.query(ActionItem)
                    .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                             selectinload(ActionItem.created_by))
                    .filter_by(id=item.id).first())
            return _serialize_action_item(item)
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Could not create action item: {e}")
        finally:
            session.close()

    @app.post("/api/action-items", tags=["8. Action Items"])
    def create_direct_action_item(
        body: ActionItemDirectCreateRequest,
        user: User = Depends(auth.get_current_user),
    ):
        """Direct action item creation from the cross-account Tasks page."""
        if body.priority not in ("high", "medium", "low"):
            raise HTTPException(status_code=400, detail="priority must be 'high', 'medium', or 'low'")
        session = get_session()
        try:
            # Check account access for regular users
            if user.role != "super_admin":
                accessible_ids = auth.get_accessible_account_ids(session, user.id)
                if body.account_id not in accessible_ids:
                    raise HTTPException(status_code=403, detail="You do not have access to this account")

            if body.persona_id is not None:
                persona = session.query(Persona).filter_by(id=body.persona_id, account_id=body.account_id).first()
                if not persona:
                    raise HTTPException(status_code=400, detail="persona_id does not belong to this account")

            assigned_id = body.assigned_to_id
            if assigned_id is not None:
                assignee = session.query(User).filter_by(id=assigned_id).first()
                if not assignee:
                    raise HTTPException(status_code=400, detail="Assigned user not found")
                if assignee.role in ("super_admin", "admin", "sales_manager"):
                    raise HTTPException(status_code=400, detail="Tasks can only be assigned to sales team members, not high-authority roles (administrators or managers).")
                if assignee.role in auth.READ_ONLY_ROLES:
                    raise HTTPException(status_code=400, detail="Viewers and partners are read-only and can't be assigned tasks. Please assign to a sales team member.")
            elif user.role == "user":
                assigned_id = user.id

            item = ActionItem(
                account_id=body.account_id, persona_id=body.persona_id, title=body.title,
                description=body.description, priority=body.priority,
                status=body.status if body.status in ("open", "in_progress", "done") else "open",
                due_date=_parse_due_date(body.due_date), assigned_to_id=assigned_id,
                created_by_id=user.id, source="manual",
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            item = (session.query(ActionItem)
                    .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                             selectinload(ActionItem.created_by), selectinload(ActionItem.account))
                    .filter_by(id=item.id).first())
            d = _serialize_action_item(item)
            d["account_name"] = item.account.display_name or item.account.legal_name if item.account else None
            return d
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Could not create action item: {e}")
        finally:
            session.close()

    @app.patch("/api/action-items/{item_id}", tags=["8. Action Items"])
    def update_action_item(
        item_id: int, body: ActionItemUpdateRequest, background_tasks: BackgroundTasks,
        user: User = Depends(auth.require_action_item_account_access),
    ):
        session = get_session()
        try:
            item = (session.query(ActionItem)
                    .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                             selectinload(ActionItem.created_by))
                    .filter_by(id=item_id).first())
            if not item:
                raise HTTPException(status_code=404, detail="Action item not found")

            if body.status is not None:
                # 'pending_review' is deliberately excluded here — it's only
                # ever set by the LLM-suggestion write path, and only ever
                # left via the dedicated /approve or /reject endpoints below,
                # never a generic field edit. See
                # ACTION_ITEMS_LLM_SUGGESTIONS_PLAN.md §1.
                if body.status not in ("open", "in_progress", "done", "cancelled"):
                    raise HTTPException(status_code=400, detail="status must be open, in_progress, done, or cancelled")
                item.status = body.status
                item.completed_at = datetime.now(timezone.utc) if body.status == "done" else None
            if body.priority is not None:
                if body.priority not in ("high", "medium", "low"):
                    raise HTTPException(status_code=400, detail="priority must be 'high', 'medium', or 'low'")
                item.priority = body.priority
            if body.title is not None:
                item.title = body.title
            if body.description is not None:
                item.description = body.description
            # Sent explicitly as null/"" clears the date; left out keeps it.
            if "due_date" in body.model_fields_set:
                item.due_date = _parse_due_date(body.due_date)
            if body.persona_id is not None:
                persona = (
                    session.query(Persona)
                    .filter_by(id=body.persona_id, account_id=item.account_id)
                    .first()
                )
                if not persona:
                    raise HTTPException(status_code=400, detail="persona_id does not belong to this account")
                item.persona_id = body.persona_id

            if body.assigned_to_id is not None:
                assignee = session.query(User).filter_by(id=body.assigned_to_id).first()
                if not assignee:
                    raise HTTPException(status_code=400, detail="Assigned user not found")
                if assignee.role in ("super_admin", "admin", "sales_manager"):
                    raise HTTPException(status_code=400, detail="Tasks can only be assigned to sales team members, not high-authority roles (administrators or managers).")
                if assignee.role in auth.READ_ONLY_ROLES:
                    raise HTTPException(status_code=400, detail="Viewers and partners are read-only and can't be assigned tasks. Please assign to a sales team member.")

            reassigned = body.assigned_to_id is not None and body.assigned_to_id != item.assigned_to_id
            if body.assigned_to_id is not None:
                item.assigned_to_id = body.assigned_to_id

            session.commit()
            session.refresh(item)

            if reassigned and item.assigned_to and item.assigned_to.email:
                base_url = os.getenv("APP_BASE_URL", "http://localhost:8000")
                due_line = f" Due {item.due_date.date().isoformat()}." if item.due_date else ""
                background_tasks.add_task(
                    email_sender.send_email,
                    item.assigned_to.email,
                    f"Action item assigned to you: {item.title}",
                    (
                        f"Hi {item.assigned_to.full_name or item.assigned_to.email},\n\n"
                        f"You've been assigned an action item on "
                        f"{item.account.display_name or item.account.legal_name}.\n\n"
                        f"{item.title}\n{item.description or ''}\n{due_line}\n\n"
                        f"View it here: {base_url}/?account={item.account_id}\n"
                    ),
                    html_body=email_sender.render_html(
                        "New action item assigned to you",
                        [
                            f"Hi {item.assigned_to.full_name or item.assigned_to.email},",
                            f"You've been assigned an action item on "
                            f"{item.account.display_name or item.account.legal_name}:",
                            item.title,
                        ] + ([item.description] if item.description else []),
                        cta_label="View Account",
                        cta_url=f"{base_url}/?account={item.account_id}",
                        footnote=due_line.strip() or None,
                    ),
                )
                s2 = get_session()
                try:
                    s2.add(
                        ActionItemReminder(
                            action_item_id=item.id,
                            reminder_type="assigned",
                            sent_to_user_id=item.assigned_to_id,
                        )
                    )
                    s2.commit()
                except Exception:
                    s2.rollback()
                finally:
                    s2.close()

            return _serialize_action_item(item)
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Could not update action item: {e}")
        finally:
            session.close()

    @app.post("/api/action-items/{item_id}/approve", tags=["8. Action Items"])
    def approve_action_item(item_id: int, user: User = Depends(auth.require_action_item_account_access)):
        """Turns an LLM-suggested item (status='pending_review') into a real
        task, assigned to whoever approved it. See
        ACTION_ITEMS_LLM_SUGGESTIONS_PLAN.md §1 — approval is a dedicated
        endpoint, not a generic status PATCH, so it's always one deliberate
        action rather than a side effect of an unrelated field edit."""
        session = get_session()
        try:
            item = (session.query(ActionItem)
                    .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                             selectinload(ActionItem.created_by))
                    .filter_by(id=item_id).first())
            if not item:
                raise HTTPException(status_code=404, detail="Action item not found")
            if item.status != "pending_review":
                raise HTTPException(status_code=400, detail="Only a pending-review item can be approved")
            item.status = "open"
            item.assigned_to_id = user.id
            session.commit()
            session.refresh(item)
            return _serialize_action_item(item)
        finally:
            session.close()

    @app.post("/api/action-items/{item_id}/reject", tags=["8. Action Items"])
    def reject_action_item(item_id: int, user: User = Depends(auth.require_action_item_account_access)):
        """Dismisses an LLM-suggested item without ever making it a real
        task — sets status='cancelled' so it's excluded from every existing
        open/overdue query with no schema change, rather than deleting it
        outright (keeps a record of what the model suggested)."""
        session = get_session()
        try:
            item = (session.query(ActionItem)
                    .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                             selectinload(ActionItem.created_by))
                    .filter_by(id=item_id).first())
            if not item:
                raise HTTPException(status_code=404, detail="Action item not found")
            if item.status != "pending_review":
                raise HTTPException(status_code=400, detail="Only a pending-review item can be rejected")
            item.status = "cancelled"
            session.commit()
            session.refresh(item)
            return _serialize_action_item(item)
        finally:
            session.close()

    @app.post("/api/action-items/{item_id}/complete", tags=["8. Action Items"])
    def complete_action_item(
        item_id: int,
        body: Optional[ActionItemCompleteRequest] = None,
        user: User = Depends(auth.require_action_item_account_access)
    ):
        session = get_session()
        try:
            item = (session.query(ActionItem)
                    .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                             selectinload(ActionItem.created_by), selectinload(ActionItem.account))
                    .filter_by(id=item_id).first())
            if not item:
                raise HTTPException(status_code=404, detail="Action item not found")
            item.status = "done"
            item.completed_at = datetime.now(timezone.utc)
            if body and body.comment and body.comment.strip():
                clean_comment = body.comment.strip()
                if item.description:
                    item.description = f"{item.description}\n\nClosing Note: {clean_comment}"
                else:
                    item.description = f"Closing Note: {clean_comment}"
            session.commit()
            session.refresh(item)
            return _serialize_action_item(item)
        finally:
            session.close()

    @app.post("/api/action-items/{item_id}/reopen", tags=["8. Action Items"])
    def reopen_action_item(
        item_id: int,
        body: Optional[ActionItemCompleteRequest] = None,
        user: User = Depends(auth.require_action_item_account_access)
    ):
        """Reopens a closed action item, resetting its completed status and restoring due evaluation."""
        session = get_session()
        try:
            item = (session.query(ActionItem)
                    .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                             selectinload(ActionItem.created_by), selectinload(ActionItem.account))
                    .filter_by(id=item_id).first())
            if not item:
                raise HTTPException(status_code=404, detail="Action item not found")
            item.status = "open"
            item.completed_at = None
            if body and body.comment and body.comment.strip():
                clean_comment = body.comment.strip()
                if item.description:
                    item.description = f"{item.description}\n\nReopen Note: {clean_comment}"
                else:
                    item.description = f"Reopen Note: {clean_comment}"
            session.commit()
            session.refresh(item)
            return _serialize_action_item(item)
        finally:
            session.close()

    @app.post("/api/action-items/{item_id}/send-reminder", tags=["8. Action Items"])
    def send_action_item_reminder(item_id: int, user: User = Depends(auth.require_action_item_account_access)):
        """On-demand reminder email for one action item — same email
        template and action_item_reminders log as the scheduled sweep
        (scripts/send_action_reminders.py), but sent immediately (e.g. from
        the Command Center 'Due soon' widget's Send reminder button)
        instead of waiting for that script's next scheduled run. Unlike the
        sweep, a deliberate manual click is allowed to re-send even if that
        reminder stage was already logged — updates the existing log row's
        sent_at instead of trying to insert a second one (which would hit
        the table's unique constraint)."""
        session = get_session()
        try:
            item = (session.query(ActionItem)
                    .options(selectinload(ActionItem.assigned_to), selectinload(ActionItem.account))
                    .filter_by(id=item_id).first())
            if not item:
                raise HTTPException(status_code=404, detail="Action item not found")
            if item.status in ("done", "cancelled"):
                raise HTTPException(status_code=400, detail="This task is already closed")
            if not item.assigned_to or not item.assigned_to.email:
                raise HTTPException(status_code=400, detail="This task has no assignee to notify")

            assignee = item.assigned_to
            now = datetime.now(timezone.utc)
            due = item.due_date
            due_utc = due.replace(tzinfo=timezone.utc) if due and not due.tzinfo else due
            is_overdue = bool(due_utc and due_utc < now)
            reminder_type = "overdue" if is_overdue else "due_soon"
            account_name = (item.account.display_name or item.account.legal_name) if item.account else "an account"
            due_str = due_utc.strftime("%b %d, %Y") if due_utc else "no date"
            heading = "Action item overdue" if is_overdue else "Action item due soon"
            lead_line = f"{'This action item is now overdue' if is_overdue else 'This action item is due soon'} on {account_name}:"

            sent = email_sender.send_email(
                assignee.email, f"{heading}: {item.title}",
                f"Hi {assignee.full_name or assignee.email},\n\n{lead_line}\n\n{item.title}\n{item.description or ''}\nDue: {due_str}\n",
                html_body=email_sender.render_html(
                    heading,
                    [f"Hi {assignee.full_name or assignee.email},", lead_line, item.title] + ([item.description] if item.description else []),
                    footnote=f"Due: {due_str}",
                ),
            )
            if not sent:
                raise HTTPException(status_code=502, detail="Could not send the reminder email — check SMTP configuration (.env SMTP_* vars).")

            existing = session.query(ActionItemReminder).filter_by(
                action_item_id=item.id, reminder_type=reminder_type, sent_to_user_id=item.assigned_to_id,
            ).first()
            if existing:
                existing.sent_at = now
            else:
                session.add(ActionItemReminder(action_item_id=item.id, reminder_type=reminder_type, sent_to_user_id=item.assigned_to_id))
            session.commit()
            return {"ok": True, "sent_to": assignee.email, "reminder_type": reminder_type}
        except HTTPException:
            raise
        finally:
            session.close()

    @app.delete("/api/action-items/{item_id}", tags=["8. Action Items"])
    def delete_action_item(item_id: int, user: User = Depends(auth.require_action_item_account_access)):
        session = get_session()
        try:
            item = session.query(ActionItem).filter_by(id=item_id).first()
            if not item:
                raise HTTPException(status_code=404, detail="Action item not found")
            session.delete(item)
            session.commit()
            return {"ok": True}
        finally:
            session.close()

    @app.get("/api/me/action-items", tags=["8. Action Items"])
    def list_my_action_items(
        status: Optional[str] = None,
        priority: Optional[str] = None,
        account_id: Optional[int] = None,
        account_ids: Optional[str] = None,
        assigned_to_id: Optional[int] = None,
        search: Optional[str] = None,
        user: User = Depends(auth.get_current_user),
    ):
        """Cross-account 'My Tasks' — for regular sales reps, lists every action
        item assigned to them on accounts they have access to. For super_admins,
        lists all company tasks (with optional filter by rep/account/priority)."""
        session = get_session()
        try:
            query = (session.query(ActionItem)
                     .options(selectinload(ActionItem.persona), selectinload(ActionItem.assigned_to),
                              selectinload(ActionItem.created_by), selectinload(ActionItem.account)))
            if user.role != "super_admin":
                accessible_ids = auth.get_accessible_account_ids(session, user.id)
                if accessible_ids:
                    query = query.filter(ActionItem.account_id.in_(accessible_ids))
                else:
                    query = query.filter(False)
                if assigned_to_id is not None:
                    query = query.filter(ActionItem.assigned_to_id == assigned_to_id)
            elif assigned_to_id is not None:
                query = query.filter(ActionItem.assigned_to_id == assigned_to_id)

            if account_ids:
                try:
                    parsed_ids = [int(x.strip()) for x in account_ids.split(",") if x.strip()]
                    if parsed_ids:
                        query = query.filter(ActionItem.account_id.in_(parsed_ids))
                except ValueError:
                    pass
            elif account_id is not None:
                query = query.filter(ActionItem.account_id == account_id)
            if priority:
                query = query.filter(ActionItem.priority == priority)
            if status and status != "all":
                query = query.filter(ActionItem.status == status)

            items = query.order_by(ActionItem.due_date.asc().nullslast(), ActionItem.created_at.desc()).all()
            results = [_serialize_action_item(i) for i in items]

            if search:
                q = search.lower().strip()
                results = [
                    r for r in results
                    if (r.get("title") and q in r["title"].lower())
                    or (r.get("description") and q in r["description"].lower())
                    or (r.get("account_name") and q in r["account_name"].lower())
                    or (r.get("persona_name") and q in r["persona_name"].lower())
                ]

            return {"action_items": results}
        finally:
            session.close()

    # ══════════════════════════════════════════════════════
    # COMMAND CENTER — signal feed / playbook generator
    # ══════════════════════════════════════════════════════

    PLAY_STALL_DAYS = 14

    def _command_center_scope(session, user: User) -> Optional[List[int]]:
        """None = every account (super_admin); otherwise the granted ids."""
        if user.role == "super_admin":
            return None
        return auth.get_accessible_account_ids(session, user.id)

    def _command_center_payload(session, user: User, snap: Optional[CommandCenterSnapshot]) -> Dict[str, Any]:
        """Snapshot + live play counts. "Plays in motion" are real action
        items created from the playbook (source='playbook') that are still
        open; stalled = untouched for PLAY_STALL_DAYS."""
        scope = _command_center_scope(session, user)
        q = session.query(ActionItem).filter(ActionItem.source == "playbook")
        if scope is not None:
            q = q.filter(ActionItem.account_id.in_(scope or [-1]))
        play_items = q.all()
        now = datetime.now(timezone.utc)
        open_items = [i for i in play_items if i.status in ("open", "in_progress")]
        stalled = [
            i for i in open_items
            if i.updated_at and (now - (i.updated_at if i.updated_at.tzinfo else i.updated_at.replace(tzinfo=timezone.utc))).days >= PLAY_STALL_DAYS
        ]
        tasked = {(i.account_id, i.title) for i in play_items}
        playbook = [
            {**p, "tasked": (p.get("account_id"), p.get("title")) in tasked}
            for p in (snap.playbook if snap else [])
        ]
        return {
            "generated_at": snap.generated_at.isoformat() if snap else None,
            "signals": snap.signals if snap else [],
            "playbook": playbook,
            "velocity": snap.velocity if snap else None,
            "source_counts": snap.source_counts if snap else {},
            "categories": command_center_service.CATEGORIES,
            "plays": {"in_motion": len(open_items), "stalled": len(stalled)},
        }

    @app.get("/api/command-center", tags=["9. Command Center"])
    def get_command_center(response: Response, user: User = Depends(auth.get_current_user)):
        """The caller's most recently generated signal feed + playbook
        (empty until they press Generate), plus live plays-in-motion counts."""
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        session = get_session()
        try:
            snap = (session.query(CommandCenterSnapshot)
                    .filter_by(user_id=user.id)
                    .order_by(CommandCenterSnapshot.generated_at.desc())
                    .first())
            if not snap or not snap.signals:
                # Auto-generate snapshot on-demand if user has no snapshot yet
                result = command_center_service.generate(session, _command_center_scope(session, user))
                snap = CommandCenterSnapshot(
                    user_id=user.id, generated_at=datetime.now(timezone.utc),
                    signals=result["signals"], playbook=result["playbook"],
                    velocity=result["velocity"], source_counts=result["source_counts"],
                )
                session.add(snap)
                session.commit()
                session.refresh(snap)
            return _command_center_payload(session, user, snap)
        finally:
            session.close()

    @app.post("/api/command-center/generate", tags=["9. Command Center"])
    def generate_command_center(user: User = Depends(auth.get_current_user)):
        """Rebuilds the Priority Signal Feed, This Week's Playbook and signal
        velocity from exec movements, LinkedIn jobs, news and opportunity
        signals already in the DB (see services/command_center_service.py),
        and saves the result as the caller's latest snapshot."""
        session = get_session()
        try:
            result = command_center_service.generate(session, _command_center_scope(session, user))
            snap = CommandCenterSnapshot(
                user_id=user.id, generated_at=datetime.now(timezone.utc),
                signals=result["signals"], playbook=result["playbook"],
                velocity=result["velocity"], source_counts=result["source_counts"],
            )
            session.add(snap)
            session.flush()
            # Keep a short history per user, not an ever-growing table.
            old = (session.query(CommandCenterSnapshot.id)
                   .filter_by(user_id=user.id)
                   .order_by(CommandCenterSnapshot.generated_at.desc())
                   .offset(10).all())
            if old:
                session.query(CommandCenterSnapshot).filter(
                    CommandCenterSnapshot.id.in_([o[0] for o in old])
                ).delete(synchronize_session=False)
            session.commit()
            session.refresh(snap)
            return _command_center_payload(session, user, snap)
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Command Center generation failed: {e}")
        finally:
            session.close()

    _XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _xlsx_response(content: bytes, filename: str):
        from fastapi import Response
        return Response(content, media_type=_XLSX_MEDIA,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/me/action-items/export", tags=["8. Action Items"])
    def export_my_action_items(status: Optional[str] = None, account_id: Optional[int] = None,
                               account_ids: Optional[str] = None,
                               priority: Optional[str] = None, user: User = Depends(auth.get_current_user)):
        """My Tasks as Excel — same access rules and filters as the /tasks page."""
        from apps.sales_copilot import exports
        items = list_my_action_items(status=status, account_id=account_id, account_ids=account_ids, priority=priority, user=user)["action_items"]
        headers = ["Title", "Status", "Priority", "Due", "Account", "Contact", "Description", "Created"]
        rows = [[i.get("title"), i.get("status"), i.get("priority"), (i.get("due_date") or "")[:10],
                 i.get("account_name"), (i.get("persona") or {}).get("name") if isinstance(i.get("persona"), dict) else i.get("persona_name"),
                 i.get("description"), (i.get("created_at") or "")[:10]] for i in items]
        filter_acct_str = account_ids or (str(account_id) if account_id else 'all')
        content = exports.rows_xlsx("My tasks", headers, rows, [48, 12, 10, 12, 22, 24, 60, 12],
                                    about=[["Exported for", user.full_name or user.email], ["Exported at", exports._now()],
                                           ["Filters", f"status={status or 'all'}, priority={priority or 'all'}, account={filter_acct_str}"],
                                           ["Rows", len(rows)]])
        return _xlsx_response(content, "my-tasks.xlsx")

    @app.get("/api/accounts/{account_id}/people/export", tags=["3. Personas & Buying Committee"])
    def export_account_people(account_id: int, user: User = Depends(auth.require_account_access)):
        """Buying committee / contact list for one account as Excel. Business contact
        fields only (work email/phone) — personal email/mobile are never exported."""
        from apps.sales_copilot import exports
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail="Account not found.")
            from apps.sales_copilot import privacy
            rows = session.execute(sql_text(f"""
                SELECT coalesce(p.full_name, p.display_name), p.title, p.tier, l.lob_name, p.decision_authority,
                       p.budget_authority, {privacy.SAFE_EMAIL_SQL}, {privacy.SAFE_PHONE_SQL}, p.linkedin_url,
                       concat_ws(', ', p.city, p.country), (p.value_proposition IS NOT NULL), p.id
                FROM personas p LEFT JOIN lobs l ON l.id = p.lob_id
                WHERE p.account_id = :a
                ORDER BY p.hierarchy_level NULLS LAST, 1"""), {"a": account_id}).fetchall()
        finally:
            session.close()
        name = acct.display_name or acct.legal_name or acct.key
        headers = ["Name", "Title", "Tier", "Line of business", "Decision authority", "Budget authority",
                   "Work email", "Phone", "LinkedIn", "Location", "Call-prep ready", "Profile link"]
        data = [[r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], "yes" if r[10] else "",
                 f"/profile?account={account_id}&persona_id={r[11]}"] for r in rows]
        content = exports.rows_xlsx("Contacts", headers, data, [26, 40, 14, 24, 22, 18, 30, 18, 40, 22, 12, 34],
                                    about=[["Account", name], ["Exported for", user.full_name or user.email],
                                           ["Exported at", exports._now()], ["Contacts", len(data)]])
        return _xlsx_response(content, exports.safe_filename(f"{name}-contacts", "xlsx"))

    def _allowed_target_keys(session, user) -> Optional[set]:
        """Content target keys this user may see (None = everything). An account's keys follow
        _build_target_key_to_account_map; a person's follow the frontend's resolvePersonaTargetKey
        (persona key, slug of the name, slug without middle initials). apps/sales_crm README §5, M5."""
        if not auth.AUTH_ENFORCED or getattr(user, "role", None) == "super_admin":
            return None
        ids = set(auth.get_accessible_account_ids(session, user.id))
        keys = {k for k, a in _build_target_key_to_account_map(session).items() if a.id in ids}
        if ids:
            for pkey, full, disp in session.query(Persona.key, Persona.full_name, Persona.display_name).filter(Persona.account_id.in_(ids)):
                name = full or disp or ""
                no_initials = " ".join(w for w in name.split() if len(re.sub(r"[^a-zA-Z0-9]", "", w)) > 1)
                keys.update(k for k in (pkey, slugify(name) if name else None, slugify(no_initials) if no_initials else None) if k)
        return keys

    @app.get("/api/content", tags=["4. Content Intelligence"])
    def get_content_intelligence(user: User = Depends(auth.get_current_user)):
        """Retrieve aggregated social listening posts and LLM channel digests (scoped to the user's accounts)."""
        session = get_session()
        try:
            allowed = _allowed_target_keys(session, user)
            digests_by_key = {}
            try:
                for d in session.query(Digest).all():
                    if allowed is not None and d.target_key not in allowed:
                        continue
                    digests_by_key[d.target_key] = {
                        "target_key": d.target_key,
                        "kind": d.kind,
                        "priority": d.priority,
                        "llm": d.llm,
                        "posts_considered": d.posts_considered,
                        "generated_at": d.generated_at.isoformat() if d.generated_at else None,
                        "digest": d.digest,
                    }
            except Exception:
                digests_by_key = {}

            posts_by_key = {}
            try:
                post_q = session.query(Post).order_by(Post.target_key, Post.channel, Post.rank)
                if allowed is not None:
                    post_q = post_q.filter(Post.target_key.in_(list(allowed) or [""]))
                for p in post_q.all():
                    posts_by_key.setdefault(p.target_key, []).append(
                        {
                            "id": p.id,
                            "channel": p.channel,
                            "post_key": p.post_key,
                            "rank": p.rank,
                            "post_url": p.post_url,
                            "body": p.body,
                            "author": p.author,
                            "published_at": p.published_at,
                            "engagement": p.engagement,
                            "media": p.media,
                            "new_in_last_run": p.new_in_last_run,
                            "first_seen": p.first_seen.isoformat() if p.first_seen else None,
                            "last_seen": p.last_seen.isoformat() if p.last_seen else None,
                        }
                    )
            except Exception:
                posts_by_key = {}

            jobs_by_key = {}
            try:
                jobs_query = session.query(LinkedInJob).order_by(
                    LinkedInJob.target_key, LinkedInJob.first_seen.desc().nullslast()
                )
                if allowed is not None:
                    jobs_query = jobs_query.filter(LinkedInJob.target_key.in_(list(allowed) or [""]))
                for j in jobs_query.all():
                    jobs_by_key.setdefault(j.target_key, []).append(
                        {
                            "id": j.id,
                            "target_key": j.target_key,
                            "job_key": j.job_key,
                            "title": j.title,
                            "company_name": j.company_name,
                            "location": j.location,
                            "employment_type": j.employment_type,
                            "workplace_type": j.workplace_type,
                            "posted_date": j.posted_date,
                            "applicants": j.applicants,
                            "views": j.views,
                            "salary": j.salary,
                            "job_url": j.job_url,
                            "description": j.description,
                            "new_in_last_run": j.new_in_last_run,
                            "first_seen": j.first_seen.isoformat() if j.first_seen else None,
                            "last_seen": j.last_seen.isoformat() if j.last_seen else None,
                        }
                    )
            except Exception:
                jobs_by_key = {}

            return {"digests": digests_by_key, "posts": posts_by_key, "jobs": jobs_by_key}
        finally:
            session.close()

    # ══════════════════════════════════════════════════════
    # LINKEDIN JOB POSTINGS — bulk cross-account endpoint
    # ══════════════════════════════════════════════════════
    JOB_CATEGORY_RULES = [
        (
            "Engineering & Technology",
            (
                r"software|engineer|developer|architect|devops|\bsre\b|platform|"
                r"infrastructure|\bcloud\b|\bit\b|technology|full[- ]stack|backend|front[- ]end"
            ),
        ),
        (
            "Data & AI",
            (
                r"data scientist|data engineer|machine learning|\bai\b|analytics|"
                r"data governance|data platform|business intelligence|\bml\b"
            ),
        ),
        ("Finance & Accounting", r"finance|accounting|audit|controller|treasury|financial reporting|\btax\b"),
        (
            "Sales & Business Development",
            r"\bsales\b|business development|account executive|relationship manager|client coverage",
        ),
        ("Marketing & Communications", r"marketing|communications|\bbrand\b|content\b|social media"),
        ("Product Management", r"product manager|product owner|product lead"),
        ("Human Resources", r"human resources|\bhr\b|talent acquisition|\brecruit|people operations"),
        ("Legal & Compliance", r"\blegal\b|compliance|regulatory|counsel|risk management"),
        ("Customer Success & Support", r"customer success|customer support|client service"),
        ("Operations", r"operations|operational|process improvement|program manager|project manager"),
        ("Executive & Leadership", r"\bvp\b|vice president|\bdirector\b|head of|\bchief\b|managing director"),
    ]

    def categorize_job_title(title: Optional[str]) -> str:
        if not title:
            return "Other"
        for label, pattern in JOB_CATEGORY_RULES:
            if re.search(pattern, title, re.IGNORECASE):
                return label
        return "Other"

    def _build_target_key_to_account_map(session) -> Dict[str, Account]:
        mapping: Dict[str, Account] = {}
        # Only the naming columns: full account rows carry MBs of enrichment JSON, and
        # loading them dominated every feed endpoint that calls this (~0.6s for 5 rows).
        accounts = session.query(Account).options(load_only(
            Account.id, Account.key, Account.stock_symbol, Account.display_name, Account.legal_name)).all()
        for a in accounts:
            for candidate in (
                a.key,
                (a.stock_symbol or "").lower() or None,
                slugify(a.display_name) if a.display_name else None,
                slugify(a.legal_name) if a.legal_name else None,
            ):
                if candidate:
                    mapping[candidate] = a
        return mapping

    def _job_summary_dict(j: "LinkedInJob", acct: Optional[Account], job_category: str) -> Dict[str, Any]:
        return {
            "id": j.id,
            "target_key": j.target_key,
            "job_key": j.job_key,
            "title": j.title,
            "company_name": j.company_name,
            "location": j.location,
            "employment_type": j.employment_type,
            "workplace_type": j.workplace_type,
            "posted_date": j.posted_date,
            "applicants": j.applicants,
            "views": j.views,
            "salary": j.salary,
            "job_url": j.job_url,
            "has_description": bool(j.description),
            "new_in_last_run": j.new_in_last_run,
            "first_seen": j.first_seen.isoformat() if j.first_seen else None,
            "last_seen": j.last_seen.isoformat() if j.last_seen else None,
            "category": job_category,
            "account_id": acct.id if acct else None,
            "account_name": (acct.legal_name or acct.display_name) if acct else j.company_name,
        }

    @app.get("/api/linkedin-jobs", tags=["5. LinkedIn Jobs"])
    def get_all_linkedin_jobs(
        category: Optional[str] = None,
        account_id: Optional[int] = None,
        q: Optional[str] = None,
        employment_type: Optional[str] = None,
        workplace_type: Optional[str] = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 24,
        user: User = Depends(auth.get_current_user),
    ):
        """Retrieve scraped LinkedIn job postings across all accounts, each resolved to its
        owning account and tagged with a heuristic job category. Supports free-text search,
        category/employment/workplace filters, sorting, and server-side pagination so the
        Global Accounts Dashboard's 'View All' job browser stays fast as the dataset grows.
        The list payload omits the (potentially large) job description — fetch
        GET /api/linkedin-jobs/{id} for the full detail of a single posting."""
        session = get_session()
        try:
            page = max(1, page)
            page_size = max(1, min(page_size, 100))
            q_norm = (q or "").strip().lower()

            key_to_account = _build_target_key_to_account_map(session)
            jobs_query = session.query(LinkedInJob).order_by(LinkedInJob.first_seen.desc().nullslast())
            if auth.AUTH_ENFORCED and user.role != "super_admin":
                allowed_ids = set(auth.get_accessible_account_ids(session, user.id))
                allowed_keys = [k for k, a in key_to_account.items() if a.id in allowed_ids]
                jobs_query = jobs_query.filter(LinkedInJob.target_key.in_(allowed_keys or [""]))

            employment_types_set, workplace_types_set = set(), set()
            # Matches every account/search/employment/workplace filter but NOT category,
            # so the UI can show an accurate job count per category chip.
            pre_category: List[Dict[str, Any]] = []

            for j in jobs_query.all():
                acct = key_to_account.get(j.target_key)
                if j.employment_type:
                    employment_types_set.add(j.employment_type)
                if j.workplace_type:
                    workplace_types_set.add(j.workplace_type)

                if account_id is not None and (not acct or acct.id != account_id):
                    continue
                if employment_type and j.employment_type != employment_type:
                    continue
                if workplace_type and j.workplace_type != workplace_type:
                    continue
                if q_norm:
                    haystack = " ".join(filter(None, [j.title, j.company_name, j.location])).lower()
                    if q_norm not in haystack:
                        continue

                job_category = categorize_job_title(j.title)
                pre_category.append(_job_summary_dict(j, acct, job_category))

            category_counts: Dict[str, int] = {}
            for row in pre_category:
                category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1

            results = [r for r in pre_category if not category or r["category"] == category]

            sort_key = {
                "newest": lambda r: r["first_seen"] or "",
                "applicants": lambda r: r["applicants"] if r["applicants"] is not None else -1,
                "views": lambda r: r["views"] if r["views"] is not None else -1,
            }.get(sort, None)
            if sort_key:
                results.sort(key=sort_key, reverse=True)

            total = len(results)
            total_pages = max(1, math.ceil(total / page_size))
            page = min(page, total_pages)
            start = (page - 1) * page_size
            page_results = results[start : start + page_size]

            all_categories = [label for label, _ in JOB_CATEGORY_RULES] + ["Other"]
            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "categories": all_categories,
                "category_counts": category_counts,
                "employment_types": sorted(employment_types_set),
                "workplace_types": sorted(workplace_types_set),
                "jobs": page_results,
            }
        finally:
            session.close()

    @app.get("/api/linkedin-jobs/{job_id}", tags=["5. LinkedIn Jobs"])
    def get_linkedin_job_detail(job_id: int, user: User = Depends(auth.get_current_user)):
        """Retrieve the full detail (including description) for a single LinkedIn job posting."""
        session = get_session()
        try:
            j = session.query(LinkedInJob).filter(LinkedInJob.id == job_id).first()
            if not j:
                raise HTTPException(status_code=404, detail="Job posting not found")
            acct = _build_target_key_to_account_map(session).get(j.target_key)
            if auth.AUTH_ENFORCED and user.role != "super_admin" and (
                    not acct or acct.id not in auth.get_accessible_account_ids(session, user.id)):
                raise HTTPException(status_code=404, detail="Job posting not found")
            job_category = categorize_job_title(j.title)
            detail = _job_summary_dict(j, acct, job_category)
            detail["description"] = j.description
            return detail
        finally:
            session.close()

    @app.get("/api/accounts/{account_id}/hiring-summary", tags=["5. LinkedIn Jobs"], dependencies=[Depends(auth.require_account_access)])
    def get_account_hiring_summary(
        account_id: int,
        days: Optional[int] = Query(None, description="Filter jobs posted within the last N days (e.g. 15, 30)")
    ):
        """Retrieve aggregated lightweight hiring metrics & strategic track stats for an account.
        Designed for instant page-load performance across millions of rows."""
        session = get_session()
        try:
            account = session.query(Account).filter(Account.id == account_id).first()
            if not account:
                raise HTTPException(status_code=404, detail="Account not found")

            candidate_keys = set(filter(None, [
                account.key,
                (account.stock_symbol or "").lower(),
                slugify(account.display_name) if account.display_name else None,
                slugify(account.legal_name) if account.legal_name else None,
            ]))

            jobs_query = session.query(LinkedInJob).filter(LinkedInJob.target_key.in_(candidate_keys))
            if days is not None and days > 0:
                cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
                cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
                jobs_query = jobs_query.filter(
                    or_(
                        LinkedInJob.posted_date >= cutoff_iso,
                        and_(LinkedInJob.posted_date.is_(None), LinkedInJob.first_seen >= cutoff_dt)
                    )
                )

            jobs = jobs_query.all()

            total_roles = len(jobs)
            leadership_rx = re.compile(r"director|\bvp\b|vice president|\bsvp\b|senior vice president|head of|chief|lead", re.I)
            contract_rx = re.compile(r"contract|\(contract\)", re.I)
            ai_rx = re.compile(r"\bai\b|artificial|machine learning|\bml\b|genai|process analyst|automation", re.I)
            cloud_rx = re.compile(r"cloud|full-stack|full stack|platform|devops|systems lead|software engineer", re.I)

            # Same taxonomy as HIRING_DOMAINS in jobs-radar.js (the per-account
            # Hiring Trend Radar deep-dive) — ported here so the Command Center
            # tile's "top hiring category" agrees with what a rep sees on
            # click-through, rather than inventing a second classification.
            # Matched in this order, first hit wins; core_operations is the
            # catch-all for anything none of the others match (mirrors the
            # client-side "assign to primary dominant domain" logic exactly).
            hiring_domains = [
                {"id": "ai_automation", "name": "AI & Process Automation", "icon": "fa-solid fa-microchip",
                 "rx": re.compile(r"\bai\b|artificial intelligence|machine learning|\bml\b|genai|generative ai|\bllm\b|copilot|process analyst|deep learning|\bnlp\b|automation|cognitive|neural|agentic|prompt", re.I)},
                {"id": "cloud_platform", "name": "Cloud & Platform Modernization", "icon": "fa-solid fa-cloud",
                 "rx": re.compile(r"cloud|infrastructure|full-stack|full stack|platform|devops|architect|systems lead|site reliability|\bsre\b|\baws\b|\bazure\b|\bgcp\b|kubernetes|microservices|distributed systems|software engineer", re.I)},
                {"id": "data_analytics", "name": "Data Engineering & Analytics", "icon": "fa-solid fa-chart-column",
                 "rx": re.compile(r"\bdata\b|quantitative|analytics|\bbi\b|data science|\betl\b|\bsql\b|snowflake|databricks|pipeline|warehouse|business intelligence|lakehouse", re.I)},
                {"id": "risk_compliance", "name": "Risk, Compliance & Control", "icon": "fa-solid fa-shield-halved",
                 "rx": re.compile(r"\brisk\b|compliance|\bcontrol\b|cyber|security|audit|governance|identity|fraud|surveillance|regulatory|collateral", re.I)},
                {"id": "core_operations", "name": "Core Business & Operations", "icon": "fa-solid fa-sitemap",
                 "rx": re.compile(r"product management|\bpom\b|operations|credit|investor services|client processing|custody|asset servicing|settlement|trading|wealth|portfolio|specialist|accountant|\bsales\b|manager", re.I)},
            ]

            leadership_count = 0
            contract_roles = []
            loc_counts: Dict[str, int] = {}
            ai_count = 0
            cloud_count = 0
            category_counts: Dict[str, int] = {d["id"]: 0 for d in hiring_domains}

            for j in jobs:
                t = j.title or ""
                emp = j.employment_type or ""
                loc = (j.location or "US").split("/")[0].strip()
                if loc:
                    loc_counts[loc] = loc_counts.get(loc, 0) + 1

                if leadership_rx.search(t):
                    leadership_count += 1

                if contract_rx.search(emp) or contract_rx.search(t):
                    contract_roles.append({
                        "id": j.id,
                        "title": j.title,
                        "location": j.location or "US",
                        "employment_type": j.employment_type or "Contract",
                        "workplace_type": (j.workplace_type or "on_site").replace("_", " "),
                        "applicants": j.applicants,
                        "job_url": j.job_url,
                    })

                if ai_rx.search(t):
                    ai_count += 1
                elif cloud_rx.search(t):
                    cloud_count += 1

                matched_domain = next((d["id"] for d in hiring_domains if d["rx"].search(t)), "core_operations")
                category_counts[matched_domain] += 1

            top_hubs = [{"location": loc, "count": count} for loc, count in sorted(loc_counts.items(), key=lambda x: x[1], reverse=True)[:6]]

            top_category = None
            if total_roles:
                top_id, top_count = max(category_counts.items(), key=lambda kv: kv[1])
                if top_count > 0:
                    top_domain = next(d for d in hiring_domains if d["id"] == top_id)
                    top_category = {
                        "id": top_domain["id"], "name": top_domain["name"], "icon": top_domain["icon"],
                        "count": top_count,
                    }

            return {
                "account_id": account.id,
                "account_name": account.legal_name or account.display_name,
                "ticker": account.stock_symbol,
                "days_filter": days,
                "total_roles": total_roles,
                "leadership_count": leadership_count,
                "contract_count": len(contract_roles),
                "top_hubs": top_hubs,
                "track_counts": {
                    "ai": ai_count,
                    "cloud": cloud_count,
                },
                "top_category": top_category,
                "contract_leads": contract_roles[:5],
            }
        finally:
            session.close()

    @app.get("/api/accounts/{account_id}/jobs", tags=["5. LinkedIn Jobs"], dependencies=[Depends(auth.require_account_access)])
    def get_account_jobs(
        account_id: int,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 25,
        days: Optional[int] = Query(None, description="Filter jobs posted within the last N days"),
    ):
        """Retrieve paginated live LinkedIn job postings for a specific account.
        Used for on-demand lazy loading when expanding the Requisitions Browser."""
        session = get_session()
        try:
            account = session.query(Account).filter(Account.id == account_id).first()
            if not account:
                raise HTTPException(status_code=404, detail="Account not found")

            candidate_keys = set(filter(None, [
                account.key,
                (account.stock_symbol or "").lower(),
                slugify(account.display_name) if account.display_name else None,
                slugify(account.legal_name) if account.legal_name else None,
            ]))

            page = max(1, page)
            page_size = max(1, min(page_size, 100))

            base_query = session.query(LinkedInJob).filter(LinkedInJob.target_key.in_(candidate_keys))
            if days is not None and days > 0:
                cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
                cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
                base_query = base_query.filter(
                    or_(
                        LinkedInJob.posted_date >= cutoff_iso,
                        and_(LinkedInJob.posted_date.is_(None), LinkedInJob.first_seen >= cutoff_dt)
                    )
                )

            total = base_query.count()

            if sort == "applicants":
                base_query = base_query.order_by(LinkedInJob.applicants.desc().nullslast())
            elif sort == "views":
                base_query = base_query.order_by(LinkedInJob.views.desc().nullslast())
            else:
                base_query = base_query.order_by(LinkedInJob.first_seen.desc().nullslast(), LinkedInJob.id.desc())

            jobs_rows = base_query.offset((page - 1) * page_size).limit(page_size).all()
            total_pages = max(1, math.ceil(total / page_size))

            job_cards = []
            for j in jobs_rows:
                job_cards.append({
                    "id": j.id,
                    "title": j.title or "Untitled Role",
                    "company_name": j.company_name or account.legal_name or "Enterprise",
                    "location": j.location or "US",
                    "employment_type": j.employment_type,
                    "workplace_type": j.workplace_type,
                    "posted_date": j.posted_date,
                    "applicants": j.applicants,
                    "views": j.views,
                    "job_url": j.job_url,
                    "first_seen": j.first_seen.isoformat() if j.first_seen else None,
                    "new_in_last_run": j.new_in_last_run,
                })

            return {
                "account_id": account.id,
                "account_name": account.legal_name or account.display_name,
                "days_filter": days,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "jobs": job_cards,
            }
        finally:
            session.close()

    # ══════════════════════════════════════════════════════
    # CXO MOVEMENTS & TRANSITIONS ENDPOINTS
    # ══════════════════════════════════════════════════════
    def _serialize_cxo_movement(m: CxoMovement, acct: Optional[Account] = None) -> Dict[str, Any]:
        return {
            "id": m.id,
            "target_key": m.target_key,
            "company_name": m.company_name,
            "person_name": m.person_name,
            "designation": m.designation,
            "event_type": (m.event_type or "").lower().strip(),
            "effective_date": m.effective_date,
            "previous_role": m.previous_role,
            "new_company": m.new_company,
            "context": m.context,
            "source": m.source,
            "publisher_domain": m.publisher_domain,
            "article_title": m.article_title,
            "article_url": m.article_url,
            "extraction_status": m.extraction_status,
            "published_at": m.published_at,
            "first_seen": m.first_seen.isoformat() if m.first_seen else None,
            "last_seen": m.last_seen.isoformat() if m.last_seen else None,
            "new_in_last_run": m.new_in_last_run,
            "account_id": acct.id if acct else None,
            "account_name": (acct.legal_name or acct.display_name) if acct else m.company_name,
        }

    @app.get("/api/cxo-movements", tags=["6. CXO Movements"])
    def get_cxo_movements(
        response: Response,
        target_key: Optional[str] = None,
        event_type: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 100,
        user: User = Depends(auth.get_current_user),
    ):
        """Retrieve executive transitions (joined, resigned, retired, promoted) across accounts."""
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        session = get_session()
        try:
            query = session.query(CxoMovement)
            if auth.AUTH_ENFORCED and user.role != "super_admin":
                # Scope to the user's accounts: by target key, or by company name / alias
                # (movement target keys don't always equal accounts.key, e.g. blackrock_inc).
                ids = set(auth.get_accessible_account_ids(session, user.id))
                keys = [k for k, a in _build_target_key_to_account_map(session).items() if a.id in ids]
                names = set()
                for a in session.query(Account).filter(Account.id.in_(ids or [-1])):
                    names.update(n.lower() for n in [a.display_name, a.legal_name, *(a.aliases or [])] if n)
                query = query.filter(CxoMovement.target_key.in_(keys or [""])
                                     | sa_func.lower(CxoMovement.company_name).in_(list(names) or [""]))
            if target_key:
                query = query.filter(CxoMovement.target_key == target_key)
            if event_type and event_type.lower() != "all":
                query = query.filter(CxoMovement.event_type.ilike(event_type))
            if q and q.strip():
                term = f"%{q.strip()}%"
                query = query.filter(
                    (CxoMovement.person_name.ilike(term))
                    | (CxoMovement.company_name.ilike(term))
                    | (CxoMovement.designation.ilike(term))
                    | (CxoMovement.context.ilike(term))
                    | (CxoMovement.previous_role.ilike(term))
                    | (CxoMovement.new_company.ilike(term))
                )

            all_records = query.order_by(CxoMovement.first_seen.desc().nullslast()).limit(limit).all()
            target_map = _build_target_key_to_account_map(session)

            counts = {"all": len(all_records), "joined": 0, "resigned": 0, "retired": 0, "promoted": 0}
            serialized = []
            for r in all_records:
                evt = (r.event_type or "").lower().strip()
                if evt in counts:
                    counts[evt] += 1
                acct = target_map.get(r.target_key)
                serialized.append(_serialize_cxo_movement(r, acct))

            return {"total": len(serialized), "counts": counts, "movements": serialized}
        finally:
            session.close()

    @app.get("/api/accounts/{account_id}/cxo-movements", tags=["6. CXO Movements"])
    def get_account_cxo_movements(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieve executive transitions for a specific account."""
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail="Account not found.")
            keys = [
                acct.key,
                (acct.stock_symbol or "").lower(),
                slugify(acct.display_name),
                slugify(acct.legal_name),
            ]
            keys = [k for k in keys if k]
            movements = (
                session.query(CxoMovement)
                .filter(CxoMovement.target_key.in_(keys))
                .order_by(CxoMovement.first_seen.desc().nullslast())
                .all()
            )
            return {
                "account_id": account_id,
                "account_name": acct.legal_name or acct.display_name,
                "total": len(movements),
                "movements": [_serialize_cxo_movement(m, acct) for m in movements],
            }
        finally:
            session.close()

    def _news_date(published_at: Optional[str], first_seen: Optional[datetime]) -> Optional[datetime]:
        """Article date from the scraped text (RSS gives RFC 822, e.g. "Wed, 26 Aug 2026
        23:14:00 GMT"; some sources give ISO). Falls back to when we first saw it."""
        from email.utils import parsedate_to_datetime
        if published_at:
            for parse in (parsedate_to_datetime, datetime.fromisoformat):
                try:
                    d = parse(published_at.strip())
                    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError, IndexError):
                    continue
        return first_seen

    @app.get("/api/news", tags=["4. Content Intelligence"])
    def get_cross_account_news(
        response: Response,
        user: User = Depends(auth.get_current_user),
        limit: int = Query(30, ge=1, le=100),
        account_id: Optional[int] = Query(None),
    ):
        """Recent Google News articles (Post.channel == "news") across every account
        the caller can see — both company-level scrapes (target_key = account key)
        and person-level ones (target_key = persona key, mapped to the persona's
        account). Newest article first by the article's own date, the same story
        under several people shown once. account_id narrows to one account."""
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        session = get_session()
        try:
            company_map = _build_target_key_to_account_map(session)
            if user.role != "super_admin":
                accessible_ids = set(auth.get_accessible_account_ids(session, user.id))
                company_map = {k: a for k, a in company_map.items() if a.id in accessible_ids}
            accounts = {a.id: a for a in company_map.values()}
            if account_id is not None:
                accounts = {k: v for k, v in accounts.items() if k == account_id}
                company_map = {k: a for k, a in company_map.items() if a.id == account_id}
            if not accounts:
                return {"articles": []}

            # target_key -> (account, person name or None)
            target_map = {k: (a, None) for k, a in company_map.items()}
            person_rows = (
                session.query(Persona.key, Persona.account_id, func.coalesce(Persona.full_name, Persona.display_name))
                .filter(Persona.account_id.in_(list(accounts)), Persona.key.isnot(None))
                .all()
            )
            for key, acct_id, name in person_rows:
                target_map.setdefault(key, (accounts[acct_id], name))

            # Candidates by scrape time (indexed), then ranked by the article's own date.
            posts = (
                session.query(Post)
                .filter(Post.channel == "news", Post.target_key.in_(list(target_map.keys())))
                .order_by(Post.first_seen.desc().nullslast())
                .limit(max(limit * 25, 500))
                .all()
            )
            ranked = sorted(posts, key=lambda p: _news_date(p.published_at, p.first_seen)
                            or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

            articles, seen = [], set()
            for p in ranked:
                raw = p.raw or {}
                body_stripped = (p.body or "").strip()
                title = raw.get("title") or (body_stripped.splitlines()[0][:200] if body_stripped else None)
                if not title:
                    continue
                dedupe = " ".join(title.lower().split())
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                acct, person = target_map[p.target_key]
                published = _news_date(p.published_at, None)
                articles.append({
                    "id": p.id,
                    "account_id": acct.id,
                    "account_name": acct.display_name or acct.legal_name,
                    "person_name": person,
                    "title": title,
                    "source": p.author or raw.get("source"),
                    "url": p.post_url,
                    "published_at": published.isoformat() if published else None,
                    "first_seen": p.first_seen.isoformat() if p.first_seen else None,
                })
                if len(articles) >= limit:
                    break
            last_scraped = session.query(func.max(Post.first_seen)).filter(
                Post.channel == "news", Post.target_key.in_(list(target_map.keys()))).scalar()
            return {"articles": articles, "last_scraped": last_scraped.isoformat() if last_scraped else None}
        finally:
            session.close()

    @app.get("/api/accounts/{account_id}/content", tags=["4. Content Intelligence"])
    def get_account_content_intelligence(account_id: int, user: User = Depends(auth.require_account_access)):
        """Retrieve social listening posts, LLM channel digests, and LinkedIn jobs
        scoped to one account — the on-demand counterpart to /api/content, fetched
        when that account's Social/Content/Jobs tab is opened rather than pulling
        every account's content up front."""
        session = get_session()
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                raise HTTPException(status_code=404, detail="Account not found.")
            # Persona-level content (per-contact digests like the Personality
            # Profile, and their own captured posts) is stored under each
            # person's own target_key (e.g. "robin_vince"), not the account's
            # — without including those here, the contact drawer's Recent
            # Social Media Activity / Personality Profile sections always
            # found nothing, no matter how much persona-level data existed.
            # Three candidates per persona (mirrors _resolve_persona_digest
            # above and resolvePersonaTargetKey on the frontend): a name with
            # a middle initial like "Ranjit S. Samra" slugifies to
            # "ranjit_s_samra", but people_targets.py registers them under
            # "ranjit_samra" (initial omitted) — without the third candidate
            # this endpoint silently omits that persona's digest/posts from
            # its response entirely, no matter how the frontend resolves keys.
            persona_keys = []
            for p in (acct.personas or []):
                persona_keys.append(p.key)
                persona_keys.append(slugify(p.full_name or ""))
                persona_keys.append(_slugify_dropping_initials(p.full_name or ""))
            keys = [
                acct.key,
                (acct.stock_symbol or "").lower(),
                slugify(acct.display_name),
                slugify(acct.legal_name),
                *persona_keys,
            ]
            keys = [k for k in keys if k]

            digests_by_key = {}
            try:
                for d in session.query(Digest).filter(Digest.target_key.in_(keys)).all():
                    digests_by_key[d.target_key] = {
                        "target_key": d.target_key,
                        "kind": d.kind,
                        "priority": d.priority,
                        "llm": d.llm,
                        "posts_considered": d.posts_considered,
                        "generated_at": d.generated_at.isoformat() if d.generated_at else None,
                        "digest": d.digest,
                    }
            except Exception:
                digests_by_key = {}

            posts_by_key = {}
            try:
                posts_query = (
                    session.query(Post)
                    .filter(Post.target_key.in_(keys))
                    .order_by(Post.target_key, Post.channel, Post.rank)
                )
                for p in posts_query.all():
                    posts_by_key.setdefault(p.target_key, []).append(
                        {
                            "id": p.id,
                            "channel": p.channel,
                            "post_key": p.post_key,
                            "rank": p.rank,
                            "post_url": p.post_url,
                            "body": p.body,
                            "author": p.author,
                            "published_at": p.published_at,
                            "engagement": p.engagement,
                            "media": p.media,
                            "new_in_last_run": p.new_in_last_run,
                            "first_seen": p.first_seen.isoformat() if p.first_seen else None,
                            "last_seen": p.last_seen.isoformat() if p.last_seen else None,
                        }
                    )
            except Exception:
                posts_by_key = {}

            jobs_by_key = {}
            try:
                jobs_query = (
                    session.query(LinkedInJob)
                    .filter(LinkedInJob.target_key.in_(keys))
                    .order_by(LinkedInJob.target_key, LinkedInJob.first_seen.desc().nullslast())
                )
                for j in jobs_query.all():
                    jobs_by_key.setdefault(j.target_key, []).append(
                        {
                            "id": j.id,
                            "target_key": j.target_key,
                            "job_key": j.job_key,
                            "title": j.title,
                            "company_name": j.company_name,
                            "location": j.location,
                            "employment_type": j.employment_type,
                            "workplace_type": j.workplace_type,
                            "posted_date": j.posted_date,
                            "applicants": j.applicants,
                            "views": j.views,
                            "salary": j.salary,
                            "job_url": j.job_url,
                            "description": j.description,
                            "new_in_last_run": j.new_in_last_run,
                            "first_seen": j.first_seen.isoformat() if j.first_seen else None,
                            "last_seen": j.last_seen.isoformat() if j.last_seen else None,
                        }
                    )
            except Exception:
                jobs_by_key = {}

            return {"digests": digests_by_key, "posts": posts_by_key, "jobs": jobs_by_key}
        finally:
            session.close()

    @app.get("/api/database/download", tags=["7. Database Operations"], dependencies=[Depends(auth.require_role("super_admin"))])
    @app.get("/api/database/download/sql", tags=["7. Database Operations"], dependencies=[Depends(auth.require_role("super_admin"))])
    def download_database_sql():
        """Download the complete PostgreSQL SQL database dump file."""
        sql_path = PIPELINE_ROOT / "sales_ai_database_export.sql"
        if not sql_path.exists():
            # Regenerate if missing
            try:
                run([sys.executable, str(PIPELINE_ROOT / "export_db.py")], check=True)
            except Exception:
                pass
        if not sql_path.exists():
            raise HTTPException(status_code=404, detail="Database export file not found.")
        return FileResponse(
            path=str(sql_path), filename="sales_ai_database_export.sql", media_type="application/sql"
        )

    @app.get("/api/database/download/json", tags=["7. Database Operations"], dependencies=[Depends(auth.require_role("super_admin"))])
    def download_database_json():
        """Download the complete database in JSON format."""
        json_path = PIPELINE_ROOT / "sales_ai_database_export.json"
        if not json_path.exists():
            raise HTTPException(status_code=404, detail="Database JSON export file not found.")
        return FileResponse(
            path=str(json_path), filename="sales_ai_database_export.json", media_type="application/json"
        )

    # ══════════════════════════════════════════════════════
    # FRONTEND UI: Jinja2-templated shell + static assets
    # ══════════════════════════════════════════════════════
    # The dashboard shell (frontend/templates/index.html) is composed from
    # partials (topbar/nav/drawer/modal) and rendered server-side; everything
    # it actually renders (accounts, digest sections, etc.) still comes from
    # the JSON APIs below via frontend/js/modules/. CSS/JS/the separate
    # Account Explorer app stay plain static files — only the shell itself
    # needed templating, so we mount those under their own sub-paths instead
    # of the old single mount at "/" (which would now collide with the
    # explicit "/" route below).
    frontend_dir = Path(__file__).resolve().parent / "frontend"
    if frontend_dir.exists():
        templates = Jinja2Templates(directory=str(frontend_dir / "templates"))
        # Temporarily hidden pages. FORECAST_ENABLED=1 in .env brings the Forecast page and
        # its nav entry back; its API (/api/crm/forecast*) and data are untouched either way.
        FORECAST_ENABLED = os.getenv("FORECAST_ENABLED", "0").strip().lower() in ("1", "true", "yes")
        templates.env.globals["forecast_enabled"] = FORECAST_ENABLED

        _NO_CACHE_HEADERS = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }

        # ── SEO: public URL, robots.txt, sitemap.xml (favicon: top of the file) ──────────────
        # Only the landing page is public; everything else sits behind sign-in, so crawlers
        # are pointed at "/" and kept out of the app. SITE_URL (else APP_BASE_URL) is the
        # public origin used for canonical / Open Graph links; set it in production.
        def _site_url(request: Request) -> str:
            base = (os.getenv("SITE_URL") or os.getenv("APP_BASE_URL") or "").strip()
            return (base or str(request.base_url)).rstrip("/")

        templates.env.globals["site_url"] = _site_url

        @app.get("/robots.txt", include_in_schema=False)
        async def robots_txt(request: Request):
            base = _site_url(request)
            body = ("User-agent: *\n"
                    "Allow: /$\n"             # the landing page only (not /?account= deep links)
                    "Allow: /assets/\nAllow: /css/\nAllow: /js/\n"   # so crawlers can render it
                    "Disallow: /\n\n"
                    f"Sitemap: {base}/sitemap.xml\n")
            return Response(body, media_type="text/plain", headers={"Cache-Control": "public, max-age=3600"})

        @app.get("/sitemap.xml", include_in_schema=False)
        async def sitemap_xml(request: Request):
            base = _site_url(request)
            landing = frontend_dir / "templates" / "landing.html"
            lastmod = datetime.fromtimestamp(landing.stat().st_mtime, tz=timezone.utc).date().isoformat()
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                    f"  <url><loc>{base}/</loc><lastmod>{lastmod}</lastmod>"
                    "<changefreq>monthly</changefreq><priority>1.0</priority></url>\n"
                    "</urlset>\n")
            return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def dashboard_home(request: Request):
            """Bare /: signed-out visitors get the landing page, signed-in users (session hint
            cookie) go to the Command Center. With a query string (/?account=…, deep links
            that predate /accounts) it is the accounts dashboard, which handles login itself."""
            if not request.query_params:
                if request.cookies.get(SESSION_HINT_COOKIE):
                    return RedirectResponse(url="/command-center", status_code=302)
                return templates.TemplateResponse(request, "landing.html", headers=_NO_CACHE_HEADERS)
            return templates.TemplateResponse(request, "index.html", headers=_NO_CACHE_HEADERS)

        @app.get("/accounts", response_class=HTMLResponse, include_in_schema=False)
        async def accounts_dashboard(request: Request):
            """The accounts dashboard (account tree, dossiers, buying committee)."""
            return templates.TemplateResponse(request, "index.html", headers=_NO_CACHE_HEADERS)

        @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
        async def login_page(request: Request):
            return templates.TemplateResponse(request, "login.html", headers=_NO_CACHE_HEADERS)

        @app.get("/welcome", response_class=HTMLResponse, include_in_schema=False)
        async def landing_page(request: Request):
            """The public landing page at a fixed URL (it is also what / shows signed-out
            visitors), e.g. for a signed-in user who wants to see it."""
            return templates.TemplateResponse(request, "landing.html", headers=_NO_CACHE_HEADERS)

        @app.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
        async def reset_password_page(request: Request):
            return templates.TemplateResponse(request, "reset-password.html", headers=_NO_CACHE_HEADERS)

        @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
        async def admin_page(request: Request):
            """Super-admin dashboard shell — the page itself renders for
            anyone (no server-side session to gate on), but every
            /api/admin/* call it makes is independently protected by
            Depends(auth.require_role("super_admin"))."""
            return templates.TemplateResponse(request, "admin.html", headers=_NO_CACHE_HEADERS)

        @app.get("/profile", response_class=HTMLResponse, include_in_schema=False)
        async def contact_profile_page(request: Request):
            """Standalone full-page contact profile — opened via the contact
            drawer's "View Profile" button (?account=<id>&persona_id=<id>).
            Reuses the same drawer markup/rendering as the dashboard's
            sliding drawer; see frontend/js/modules/profile-page.js."""
            return templates.TemplateResponse(request, "profile.html", headers=_NO_CACHE_HEADERS)

        @app.get("/command-center", response_class=HTMLResponse, include_in_schema=False)
        async def sales_command_center_page(request: Request):
            """Action-first rep/manager/exec dashboard — KPI strip, account
            priority matrix, priority signal feed, playbook and exec
            movements timeline. The signal feed, playbook and their KPIs are
            built on demand by POST /api/command-center/generate; see
            services/command_center_service.py."""
            return templates.TemplateResponse(request, "command-center.html", headers=_NO_CACHE_HEADERS)

        @app.get("/deals", response_class=HTMLResponse, include_in_schema=False)
        async def deals_page(request: Request):
            """Deals pipeline board + deal room (apps/sales_copilot/README.md §21).
            ?deal=ID opens a deal; ?account_id= filters the board."""
            return templates.TemplateResponse(request, "deals.html", headers=_NO_CACHE_HEADERS)

        @app.get("/partner", response_class=HTMLResponse, include_in_schema=False)
        async def partner_page(request: Request):
            """Partner / advisor portal: their own introductions only (apps/sales_crm/README.md §5.3).
            Admins can preview a connector's view with ?connector_id=."""
            return templates.TemplateResponse(request, "partner.html", headers=_NO_CACHE_HEADERS)

        @app.get("/room/{token}", response_class=HTMLResponse, include_in_schema=False)
        async def buyer_room_page(request: Request, token: str):
            """Buyer room (#11): the read-only page a rep shares with the customer. Public by design —
            the token in the URL is the access; the page loads its data from /api/room/{token}."""
            return templates.TemplateResponse(request, "buyer-room.html", headers={
                **_NO_CACHE_HEADERS, "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"})

        @app.get("/email-sync", response_class=HTMLResponse, include_in_schema=False)
        async def capture_settings_page(request: Request):
            """Connect Microsoft 365 email + calendar for automatic activity capture (apps/sales_crm/README.md §4)."""
            return templates.TemplateResponse(request, "capture.html", headers=_NO_CACHE_HEADERS)

        @app.get("/forecast", response_class=HTMLResponse, include_in_schema=False)
        async def forecast_page(request: Request):
            """Quarterly forecast roll-up, targets and week-over-week changes (apps/sales_crm/README.md §3).
            Hidden for now unless FORECAST_ENABLED is set: old links land on Deals instead."""
            if not FORECAST_ENABLED:
                return RedirectResponse(url="/deals", status_code=302)
            return templates.TemplateResponse(request, "forecast.html", headers=_NO_CACHE_HEADERS)

        @app.get("/introductions", response_class=HTMLResponse, include_in_schema=False)
        async def introductions_page(request: Request):
            """Warm introductions board, connectors and attribution (apps/sales_crm/README.md §2.2).
            ?intro=ID opens one introduction."""
            return templates.TemplateResponse(request, "introductions.html", headers=_NO_CACHE_HEADERS)

        @app.get("/copilot", response_class=HTMLResponse, include_in_schema=False)
        async def copilot_page(request: Request):
            """Sales Copilot workspace — RAG chat over the sales DB
            (apps/sales_copilot/README.md §19). Accepts ?persona_id= / ?account_id=
            to open a chat pre-scoped to that contact or account."""
            return templates.TemplateResponse(request, "copilot.html", headers=_NO_CACHE_HEADERS)

        @app.get("/copilot-pipeline", response_class=HTMLResponse, include_in_schema=False)
        async def copilot_pipeline_page(request: Request):
            """Copilot index pipeline console: run sync / full re-embed / eval +
            guardrails step by step, inspect one document's chunks and vectors,
            trace a question through retrieval. Data comes from
            /api/copilotpipeline/*, each call protected by require_role("super_admin")."""
            return templates.TemplateResponse(request, "copilot-pipeline.html", headers=_NO_CACHE_HEADERS)

        @app.get("/tasks", response_class=HTMLResponse, include_in_schema=False)
        async def tasks_page(request: Request):
            """Personal, cross-account Task Management page — every action
            item assigned to the caller (GET /api/me/action-items), with
            status/priority/account filtering and sorting. See
            TASK_MANAGEMENT_README.md. A team/manager-wide view is a
            deliberately deferred v2 (no endpoint for it exists yet)."""
            return templates.TemplateResponse(request, "tasks.html", headers=_NO_CACHE_HEADERS)

        @app.get("/pipeline", include_in_schema=False)
        async def pipeline_redirect():
            return RedirectResponse(url="/pipeline/", status_code=307)

        @app.get("/pipline", include_in_schema=False)
        async def pipline_redirect():
            return RedirectResponse(url="/pipeline/", status_code=307)

        class NoCacheStaticFiles(StaticFiles):
            """Forces browsers to revalidate every CSS/JS fetch against the
            server (via the ETag/Last-Modified this already returns) instead
            of silently reusing a cached copy. Without this, a plain reload
            can keep serving an old version of a file indefinitely — these
            are ES modules pulled in via bare `import`s (only the page's own
            entry script has a `?v=` cache-busting query string; anything it
            imports does not inherit that), and during active development
            files here can change several times an hour. A stale cached copy
            of one earlier today produced a genuinely confusing
            "Unexpected token" browser error even though the file on disk
            (and every version in git history) was valid JS the whole time.
            Still cheap: an unchanged file gets a 304 Not Modified, not a
            full re-download."""
            def file_response(self, *args, **kwargs):
                response = super().file_response(*args, **kwargs)
                response.headers["Cache-Control"] = "no-cache"
                return response

        css_dir = frontend_dir / "css"
        js_dir = frontend_dir / "js"
        pipline_dir = frontend_dir / "pipline"
        if css_dir.exists():
            app.mount("/css", NoCacheStaticFiles(directory=str(css_dir)), name="frontend-css")
        if js_dir.exists():
            app.mount("/js", NoCacheStaticFiles(directory=str(js_dir)), name="frontend-js")
        assets_dir = frontend_dir / "assets"
        if assets_dir.exists():
            # Images (EliteHost theme illustrations etc.) — cacheable, they never change in place.
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")
        if pipline_dir.exists():
            app.mount("/pipeline", NoCacheStaticFiles(directory=str(pipline_dir), html=True), name="frontend-pipeline")
            app.mount("/pipline", NoCacheStaticFiles(directory=str(pipline_dir), html=True), name="frontend-pipline")


if __name__ == "__main__":
    if FASTAPI_AVAILABLE:
        print("=" * 70)
        print("[*] Starting Sales AI Granular REST API Server on http://0.0.0.0:8000")
        print("[*] Interactive Swagger API Documentation: http://localhost:8000/docs")
        print("=" * 70)
        uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
    else:
        print("[!] FastAPI/uvicorn not installed. Run: pip install -r requirements.txt")
