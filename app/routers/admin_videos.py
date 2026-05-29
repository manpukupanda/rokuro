import sqlite3
from contextlib import closing
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from auth import current_admin
from database import db_connect, now_iso
from flash import base_context, set_flash
from goro import GoroAPIError, goro_delete, goro_list_videos, goro_post, goro_put
from video_helpers import apply_local_video_metadata, set_video_tags, sort_videos
from web import templates

router = APIRouter(prefix="/admin/videos", tags=["admin-videos"])


@router.get("")
def admin_videos_page(request: Request, _: sqlite3.Row = Depends(current_admin)):
    videos: list[dict[str, Any]] = []
    error_message = None
    try:
        videos = goro_list_videos()
    except GoroAPIError:
        error_message = "Failed to load videos from goro API."
    apply_local_video_metadata(videos)
    videos = sort_videos(videos)

    with closing(db_connect()) as conn:
        tag_rows = conn.execute(
            """
            SELECT vt.video_id, t.name
              FROM video_tags vt
              JOIN tags t ON t.id = vt.tag_id
             ORDER BY t.name
            """
        ).fetchall()
    video_tags: dict[str, list[str]] = {}
    for row in tag_rows:
        video_tags.setdefault(row["video_id"], []).append(row["name"])
    for video in videos:
        names = video_tags.get(video["public_id"], [])
        video["tags_str"] = " ".join(f"#{n}" for n in names)

    return templates.TemplateResponse(
        request,
        "admin_videos.html",
        base_context(request, videos=videos, error_message=error_message),
    )


@router.post("/upload")
def admin_video_upload(request: Request, file: UploadFile, _: sqlite3.Row = Depends(current_admin)):
    filename = file.filename or ""
    if not filename.lower().endswith(".mp4"):
        set_flash(request, "danger", "Only .mp4 files are supported.")
        return RedirectResponse("/admin/videos", status_code=303)

    try:
        goro_post(
            "/videos",
            files={"file": (filename, file.file, file.content_type or "video/mp4")},
        )
    except GoroAPIError:
        set_flash(request, "danger", "Upload failed.")
        return RedirectResponse("/admin/videos", status_code=303)

    set_flash(request, "success", "Video upload accepted.")
    return RedirectResponse("/admin/videos", status_code=303)


@router.post("/{video_id}/visibility")
def admin_video_visibility(
    request: Request,
    video_id: str,
    visibility: str = Form(...),
    _: sqlite3.Row = Depends(current_admin),
):
    if visibility not in {"public", "private"}:
        set_flash(request, "danger", "Invalid visibility.")
        return RedirectResponse("/admin/videos", status_code=303)

    try:
        goro_put(f"/videos/{quote(video_id)}/visibility", {"visibility": visibility})
    except GoroAPIError:
        set_flash(request, "danger", "Failed to update visibility.")
        return RedirectResponse("/admin/videos", status_code=303)

    set_flash(request, "success", "Visibility updated.")
    return RedirectResponse("/admin/videos", status_code=303)


@router.post("/{video_id}/delete")
def admin_video_delete(request: Request, video_id: str, _: sqlite3.Row = Depends(current_admin)):
    try:
        goro_delete(f"/videos/{quote(video_id)}")
    except GoroAPIError:
        set_flash(request, "danger", "Failed to delete video.")
        return RedirectResponse("/admin/videos", status_code=303)

    set_flash(request, "success", "Video deleted.")
    return RedirectResponse("/admin/videos", status_code=303)


@router.post("/{video_id}/tags")
def admin_video_update_tags(
    request: Request,
    video_id: str,
    tags: str = Form(""),
    _: sqlite3.Row = Depends(current_admin),
):
    with closing(db_connect()) as conn:
        set_video_tags(conn, video_id, tags)
        conn.commit()

    set_flash(request, "success", "Tags updated.")
    return RedirectResponse("/admin/videos", status_code=303)


@router.post("/{video_id}/metadata")
def admin_video_update_metadata(
    request: Request,
    video_id: str,
    description: str = Form(""),
    display_order: str = Form("0"),
    _: sqlite3.Row = Depends(current_admin),
):
    try:
        order_val = int(display_order.strip() or "0")
    except ValueError:
        set_flash(request, "danger", "Display order must be an integer.")
        return RedirectResponse("/admin/videos", status_code=303)

    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO video_metadata (video_id, description, display_order, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (video_id) DO UPDATE
               SET description = excluded.description,
                   display_order = excluded.display_order,
                   updated_at = excluded.updated_at
            """,
            (video_id, description.strip(), order_val, now_iso()),
        )
        conn.commit()

    set_flash(request, "success", "Metadata updated.")
    return RedirectResponse("/admin/videos", status_code=303)
