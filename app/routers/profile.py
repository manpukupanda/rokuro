import sqlite3
from contextlib import closing

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from auth import current_user, verify_password, hash_password
from database import db_connect, now_iso
from flash import base_context, set_flash
from web import templates

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("")
def profile_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    return templates.TemplateResponse(request, "profile.html", base_context(request, user=user))


@router.post("/display-name")
def update_display_name(
    request: Request,
    display_name: str = Form(...),
    user: sqlite3.Row = Depends(current_user),
):
    display_name = display_name.strip()
    if not display_name:
        set_flash(request, "danger", "Display name is required.")
        return RedirectResponse("/profile", status_code=303)

    with closing(db_connect()) as conn:
        conn.execute(
            "UPDATE users SET display_name = ?, updated_at = ? WHERE account = ?",
            (display_name, now_iso(), user["account"]),
        )
        conn.commit()
    set_flash(request, "success", "Display name updated.")
    return RedirectResponse("/profile", status_code=303)


@router.post("/password")
def update_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: sqlite3.Row = Depends(current_user),
):
    if len(new_password) < 8:
        set_flash(request, "danger", "New password must be at least 8 characters.")
        return RedirectResponse("/profile", status_code=303)

    if not verify_password(current_password, user["password_hash"]):
        set_flash(request, "danger", "Current password is incorrect.")
        return RedirectResponse("/profile", status_code=303)

    with closing(db_connect()) as conn:
        conn.execute(
            """
            UPDATE users
               SET password_hash = ?,
                   session_version = session_version + 1,
                   updated_at = ?
             WHERE account = ?
            """,
            (hash_password(new_password), now_iso(), user["account"]),
        )
        conn.commit()
    request.session.clear()
    set_flash(request, "success", "Password updated. Please login again.")
    return RedirectResponse("/login", status_code=303)
