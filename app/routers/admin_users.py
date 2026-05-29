import sqlite3
from contextlib import closing

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from auth import current_admin, hash_password
from database import db_connect, now_iso
from flash import base_context, set_flash
from web import templates

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


@router.get("")
def admin_users_page(request: Request, _: sqlite3.Row = Depends(current_admin)):
    with closing(db_connect()) as conn:
        users = conn.execute(
            "SELECT account, display_name, role, is_active, is_protected, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    return templates.TemplateResponse(request, "admin_users.html", base_context(request, users=users))


@router.post("/create")
def admin_user_create(
    request: Request,
    account: str = Form(...),
    display_name: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    _: sqlite3.Row = Depends(current_admin),
):
    account = account.strip()
    display_name = display_name.strip()
    if not account or not display_name:
        set_flash(request, "danger", "Account and display name are required.")
        return RedirectResponse("/admin/users", status_code=303)
    if role not in {"admin", "user"}:
        set_flash(request, "danger", "Invalid role.")
        return RedirectResponse("/admin/users", status_code=303)
    if len(password) < 8:
        set_flash(request, "danger", "Password must be at least 8 characters.")
        return RedirectResponse("/admin/users", status_code=303)

    ts = now_iso()
    try:
        with closing(db_connect()) as conn:
            conn.execute(
                """
                INSERT INTO users (
                    account, display_name, role, password_hash, is_active, is_protected,
                    session_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, 0, 1, ?, ?)
                """,
                (account, display_name, role, hash_password(password), ts, ts),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        set_flash(request, "danger", "Account already exists.")
        return RedirectResponse("/admin/users", status_code=303)

    set_flash(request, "success", "User created.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/{account}/delete")
def admin_user_delete(request: Request, account: str, current: sqlite3.Row = Depends(current_admin)):
    if account == current["account"]:
        set_flash(request, "danger", "You cannot delete your own account.")
        return RedirectResponse("/admin/users", status_code=303)

    with closing(db_connect()) as conn:
        target = conn.execute("SELECT * FROM users WHERE account = ?", (account,)).fetchone()
        if not target:
            set_flash(request, "danger", "User not found.")
            return RedirectResponse("/admin/users", status_code=303)
        if target["is_protected"]:
            set_flash(request, "danger", "Protected admin cannot be deleted.")
            return RedirectResponse("/admin/users", status_code=303)
        conn.execute("DELETE FROM users WHERE account = ?", (account,))
        conn.commit()

    set_flash(request, "success", "User deleted.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/{account}/active")
def admin_user_set_active(
    request: Request,
    account: str,
    active: int = Form(...),
    current: sqlite3.Row = Depends(current_admin),
):
    if account == current["account"] and int(active) == 0:
        set_flash(request, "danger", "You cannot disable your own account.")
        return RedirectResponse("/admin/users", status_code=303)

    with closing(db_connect()) as conn:
        target = conn.execute("SELECT * FROM users WHERE account = ?", (account,)).fetchone()
        if not target:
            set_flash(request, "danger", "User not found.")
            return RedirectResponse("/admin/users", status_code=303)
        if target["is_protected"] and int(active) == 0:
            set_flash(request, "danger", "Protected admin cannot be disabled.")
            return RedirectResponse("/admin/users", status_code=303)
        conn.execute(
            """
            UPDATE users
               SET is_active = ?,
                   session_version = session_version + 1,
                   updated_at = ?
             WHERE account = ?
            """,
            (1 if int(active) else 0, now_iso(), account),
        )
        conn.commit()

    set_flash(request, "success", "User status updated.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/{account}/password")
def admin_user_reset_password(
    request: Request,
    account: str,
    new_password: str = Form(...),
    _: sqlite3.Row = Depends(current_admin),
):
    if len(new_password) < 8:
        set_flash(request, "danger", "Password must be at least 8 characters.")
        return RedirectResponse("/admin/users", status_code=303)

    with closing(db_connect()) as conn:
        target = conn.execute("SELECT * FROM users WHERE account = ?", (account,)).fetchone()
        if not target:
            set_flash(request, "danger", "User not found.")
            return RedirectResponse("/admin/users", status_code=303)
        conn.execute(
            """
            UPDATE users
               SET password_hash = ?,
                   session_version = session_version + 1,
                   updated_at = ?
             WHERE account = ?
            """,
            (hash_password(new_password), now_iso(), account),
        )
        conn.commit()

    set_flash(request, "success", "Password reset successfully.")
    return RedirectResponse("/admin/users", status_code=303)
