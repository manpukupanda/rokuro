import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from config import DB_PATH, INITIAL_ADMIN_ACCOUNT, INITIAL_ADMIN_PASSWORD


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_metadata (
                video_id TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                display_order INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def seed_initial_admin() -> None:
    from auth import hash_password

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
