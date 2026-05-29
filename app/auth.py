import sqlite3
from contextlib import closing

import bcrypt
from fastapi import HTTPException, Request

from database import db_connect


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


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


def current_user(request: Request) -> sqlite3.Row:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def current_admin(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user
