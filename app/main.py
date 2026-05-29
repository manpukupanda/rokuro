import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    conn.execute("PRAGMA foreign_keys = ON")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_categories (
                video_id TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                PRIMARY KEY (video_id, category_id),
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_tags (
                video_id TEXT NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (video_id, tag_id),
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_series (
                video_id TEXT NOT NULL PRIMARY KEY,
                series_id INTEGER NOT NULL,
                series_order INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
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


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------

def parse_tags(tags_str: str) -> list[str]:
    """Parse a string like '#Tag1 #tag2 tag3' into normalised tag names."""
    tokens = re.split(r"[\s\u3000]+", tags_str.strip())
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        name = t.lstrip("#").lower()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def set_video_tags(conn: sqlite3.Connection, video_id: str, tags_str: str) -> None:
    """Replace all tags for *video_id* with the parsed contents of *tags_str*.

    Orphaned tags (no remaining associations) are pruned automatically.
    """
    names = parse_tags(tags_str)
    conn.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
    ts = now_iso()
    for name in names:
        conn.execute("INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (name, ts))
        tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO video_tags (video_id, tag_id) VALUES (?, ?)",
            (video_id, tag_row["id"]),
        )
    conn.execute(
        "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM video_tags)"
    )


# ---------------------------------------------------------------------------
# Classification helpers (used by top_page)
# ---------------------------------------------------------------------------

def load_classification_for_videos(
    videos: list[dict[str, Any]],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return categories/series/tags filtered to *videos* that are visible.

    Each item in the returned lists is a dict with keys ``name`` and ``videos``
    (a list of video dicts drawn from *videos*).  Groups that contain no
    visible videos are omitted.
    """
    if not videos:
        return [], [], []

    video_lookup = {v["public_id"]: v for v in videos}
    visible_ids = set(video_lookup.keys())

    with closing(db_connect()) as conn:
        # ---- categories ----
        cat_rows = conn.execute(
            """
            SELECT vc.category_id, vc.video_id, c.name
              FROM video_categories vc
              JOIN categories c ON c.id = vc.category_id
             ORDER BY c.display_order, c.name
            """
        ).fetchall()
        cat_map: dict[int, dict] = {}
        for row in cat_rows:
            if row["video_id"] not in visible_ids:
                continue
            cid = row["category_id"]
            if cid not in cat_map:
                cat_map[cid] = {"name": row["name"], "videos": []}
            cat_map[cid]["videos"].append(video_lookup[row["video_id"]])
        categories_with_videos = [v for v in cat_map.values() if v["videos"]]

        # ---- series ----
        series_rows = conn.execute(
            """
            SELECT vs.series_id, vs.video_id, s.name
              FROM video_series vs
              JOIN series s ON s.id = vs.series_id
             ORDER BY s.display_order, s.name,
                      COALESCE(vs.series_order, 2147483647), vs.created_at
            """
        ).fetchall()
        series_map: dict[int, dict] = {}
        for row in series_rows:
            if row["video_id"] not in visible_ids:
                continue
            sid = row["series_id"]
            if sid not in series_map:
                series_map[sid] = {"name": row["name"], "videos": []}
            series_map[sid]["videos"].append(video_lookup[row["video_id"]])
        series_with_videos = [v for v in series_map.values() if v["videos"]]

        # ---- tags ----
        tag_rows = conn.execute(
            """
            SELECT vt.tag_id, vt.video_id, t.name
              FROM video_tags vt
              JOIN tags t ON t.id = vt.tag_id
             ORDER BY t.name
            """
        ).fetchall()
        tag_map: dict[int, dict] = {}
        for row in tag_rows:
            if row["video_id"] not in visible_ids:
                continue
            tid = row["tag_id"]
            if tid not in tag_map:
                tag_map[tid] = {"name": row["name"], "videos": []}
            tag_map[tid]["videos"].append(video_lookup[row["video_id"]])
        tags_with_videos = [v for v in tag_map.values() if v["videos"]]

    return categories_with_videos, series_with_videos, tags_with_videos


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
        videos = [v for v in videos if v.get("visibility") == "public" and v.get("status") == "ready"]
    elif user["role"] != "admin":
        videos = [v for v in videos if v.get("status") == "ready"]

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

    for video in videos:
        video_id_for_thumbnail = video.get("public_id", "")
        thumbnail_name = thumbnail_names.get(video_id_for_thumbnail)
        thumbnail_url = None
        if thumbnail_name and video_id_for_thumbnail:
            base_thumbnail_url = (
                f"{GORO_PLAYBACK_BASE_URL.rstrip('/')}/videos/"
                f"{quote(video_id_for_thumbnail)}/thumbnails/{quote(thumbnail_name)}"
            )
            if video.get("visibility") == "private":
                token = private_tokens.get(video_id_for_thumbnail)
                if token:
                    thumbnail_url = f"{base_thumbnail_url}?token={quote(token, safe='')}"
            else:
                thumbnail_url = base_thumbnail_url
        video["thumbnail_url"] = thumbnail_url

    selected = None
    playback_token = None
    selected_profiles: list[dict[str, Any]] = []
    if video_id:
        selected = next((v for v in videos if v.get("public_id") == video_id), None)
        if selected and selected.get("status") == "ready":
            token = goro_issue_token(video_id)
            if token:
                playback_token = token
                detail = video_details.get(video_id) or goro_get_video_detail(video_id)
                if detail:
                    selected_profiles = detail.get("profiles", [])
            else:
                error_message = "Failed to issue playback token."

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


# ---------------------------------------------------------------------------
# Admin: video tags
# ---------------------------------------------------------------------------

@app.post("/admin/videos/{video_id}/tags")
def admin_video_update_tags(request: Request, video_id: str, tags: str = Form("")):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    with closing(db_connect()) as conn:
        set_video_tags(conn, video_id, tags)
        conn.commit()

    set_flash(request, "success", "Tags updated.")
    return RedirectResponse("/admin/videos", status_code=303)


# ---------------------------------------------------------------------------
# Admin: categories
# ---------------------------------------------------------------------------

@app.get("/admin/categories")
def admin_categories_page(request: Request):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    with closing(db_connect()) as conn:
        categories = conn.execute(
            "SELECT * FROM categories ORDER BY display_order, name"
        ).fetchall()
    return templates.TemplateResponse(
        request, "admin_categories.html", base_context(request, categories=categories)
    )


@app.post("/admin/categories/create")
def admin_category_create(
    request: Request,
    name: str = Form(...),
    display_order: int = Form(0),
):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/categories/{cat_id}/update")
def admin_category_update(
    request: Request,
    cat_id: int,
    name: str = Form(...),
    display_order: int = Form(0),
):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/categories/{cat_id}/delete")
def admin_category_delete(request: Request, cat_id: int):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    with closing(db_connect()) as conn:
        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        conn.commit()

    set_flash(request, "success", "Category deleted.")
    return RedirectResponse("/admin/categories", status_code=303)


@app.get("/admin/categories/{cat_id}/videos")
def admin_category_videos_page(request: Request, cat_id: int):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/categories/{cat_id}/videos/add")
def admin_category_video_add(request: Request, cat_id: int, video_id: str = Form(...)):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/categories/{cat_id}/videos/remove/{video_id}")
def admin_category_video_remove(request: Request, cat_id: int, video_id: str):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    with closing(db_connect()) as conn:
        conn.execute(
            "DELETE FROM video_categories WHERE video_id = ? AND category_id = ?",
            (video_id, cat_id),
        )
        conn.commit()

    set_flash(request, "success", "Video removed from category.")
    return RedirectResponse(f"/admin/categories/{int(cat_id)}/videos", status_code=303)


# ---------------------------------------------------------------------------
# Admin: series
# ---------------------------------------------------------------------------

@app.get("/admin/series")
def admin_series_page(request: Request):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    with closing(db_connect()) as conn:
        series_list = conn.execute(
            "SELECT * FROM series ORDER BY display_order, name"
        ).fetchall()
    return templates.TemplateResponse(
        request, "admin_series.html", base_context(request, series_list=series_list)
    )


@app.post("/admin/series/create")
def admin_series_create(
    request: Request,
    name: str = Form(...),
    display_order: int = Form(0),
):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/series/{series_id}/update")
def admin_series_update(
    request: Request,
    series_id: int,
    name: str = Form(...),
    display_order: int = Form(0),
):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/series/{series_id}/delete")
def admin_series_delete(request: Request, series_id: int):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    with closing(db_connect()) as conn:
        conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
        conn.commit()

    set_flash(request, "success", "Series deleted.")
    return RedirectResponse("/admin/series", status_code=303)


@app.get("/admin/series/{series_id}/videos")
def admin_series_videos_page(request: Request, series_id: int):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/series/{series_id}/videos/add")
def admin_series_video_add(
    request: Request,
    series_id: int,
    video_id: str = Form(...),
    series_order: str = Form(""),
):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/series/{series_id}/videos/update/{video_id}")
def admin_series_video_update_order(
    request: Request,
    series_id: int,
    video_id: str,
    series_order: str = Form(""),
):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

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


@app.post("/admin/series/{series_id}/videos/remove/{video_id}")
def admin_series_video_remove(request: Request, series_id: int, video_id: str):
    try:
        require_admin(request)
    except PermissionError:
        return RedirectResponse("/login", status_code=303)

    with closing(db_connect()) as conn:
        conn.execute(
            "DELETE FROM video_series WHERE video_id = ? AND series_id = ?",
            (video_id, series_id),
        )
        conn.commit()

    set_flash(request, "success", "Video removed from series.")
    return RedirectResponse(f"/admin/series/{int(series_id)}/videos", status_code=303)
