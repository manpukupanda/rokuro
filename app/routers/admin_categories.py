import sqlite3
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from auth import current_admin
from database import db_connect, now_iso
from flash import base_context, set_flash
from goro import GoroAPIError, goro_list_videos
from web import templates

router = APIRouter(prefix="/admin/categories", tags=["admin-categories"])


@router.get("")
def admin_categories_page(request: Request, _: sqlite3.Row = Depends(current_admin)):
    with closing(db_connect()) as conn:
        categories = conn.execute("SELECT * FROM categories ORDER BY display_order, name").fetchall()
    return templates.TemplateResponse(
        request,
        "admin_categories.html",
        base_context(request, categories=categories),
    )


@router.post("/create")
def admin_category_create(
    request: Request,
    name: str = Form(...),
    display_order: int = Form(0),
    _: sqlite3.Row = Depends(current_admin),
):
    name = name.strip()
    if not name:
        set_flash(request, "danger", "Category name is required.")
        return RedirectResponse("/admin/categories", status_code=303)

    ts = now_iso()
    try:
        with closing(db_connect()) as conn:
            conn.execute(
                "INSERT INTO categories (name, display_order, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, display_order, ts, ts),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        set_flash(request, "danger", "A category with that name already exists.")
        return RedirectResponse("/admin/categories", status_code=303)

    set_flash(request, "success", "Category created.")
    return RedirectResponse("/admin/categories", status_code=303)


@router.post("/{cat_id}/update")
def admin_category_update(
    request: Request,
    cat_id: int,
    name: str = Form(...),
    display_order: int = Form(0),
    _: sqlite3.Row = Depends(current_admin),
):
    name = name.strip()
    if not name:
        set_flash(request, "danger", "Category name is required.")
        return RedirectResponse("/admin/categories", status_code=303)

    try:
        with closing(db_connect()) as conn:
            result = conn.execute(
                "UPDATE categories SET name = ?, display_order = ?, updated_at = ? WHERE id = ?",
                (name, display_order, now_iso(), cat_id),
            )
            conn.commit()
        if result.rowcount == 0:
            set_flash(request, "danger", "Category not found.")
    except sqlite3.IntegrityError:
        set_flash(request, "danger", "A category with that name already exists.")
        return RedirectResponse("/admin/categories", status_code=303)

    set_flash(request, "success", "Category updated.")
    return RedirectResponse("/admin/categories", status_code=303)


@router.post("/{cat_id}/delete")
def admin_category_delete(request: Request, cat_id: int, _: sqlite3.Row = Depends(current_admin)):
    with closing(db_connect()) as conn:
        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        conn.commit()

    set_flash(request, "success", "Category deleted.")
    return RedirectResponse("/admin/categories", status_code=303)


@router.get("/{cat_id}/videos")
def admin_category_videos_page(request: Request, cat_id: int, _: sqlite3.Row = Depends(current_admin)):
    with closing(db_connect()) as conn:
        category = conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
        if not category:
            set_flash(request, "danger", "Category not found.")
            return RedirectResponse("/admin/categories", status_code=303)
        assigned_ids = {
            row["video_id"]
            for row in conn.execute(
                "SELECT video_id FROM video_categories WHERE category_id = ?", (cat_id,)
            ).fetchall()
        }

    all_videos: list[dict[str, Any]] = []
    error_message = None
    try:
        all_videos = goro_list_videos()
    except GoroAPIError:
        error_message = "Failed to load videos from goro API."

    assigned_videos = [v for v in all_videos if v["public_id"] in assigned_ids]
    available_videos = [v for v in all_videos if v["public_id"] not in assigned_ids]
    return templates.TemplateResponse(
        request,
        "admin_category_videos.html",
        base_context(
            request,
            category=category,
            assigned_videos=assigned_videos,
            available_videos=available_videos,
            error_message=error_message,
        ),
    )


@router.post("/{cat_id}/videos/add")
def admin_category_video_add(
    request: Request,
    cat_id: int,
    video_id: str = Form(...),
    _: sqlite3.Row = Depends(current_admin),
):
    with closing(db_connect()) as conn:
        if not conn.execute("SELECT id FROM categories WHERE id = ?", (cat_id,)).fetchone():
            set_flash(request, "danger", "Category not found.")
            return RedirectResponse("/admin/categories", status_code=303)
        conn.execute(
            "INSERT OR IGNORE INTO video_categories (video_id, category_id) VALUES (?, ?)",
            (video_id, cat_id),
        )
        conn.commit()

    set_flash(request, "success", "Video added to category.")
    return RedirectResponse(f"/admin/categories/{int(cat_id)}/videos", status_code=303)


@router.post("/{cat_id}/videos/remove/{video_id}")
def admin_category_video_remove(
    request: Request,
    cat_id: int,
    video_id: str,
    _: sqlite3.Row = Depends(current_admin),
):
    with closing(db_connect()) as conn:
        conn.execute(
            "DELETE FROM video_categories WHERE video_id = ? AND category_id = ?",
            (video_id, cat_id),
        )
        conn.commit()

    set_flash(request, "success", "Video removed from category.")
    return RedirectResponse(f"/admin/categories/{int(cat_id)}/videos", status_code=303)
