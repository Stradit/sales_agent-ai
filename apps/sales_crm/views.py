"""Saved list views (#6): each user's named filter + sort combinations, per page. Personal only -
no one else sees or changes them (viewers may save their own too; permissions.VIEWER_WRITE_ALLOWED)."""
import json
from typing import Any, Dict, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from db.connection import get_session
from db.models.user import User

router = APIRouter(prefix="/api/crm/views", tags=["CRM saved views"])
MAX_PER_PAGE = 20
Page = Literal["deals", "introductions"]


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


class ViewIn(BaseModel):
    page: Page
    name: str = Field(..., min_length=1, max_length=60)
    state: Dict[str, Any] = {}


@router.get("")
def list_views(page: Page, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    rows = s.execute(text("SELECT id, name, state FROM crm_saved_views WHERE user_id = :u AND page = :p ORDER BY name"),
                     {"u": user.id, "p": page}).mappings().all()
    return {"views": [dict(r) for r in rows]}


@router.post("")
def save_view(body: ViewIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Create, or overwrite the user's view with the same name."""
    state = json.dumps(body.state)
    if len(state) > 4000:
        raise HTTPException(400, "That view has too many settings to save.")
    n = s.execute(text("SELECT count(*) FROM crm_saved_views WHERE user_id = :u AND page = :p AND name <> :n"),
                  {"u": user.id, "p": body.page, "n": body.name.strip()}).scalar()
    if n >= MAX_PER_PAGE:
        raise HTTPException(400, f"You can keep up to {MAX_PER_PAGE} saved views per page — delete one first.")
    row = s.execute(text("""INSERT INTO crm_saved_views (user_id, page, name, state) VALUES (:u, :p, :n, CAST(:st AS jsonb))
                            ON CONFLICT (user_id, page, name) DO UPDATE SET state = EXCLUDED.state
                            RETURNING id, name, state"""),
                    {"u": user.id, "p": body.page, "n": body.name.strip(), "st": state}).mappings().fetchone()
    s.commit()
    return dict(row)


@router.delete("/{view_id}")
def delete_view(view_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    gone = s.execute(text("DELETE FROM crm_saved_views WHERE id = :i AND user_id = :u"), {"i": view_id, "u": user.id}).rowcount
    s.commit()
    if not gone:
        raise HTTPException(404, "View not found.")
    return {"ok": True}
