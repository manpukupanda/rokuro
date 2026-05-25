import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import bcrypt
import httpx
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("ROKURO_DB_PATH", "/var/lib/rokuro/rokuro.db")
GORO_API_BASE_URL = os.getenv("GORO_API_BASE_URL", "http://goro:5600")
GORO_PLAYBACK_BASE_URL = os.getenv("GORO_PLAYBACK_BASE_URL", "http://localhost:5600")
GORO_API_KEY = os.getenv("GORO_API_KEY", "")
SESSION_SECRET = os.getenv("ROKURO_SESSION_SECRET", "")
INITIAL_ADMIN_ACCOUNT = "admin"
INITIAL_ADMIN_PASSWORD = os.getenv("ROKURO_INITIAL_ADMIN_PASSWORD", "")

if not GORO_API_KEY:
    raise RuntimeError("GORO_API_KEY must be set")
if not SESSION_SECRET:
    raise RuntimeError("ROKURO_SESSION_SECRET must be set")
if not INITIAL_ADMIN_PASSWORD:
    raise RuntimeError("ROKURO_INITIAL_ADMIN_PASSWORD must be set")


class GoroAPIError(Exception):
    pass


app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=60 * 60 * 24 * 7,
    https_only=False,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


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


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with closing(db_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                account TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_protected INTEGER NOT NULL DEFAULT 0,
                session_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def seed_initial_admin() -> None:
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT account FROM users WHERE account = ?", (INITIAL_ADMIN_ACCOUNT,)
        ).fetchone()
        if row:
            return
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO users (
                account, display_name, role, password_hash, is_active, is_protected,
                session_version, created_at, updated_at
            ) VALUES (?, ?, 'admin', ?, 1, 1, 1, ?, ?)
            """,
            (INITIAL_ADMIN_ACCOUNT, "Administrator", hash_password(INITIAL_ADMIN_PASSWORD), ts, ts),
        )
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_initial_admin()


def fetch_user(account: str) -> sqlite3.Row | None:
    with closing(db_connect()) as conn:
        return conn.execute("SELECT * FROM users WHERE account = ?", (account,)).fetchone()


def get_current_user_optional(request: Request) -> sqlite3.Row | None:
    account = request.session.get("account")
    session_version = request.session.get("session_version")
    if not account or not session_version:
        return None
    user = fetch_user(account)
    if not user:
        request.session.clear()
        return None
    if not user["is_active"] or user["session_version"] != session_version:
        request.session.clear()
        return None
    return user


def require_login(request: Request) -> sqlite3.Row:
    user = get_current_user_optional(request)
    if not user:
        raise PermissionError
    return user


def require_admin(request: Request) -> sqlite3.Row:
    user = require_login(request)
    if user["role"] != "admin":
        raise PermissionError
    return user


def goro_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {GORO_API_KEY}"}


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


def get_video_by_id(video_id: str) -> dict[str, Any] | None:
    for video in goro_list_videos():
        if video.get("public_id") == video_id:
            return video
    return None


@app.get("/healthz")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def top_page(request: Request, video_id: str | None = None):
    user = get_current_user_optional(request)
    videos: list[dict[str, Any]] = []
    error_message = None
    try:
        videos = goro_list_videos()
    except GoroAPIError:
        error_message = "Failed to load videos from goro API."

    if not user:
        videos = [v for v in videos if v.get("visibility") == "public"]

    selected = None
    if video_id:
        selected = next((v for v in videos if v.get("public_id") == video_id), None)

    return templates.TemplateResponse(
        request,
        "top.html",
        base_context(request, videos=videos, selected_video=selected, error_message=error_message),
    )


@app.get("/stream/{video_id}/token")
def stream_token(request: Request, video_id: str):
    user = get_current_user_optional(request)
    video = get_video_by_id(video_id)
    if not video:
        return JSONResponse({"error": "not_found"}, status_code=404)

    visibility = video.get("visibility")
    if visibility == "private" and not user:
        return JSONResponse({"error": "login_required"}, status_code=403)

    try:
        token_resp = goro_post(f"/videos/{quote(video_id)}/tokens")
    except GoroAPIError:
        return JSONResponse({"error": "token_issue_failed"}, status_code=502)

    return {"token": token_resp.get("token", "")}


@app.get("/login")
def login_form(request: Request):
    if get_current_user_optional(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", base_context(request))


@app.post("/login")
def login(request: Request, account: str = Form(...), password: str = Form(...)):
    user = fetch_user(account)
    if not user or not user["is_active"] or not verify_password(password, user["password_hash"]):
        set_flash(request, "danger", "Invalid credentials.")
        return RedirectResponse("/login", status_code=303)

    request.session["account"] = user["account"]
    request.session["session_version"] = user["session_version"]
    set_flash(request, "success", "Logged in successfully.")
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/profile")
def profile_page(request: Request):
    try:
        user = require_login(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "profile.html", base_context(request, user=user))


@app.post("/profile/display-name")
def update_display_name(request: Request, display_name: str = Form(...)):
    try:
        user = require_login(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/profile/password")
def update_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    try:
        user = require_login(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.get("/admin/users")
def admin_users_page(request: Request):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    with closing(db_connect()) as conn:
        users = conn.execute(
            "SELECT account, display_name, role, is_active, is_protected, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    return templates.TemplateResponse(request, "admin_users.html", base_context(request, users=users))


@app.post("/admin/users/create")
def admin_user_create(
    request: Request,
    account: str = Form(...),
    display_name: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/users/{account}/delete")
def admin_user_delete(request: Request, account: str):
    try:
        current = require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/users/{account}/active")
def admin_user_set_active(request: Request, account: str, active: int = Form(...)):
    try:
        current = require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/users/{account}/password")
def admin_user_reset_password(request: Request, account: str, new_password: str = Form(...)):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.get("/admin/videos")
def admin_videos_page(request: Request):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    videos: list[dict[str, Any]] = []
    error_message = None
    try:
        videos = goro_list_videos()
    except GoroAPIError:
        error_message = "Failed to load videos from goro API."
    return templates.TemplateResponse(
        request, "admin_videos.html", base_context(request, videos=videos, error_message=error_message)
    )


@app.post("/admin/videos/upload")
def admin_video_upload(request: Request, file: UploadFile):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/videos/{video_id}/visibility")
def admin_video_visibility(request: Request, video_id: str, visibility: str = Form(...)):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/videos/{video_id}/delete")
def admin_video_delete(request: Request, video_id: str):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    try:
        goro_delete(f"/videos/{quote(video_id)}")
    except GoroAPIError:
        set_flash(request, "danger", "Failed to delete video.")
        return RedirectResponse("/admin/videos", status_code=303)

    set_flash(request, "success", "Video deleted.")
    return RedirectResponse("/admin/videos", status_code=303)
