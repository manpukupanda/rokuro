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

router = APIRouter(prefix="/admin/series", tags=["admin-series"])


@router.get("")
def admin_series_page(request: Request, _: sqlite3.Row = Depends(current_admin)):
    with closing(db_connect()) as conn:
        series_list = conn.execute("SELECT * FROM series ORDER BY display_order, name").fetchall()
    return templates.TemplateResponse(
        request,
        "admin_series.html",
        base_context(request, series_list=series_list),
    )


@router.post("/create")
def admin_series_create(
    request: Request,
    name: str = Form(...),
    display_order: int = Form(0),
    _: sqlite3.Row = Depends(current_admin),
):
    name = name.strip()
    if not name:
        set_flash(request, "danger", "Series name is required.")
        return RedirectResponse("/admin/series", status_code=303)

    ts = now_iso()
    try:
        with closing(db_connect()) as conn:
            conn.execute(
                "INSERT INTO series (name, display_order, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, display_order, ts, ts),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        set_flash(request, "danger", "A series with that name already exists.")
        return RedirectResponse("/admin/series", status_code=303)

    set_flash(request, "success", "Series created.")
    return RedirectResponse("/admin/series", status_code=303)


@router.post("/{series_id}/update")
def admin_series_update(
    request: Request,
    series_id: int,
    name: str = Form(...),
    display_order: int = Form(0),
    _: sqlite3.Row = Depends(current_admin),
):
    name = name.strip()
    if not name:
        set_flash(request, "danger", "Series name is required.")
        return RedirectResponse("/admin/series", status_code=303)

    try:
        with closing(db_connect()) as conn:
            result = conn.execute(
                "UPDATE series SET name = ?, display_order = ?, updated_at = ? WHERE id = ?",
                (name, display_order, now_iso(), series_id),
            )
            conn.commit()
        if result.rowcount == 0:
            set_flash(request, "danger", "Series not found.")
    except sqlite3.IntegrityError:
        set_flash(request, "danger", "A series with that name already exists.")
        return RedirectResponse("/admin/series", status_code=303)

    set_flash(request, "success", "Series updated.")
    return RedirectResponse("/admin/series", status_code=303)


@router.post("/{series_id}/delete")
def admin_series_delete(request: Request, series_id: int, _: sqlite3.Row = Depends(current_admin)):
    with closing(db_connect()) as conn:
        conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
        conn.commit()

    set_flash(request, "success", "Series deleted.")
    return RedirectResponse("/admin/series", status_code=303)


@router.get("/{series_id}/videos")
def admin_series_videos_page(request: Request, series_id: int, _: sqlite3.Row = Depends(current_admin)):
    with closing(db_connect()) as conn:
        series = conn.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
        if not series:
            set_flash(request, "danger", "Series not found.")
            return RedirectResponse("/admin/series", status_code=303)
        membership_rows = conn.execute(
            """
            SELECT video_id, series_order, created_at
              FROM video_series
             WHERE series_id = ?
             ORDER BY COALESCE(series_order, 2147483647), created_at
            """,
            (series_id,),
        ).fetchall()

    assigned_ids = {row["video_id"] for row in membership_rows}
    order_map = {row["video_id"]: row["series_order"] for row in membership_rows}

    all_videos: list[dict[str, Any]] = []
    error_message = None
    try:
        all_videos = goro_list_videos()
    except GoroAPIError:
        error_message = "Failed to load videos from goro API."

    video_lookup = {v["public_id"]: v for v in all_videos}
    assigned_videos = []
    for row in membership_rows:
        v = video_lookup.get(row["video_id"])
        if v:
            v = dict(v)
            v["series_order"] = order_map[row["video_id"]]
            assigned_videos.append(v)

    available_videos = [v for v in all_videos if v["public_id"] not in assigned_ids]
    return templates.TemplateResponse(
        request,
        "admin_series_videos.html",
        base_context(
            request,
            series=series,
            assigned_videos=assigned_videos,
            available_videos=available_videos,
            error_message=error_message,
        ),
    )


@router.post("/{series_id}/videos/add")
def admin_series_video_add(
    request: Request,
    series_id: int,
    video_id: str = Form(...),
    series_order: str = Form(""),
    _: sqlite3.Row = Depends(current_admin),
):
    order_val: int | None = None
    if series_order.strip():
        try:
            order_val = int(series_order.strip())
        except ValueError:
            set_flash(request, "danger", "Order must be an integer.")
            return RedirectResponse(f"/admin/series/{int(series_id)}/videos", status_code=303)

    with closing(db_connect()) as conn:
        if not conn.execute("SELECT id FROM series WHERE id = ?", (series_id,)).fetchone():
            set_flash(request, "danger", "Series not found.")
            return RedirectResponse("/admin/series", status_code=303)
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO video_series (video_id, series_id, series_order, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (video_id) DO UPDATE
               SET series_id = excluded.series_id,
                   series_order = excluded.series_order,
                   created_at = excluded.created_at
            """,
            (video_id, series_id, order_val, ts),
        )
        conn.commit()

    set_flash(request, "success", "Video added to series.")
    return RedirectResponse(f"/admin/series/{int(series_id)}/videos", status_code=303)


@router.post("/{series_id}/videos/update/{video_id}")
def admin_series_video_update_order(
    request: Request,
    series_id: int,
    video_id: str,
    series_order: str = Form(""),
    _: sqlite3.Row = Depends(current_admin),
):
    order_val: int | None = None
    if series_order.strip():
        try:
            order_val = int(series_order.strip())
        except ValueError:
            set_flash(request, "danger", "Order must be an integer.")
            return RedirectResponse(f"/admin/series/{int(series_id)}/videos", status_code=303)

    with closing(db_connect()) as conn:
        conn.execute(
            "UPDATE video_series SET series_order = ? WHERE video_id = ? AND series_id = ?",
            (order_val, video_id, series_id),
        )
        conn.commit()

    set_flash(request, "success", "Order updated.")
    return RedirectResponse(f"/admin/series/{int(series_id)}/videos", status_code=303)


@router.post("/{series_id}/videos/remove/{video_id}")
def admin_series_video_remove(
    request: Request,
    series_id: int,
    video_id: str,
    _: sqlite3.Row = Depends(current_admin),
):
    with closing(db_connect()) as conn:
        conn.execute(
            "DELETE FROM video_series WHERE video_id = ? AND series_id = ?",
            (video_id, series_id),
        )
        conn.commit()

    set_flash(request, "success", "Video removed from series.")
    return RedirectResponse(f"/admin/series/{int(series_id)}/videos", status_code=303)
