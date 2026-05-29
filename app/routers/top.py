from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request

from auth import get_current_user_optional
from flash import base_context
from goro import GoroAPIError, goro_get_video_detail, goro_issue_token, goro_list_videos
from web import templates
from video_helpers import apply_local_video_metadata, load_classification_for_videos, sort_videos

router = APIRouter(tags=["top"])


def _collect_thumbnails(
    videos: list[dict[str, Any]],
    user: Any,
    goro_playback_base_url: str,
) -> tuple[dict[str, str | None], dict[str, dict[str, Any]]]:
    thumbnail_names: dict[str, str] = {}
    video_details: dict[str, dict[str, Any]] = {}
    if videos:
        with ThreadPoolExecutor(max_workers=min(8, len(videos))) as executor:
            detail_futures = {
                executor.submit(goro_get_video_detail, video["public_id"]): video["public_id"]
                for video in videos
                if video.get("public_id")
            }
            for future in as_completed(detail_futures):
                video_id_for_detail = detail_futures[future]
                detail = future.result()
                if detail:
                    video_details[video_id_for_detail] = detail
                names = detail.get("thumbnail_names", []) if detail else []
                if names:
                    thumbnail_names[video_id_for_detail] = names[0]

    private_tokens: dict[str, str] = {}
    private_ids = [
        video["public_id"]
        for video in videos
        if user and video.get("visibility") == "private" and thumbnail_names.get(video.get("public_id", ""))
    ]
    if private_ids:
        with ThreadPoolExecutor(max_workers=min(8, len(private_ids))) as executor:
            token_futures = {executor.submit(goro_issue_token, v_id): v_id for v_id in private_ids}
            for future in as_completed(token_futures):
                private_video_id = token_futures[future]
                token = future.result()
                if token:
                    private_tokens[private_video_id] = token

    thumbnail_urls: dict[str, str | None] = {}
    for video in videos:
        video_id_for_thumbnail = video.get("public_id", "")
        thumbnail_name = thumbnail_names.get(video_id_for_thumbnail)
        thumbnail_url = None
        if thumbnail_name and video_id_for_thumbnail:
            base_thumbnail_url = (
                f"{goro_playback_base_url}/videos/"
                f"{quote(video_id_for_thumbnail)}/thumbnails/{quote(thumbnail_name)}"
            )
            if video.get("visibility") == "private":
                token = private_tokens.get(video_id_for_thumbnail)
                if token:
                    thumbnail_url = f"{base_thumbnail_url}?token={quote(token, safe='')}"
            else:
                thumbnail_url = base_thumbnail_url
        thumbnail_urls[video_id_for_thumbnail] = thumbnail_url

    return thumbnail_urls, video_details


def _get_playback_info(
    video_id: str | None,
    videos: list[dict[str, Any]],
    video_details: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]], str | None]:
    if not video_id:
        return None, None, [], None

    selected = next((v for v in videos if v.get("public_id") == video_id), None)
    if not selected or selected.get("status") != "ready":
        return selected, None, [], None

    token = goro_issue_token(video_id)
    if not token:
        return selected, None, [], "Failed to issue playback token."

    detail = video_details.get(video_id) or goro_get_video_detail(video_id)
    profiles = detail.get("profiles", []) if detail else []
    return selected, token, profiles, None


@router.get("/healthz")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/")
def top_page(request: Request, video_id: str | None = None):
    user = get_current_user_optional(request)
    videos: list[dict[str, Any]] = []
    error_message = None
    try:
        videos = goro_list_videos()
    except GoroAPIError:
        error_message = "Failed to load videos from goro API."

    if not user:
        videos = [v for v in videos if v.get("visibility") == "public" and v.get("status") == "ready"]
    elif user["role"] != "admin":
        videos = [v for v in videos if v.get("status") == "ready"]

    apply_local_video_metadata(videos)
    videos = sort_videos(videos)

    ctx = base_context(request)
    playback_base_url = ctx["goro_playback_base_url"]
    thumbnail_urls, video_details = _collect_thumbnails(videos, user, playback_base_url)
    for video in videos:
        video["thumbnail_url"] = thumbnail_urls.get(video.get("public_id", ""))

    selected, playback_token, selected_profiles, playback_error = _get_playback_info(
        video_id,
        videos,
        video_details,
    )
    if playback_error:
        error_message = playback_error

    categories_with_videos, series_with_videos, tags_with_videos = load_classification_for_videos(videos)

    return templates.TemplateResponse(
        request,
        "top.html",
        base_context(
            request,
            videos=videos,
            selected_video=selected,
            playback_token=playback_token,
            selected_profiles=selected_profiles,
            error_message=error_message,
            categories_with_videos=categories_with_videos,
            series_with_videos=series_with_videos,
            tags_with_videos=tags_with_videos,
        ),
    )
