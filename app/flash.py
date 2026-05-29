from typing import Any

from fastapi import Request

from auth import get_current_user_optional
from config import GORO_PLAYBACK_BASE_URL


def set_flash(request: Request, level: str, message: str) -> None:
    request.session.setdefault("flash", []).append({"level": level, "message": message})


def pop_flash(request: Request) -> list[dict[str, str]]:
    flashes = request.session.get("flash", [])
    request.session["flash"] = []
    return flashes


def base_context(request: Request, **kwargs: Any) -> dict[str, Any]:
    return {
        "request": request,
        "current_user": get_current_user_optional(request),
        "flashes": pop_flash(request),
        "goro_playback_base_url": GORO_PLAYBACK_BASE_URL.rstrip("/"),
        **kwargs,
    }
