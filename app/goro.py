from typing import Any
from urllib.parse import quote

import httpx

from config import GORO_API_BASE_URL, GORO_API_KEY


class GoroAPIError(Exception):
    pass


def goro_headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + GORO_API_KEY}


def goro_get(path: str, params: dict[str, Any] | None = None) -> Any:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(f"{GORO_API_BASE_URL}{path}", headers=goro_headers(), params=params)
    if response.status_code >= 400:
        raise GoroAPIError(response.text)
    return response.json()


def goro_post(path: str, files: dict[str, Any] | None = None) -> Any:
    with httpx.Client(timeout=120.0) as client:
        response = client.post(f"{GORO_API_BASE_URL}{path}", headers=goro_headers(), files=files)
    if response.status_code >= 400:
        raise GoroAPIError(response.text)
    if response.status_code == 204:
        return None
    return response.json()


def goro_put(path: str, payload: dict[str, Any]) -> Any:
    with httpx.Client(timeout=30.0) as client:
        response = client.put(f"{GORO_API_BASE_URL}{path}", headers=goro_headers(), json=payload)
    if response.status_code >= 400:
        raise GoroAPIError(response.text)
    return response.json()


def goro_delete(path: str) -> None:
    with httpx.Client(timeout=30.0) as client:
        response = client.delete(f"{GORO_API_BASE_URL}{path}", headers=goro_headers())
    if response.status_code >= 400:
        raise GoroAPIError(response.text)


def goro_list_videos() -> list[dict[str, Any]]:
    payload = goro_get("/videos")
    videos = payload.get("videos", [])
    return sorted(videos, key=lambda x: x.get("created_at", ""), reverse=True)


def goro_get_video_detail(video_id: str) -> dict[str, Any] | None:
    try:
        return goro_get(f"/videos/{quote(video_id)}")
    except GoroAPIError:
        return None


def goro_issue_token(video_id: str) -> str | None:
    try:
        token_resp = goro_post(f"/videos/{quote(video_id)}/tokens")
    except GoroAPIError:
        return None
    token = token_resp.get("token", "")
    return token if token else None
