"""Account owner / business line and hand-entered contacts (apps/sales_crm/README.md §B).

  GET    /api/crm/accounts/{id}/crm        owner, primary business line, who can be assigned
  PATCH  /api/crm/accounts/{id}            set / clear owner and primary business line (notifies the new owner)
  POST   /api/crm/contacts                 add a contact by hand (source = 'manual')
  PATCH  /api/crm/contacts/{id}            edit name / title / work email / phone / LinkedIn / LOB
  DELETE /api/crm/contacts/{id}            only hand-entered contacts, by their creator or an admin

Contact rules: work email only (free-mail / personal addresses are refused — personal contact data is
never stored in the contact's work fields); one contact per work email per account.
"""

import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from apps.sales_copilot.privacy import FREEMAIL
from apps.sales_crm import notify, permissions
from db.connection import get_session
from db.models.user import User

router = APIRouter(prefix="/api/crm", tags=["CRM records"])
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _scope_check(s, user, account_id: int) -> None:
    if account_id not in auth.account_scope(s, user):
        raise HTTPException(404, "Account not found.")


def assignable_users(s, account_id: int) -> List[Dict[str, Any]]:
    """Active StradIT users who can open this account (grant, team or admin)."""
    out = []
    for uid, name, role in s.execute(text("""SELECT id, coalesce(full_name, email), role FROM users
                                             WHERE is_active AND role IN ('user','sales_manager','super_admin') ORDER BY 2""")):
        if role == "super_admin" or account_id in auth.get_accessible_account_ids(s, uid):
            out.append({"id": uid, "name": name, "role": role})
    return out


# ── Account owner + business line ─────────────────────────────────────────────


@router.get("/accounts/{account_id}/crm")
def account_crm(account_id: int, user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")),
                s=Depends(_session)):
    _scope_check(s, user, account_id)
    r = s.execute(text("""SELECT a.id, a.display_name, a.owner_user_id, coalesce(u.full_name, u.email) AS owner_name,
                                 a.primary_business_line_id, bl.name AS business_line_name
                          FROM accounts a LEFT JOIN users u ON u.id = a.owner_user_id
                          LEFT JOIN business_lines bl ON bl.id = a.primary_business_line_id WHERE a.id = :a"""),
                  {"a": account_id}).mappings().fetchone()
    if not r:
        raise HTTPException(404, "Account not found.")
    return {**dict(r), "assignable": assignable_users(s, account_id),
            "business_lines": [dict(x) for x in s.execute(text("SELECT id, name FROM business_lines WHERE active ORDER BY sort, name")).mappings()],
            "can_edit": not (auth.AUTH_ENFORCED and user.role in auth.READ_ONLY_ROLES)}


class AccountCrmIn(BaseModel):
    owner_user_id: Optional[int] = None
    clear_owner: bool = False
    primary_business_line_id: Optional[int] = None
    clear_business_line: bool = False


@router.patch("/accounts/{account_id}")
def update_account_crm(account_id: int, body: AccountCrmIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    _scope_check(s, user, account_id)
    cur = s.execute(text("SELECT display_name, owner_user_id FROM accounts WHERE id = :a"), {"a": account_id}).fetchone()
    details: Dict[str, Any] = {}
    if body.clear_owner or body.owner_user_id is not None:
        new = None if body.clear_owner else body.owner_user_id
        if new is not None and new not in {u["id"] for u in assignable_users(s, account_id)}:
            raise HTTPException(400, "That person can't be the owner — they need access to this account first.")
        s.execute(text("UPDATE accounts SET owner_user_id = :o WHERE id = :a"), {"o": new, "a": account_id})
        details["owner_user_id"] = {"old": cur[1], "new": new}
        if new and new != cur[1] and new != user.id:
            notify.enqueue(s, new, "account_assigned", f"You now own {cur[0]}",
                           [f"{user.full_name or user.email} made you the owner of {cur[0]}.",
                            "Partner submissions for this account will come to you for triage."],
                           link=f"/?account={account_id}", dedupe_key=f"account_owner:{account_id}:{new}:{cur[1]}")
    if body.clear_business_line or body.primary_business_line_id is not None:
        bl = None if body.clear_business_line else body.primary_business_line_id
        if bl is not None and not s.execute(text("SELECT 1 FROM business_lines WHERE id = :b AND active"), {"b": bl}).scalar():
            raise HTTPException(400, "Unknown or inactive business line.")
        s.execute(text("UPDATE accounts SET primary_business_line_id = :b WHERE id = :a"), {"b": bl, "a": account_id})
        details["primary_business_line_id"] = bl
    s.commit()
    if details:
        auth.log_audit(s, user.id, "account_crm_updated", details={"account_id": account_id, **details})
    return account_crm(account_id, user, s)


# ── Contacts entered by hand ──────────────────────────────────────────────────


def _clean_email(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip().lower()
    if not v:
        return None
    if not EMAIL_RE.match(v):
        raise HTTPException(400, "That doesn't look like an email address.")
    if v.rsplit("@", 1)[1] in FREEMAIL:
        raise HTTPException(400, "Use the contact's work email — personal addresses (Gmail, Outlook.com …) aren't stored.")
    return v


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:120] or "contact"


class ContactIn(BaseModel):
    account_id: int
    full_name: str = Field(..., min_length=2, max_length=200)
    title: Optional[str] = Field(None, max_length=250)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=60)
    linkedin_url: Optional[str] = Field(None, max_length=500)
    lob_id: Optional[int] = None


class ContactPatch(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=200)
    title: Optional[str] = Field(None, max_length=250)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=60)
    linkedin_url: Optional[str] = Field(None, max_length=500)
    lob_id: Optional[int] = None


def _check_lob(s, lob_id: Optional[int], account_id: int) -> None:
    if lob_id is not None and s.execute(text("SELECT account_id FROM lobs WHERE id = :l"), {"l": lob_id}).scalar() != account_id:
        raise HTTPException(400, "That line of business belongs to a different account.")


def _check_linkedin(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    if v and not re.match(r"^https?://([a-z]+\.)?linkedin\.com/", v, re.I):
        raise HTTPException(400, "LinkedIn URL should start with https://www.linkedin.com/")
    return v or None


def _contact_out(s, pid: int) -> Dict[str, Any]:
    from apps.sales_copilot import privacy
    r = s.execute(text(f"""SELECT p.id, p.account_id, coalesce(p.full_name, p.display_name) AS name, p.title,
                                  {privacy.SAFE_EMAIL_SQL} AS email, {privacy.SAFE_PHONE_SQL} AS phone, p.linkedin_url, p.lob_id,
                                  p.source, p.created_by_user_id FROM personas p WHERE p.id = :p"""), {"p": pid}).mappings().fetchone()
    return dict(r)


@router.post("/contacts")
def add_contact(body: ContactIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    _scope_check(s, user, body.account_id)
    email = _clean_email(body.email)
    name = re.sub(r"\s+", " ", body.full_name).strip()
    _check_lob(s, body.lob_id, body.account_id)
    dup = s.execute(text("""SELECT id FROM personas WHERE account_id = :a AND (
                              (CAST(:e AS text) IS NOT NULL AND lower(email) = :e) OR lower(coalesce(full_name, display_name)) = lower(:n))
                            LIMIT 1"""), {"a": body.account_id, "e": email, "n": name}).scalar()
    if dup:
        raise HTTPException(409, f"This contact already exists at the account (id {dup}).")
    parts = name.split(" ")
    pid = s.execute(text("""
        INSERT INTO personas (account_id, lob_id, key, display_name, full_name, first_name, last_name, title, email, phone,
                              linkedin_url, source, created_by_user_id, is_manually_verified, manually_verified_at)
        VALUES (:a, :l, :k, :n, :n, :f, :la, :t, :e, :ph, :li, 'manual', :u, true, now()) RETURNING id"""),
        {"a": body.account_id, "l": body.lob_id, "k": _slug(name), "n": name, "f": parts[0],
         "la": parts[-1] if len(parts) > 1 else None, "t": (body.title or "").strip() or None, "e": email,
         "ph": (body.phone or "").strip() or None, "li": _check_linkedin(body.linkedin_url), "u": user.id}).scalar()
    s.commit()
    auth.log_audit(s, user.id, "contact_created", details={"persona_id": pid, "account_id": body.account_id})
    return _contact_out(s, pid)


def _load_contact(s, user, pid: int) -> Dict[str, Any]:
    r = s.execute(text("SELECT id, account_id, source, created_by_user_id FROM personas WHERE id = :p"), {"p": pid}).mappings().fetchone()
    if not r or r["account_id"] not in auth.account_scope(s, user):
        raise HTTPException(404, "Contact not found.")
    return dict(r)


@router.patch("/contacts/{persona_id}")
def edit_contact(persona_id: int, body: ContactPatch, user: User = Depends(permissions.require_write), s=Depends(_session)):
    c = _load_contact(s, user, persona_id)
    ch = body.model_dump(exclude_unset=True)
    if "email" in ch:
        ch["email"] = _clean_email(ch["email"])
    if "linkedin_url" in ch:
        ch["linkedin_url"] = _check_linkedin(ch["linkedin_url"])
    if "lob_id" in ch:
        _check_lob(s, ch["lob_id"], c["account_id"])
    if "full_name" in ch:
        ch["full_name"] = re.sub(r"\s+", " ", ch["full_name"]).strip()
        ch["display_name"] = ch["full_name"]
    if not ch:
        raise HTTPException(400, "Nothing to update.")
    s.execute(text("UPDATE personas SET " + ", ".join(f"{k} = :{k}" for k in ch) +
                   ", is_manually_verified = true, manually_verified_at = now() WHERE id = :p"), {**ch, "p": persona_id})
    s.commit()
    auth.log_audit(s, user.id, "contact_updated", details={"persona_id": persona_id, "fields": sorted(ch)})
    return _contact_out(s, persona_id)


@router.delete("/contacts/{persona_id}")
def delete_contact(persona_id: int, user: User = Depends(permissions.require_write), s=Depends(_session)):
    c = _load_contact(s, user, persona_id)
    if c["source"] != "manual":
        raise HTTPException(400, "Only contacts added by hand can be deleted here; data-pipeline contacts are managed by the pipeline.")
    if not (permissions.is_admin(user) or c["created_by_user_id"] == user.id):
        raise HTTPException(403, "Only the person who added this contact (or an admin) can delete it.")
    s.execute(text("DELETE FROM personas WHERE id = :p"), {"p": persona_id})
    s.commit()
    auth.log_audit(s, user.id, "contact_deleted", details={"persona_id": persona_id})
    return {"ok": True}
