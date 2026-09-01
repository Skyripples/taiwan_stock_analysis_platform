"""Password authentication, sessions, and administrator-managed feature access."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash

from api.db import pool

PASSWORDS = PasswordHash.recommended()
DUMMY_HASH = PASSWORDS.hash("not-a-real-password-value")
SESSION_TTL = int(os.environ.get("AUTH_SESSION_TTL_SECONDS", "900"))
FEATURE_KEYS = (
    "calendar", "prediction", "market_overview",
    "chips_analysis", "backtest", "stock_analysis",
)


class AuthError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message); self.code = code; self.status = status


def _audit(cursor, user_id, event: str, success: bool) -> None:
    cursor.execute("INSERT INTO auth_audit_log(user_id,event,success) VALUES (%s,%s,%s)", (user_id, event, success))


def _permissions(cursor, user_id, role: str) -> dict[str, bool]:
    if role == "admin": return {key: True for key in FEATURE_KEYS}
    cursor.execute("SELECT feature_key,allowed FROM auth_feature_permissions WHERE user_id=%s", (user_id,))
    saved = {row["feature_key"]: row["allowed"] for row in cursor.fetchall()}
    return {key: bool(saved.get(key, False)) for key in FEATURE_KEYS}


def _session_row(cursor, token: str):
    digest = hashlib.sha256(token.encode()).digest()
    cursor.execute("""SELECT u.user_id,u.username,u.role,s.expires_at
                      FROM auth_sessions s JOIN auth_users u USING(user_id)
                      WHERE token_hash=%s AND s.revoked_at IS NULL
                        AND s.expires_at>now() AND u.is_active""", (digest,))
    row = cursor.fetchone()
    if not row: raise AuthError("SESSION_EXPIRED", "登入已逾時")
    cursor.execute("UPDATE auth_sessions SET last_seen_at=now() WHERE token_hash=%s", (digest,))
    return row


def _issue_session(cursor, user) -> dict:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL)
    cursor.execute("INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES (%s,%s,%s)",
                   (hashlib.sha256(token.encode()).digest(), user["user_id"], expires))
    return {
        "access_token": token, "token_type": "bearer", "expires_at": expires.isoformat(),
        "user": {"username": user["username"], "role": user["role"]},
        "permissions": _permissions(cursor, user["user_id"], user["role"]),
    }


def login(username: str, password: str) -> dict:
    now = datetime.now(timezone.utc)
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SELECT * FROM auth_users WHERE lower(username)=lower(%s)", (username,))
        user = cursor.fetchone()
        valid = bool(user and user["is_active"] and PASSWORDS.verify(password, user["password_hash"]))
        if not user: PASSWORDS.verify(password, DUMMY_HASH)
        if user and user["locked_until"] and user["locked_until"] > now: valid = False
        if not valid:
            if user:
                attempts = user["failed_attempts"] + 1
                locked = now + timedelta(minutes=15) if attempts >= 5 else None
                cursor.execute("UPDATE auth_users SET failed_attempts=%s,locked_until=%s WHERE user_id=%s", (attempts, locked, user["user_id"]))
                _audit(cursor, user["user_id"], "password_login", False)
            raise AuthError("INVALID_CREDENTIALS", "帳號或密碼錯誤")
        cursor.execute("UPDATE auth_users SET failed_attempts=0,locked_until=NULL WHERE user_id=%s", (user["user_id"],))
        _audit(cursor, user["user_id"], "password_login", True)
        return _issue_session(cursor, user)


def session(token: str) -> dict:
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        row = _session_row(cursor, token)
        return {"username": row["username"], "role": row["role"],
                "expires_at": row["expires_at"].isoformat(),
                "permissions": _permissions(cursor, row["user_id"], row["role"])}


def logout(token: str) -> None:
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute("UPDATE auth_sessions SET revoked_at=now() WHERE token_hash=%s", (hashlib.sha256(token.encode()).digest(),))


def list_users(token: str) -> list[dict]:
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        admin = _session_row(cursor, token)
        if admin["role"] != "admin": raise AuthError("ADMIN_REQUIRED", "需要管理員權限", 403)
        cursor.execute("SELECT user_id,username,role,is_active,created_at FROM auth_users ORDER BY lower(username)")
        return [{
            "username": row["username"], "password": "********", "role": row["role"],
            "active": row["is_active"], "created_at": row["created_at"].isoformat(),
            "permissions": _permissions(cursor, row["user_id"], row["role"]),
        } for row in cursor.fetchall()]


def create_user(token: str, username: str, password: str, permissions: dict[str, bool]) -> dict:
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise AuthError("INVALID_USERNAME", "帳號需為 3～32 個英數字或 . _ -", 400)
    if len(password) < 4 or len(password) > 128:
        raise AuthError("INVALID_PASSWORD", "測試密碼至少需要 4 個字元", 400)
    normalized = {key: bool(permissions.get(key, False)) for key in FEATURE_KEYS}
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        admin = _session_row(cursor, token)
        if admin["role"] != "admin": raise AuthError("ADMIN_REQUIRED", "需要管理員權限", 403)
        cursor.execute("SELECT 1 FROM auth_users WHERE lower(username)=lower(%s)", (username,))
        if cursor.fetchone(): raise AuthError("USERNAME_EXISTS", "帳號已存在", 409)
        user_id = uuid.uuid4()
        cursor.execute("INSERT INTO auth_users(user_id,username,password_hash,role) VALUES (%s,%s,%s,'user')",
                       (user_id, username, PASSWORDS.hash(password)))
        cursor.executemany("INSERT INTO auth_feature_permissions(user_id,feature_key,allowed) VALUES (%s,%s,%s)",
                           [(user_id, key, value) for key, value in normalized.items()])
        _audit(cursor, admin["user_id"], "user_created", True)
        return {"username": username, "password": "********", "role": "user", "active": True, "permissions": normalized}


def update_permissions(token: str, username: str, permissions: dict[str, bool]) -> dict:
    normalized = {key: bool(permissions.get(key, False)) for key in FEATURE_KEYS}
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        admin = _session_row(cursor, token)
        if admin["role"] != "admin": raise AuthError("ADMIN_REQUIRED", "需要管理員權限", 403)
        cursor.execute("SELECT user_id,role FROM auth_users WHERE lower(username)=lower(%s)", (username,))
        user = cursor.fetchone()
        if not user: raise AuthError("USER_NOT_FOUND", "找不到使用者", 404)
        if user["role"] == "admin": raise AuthError("ADMIN_PERMISSIONS_FIXED", "管理員功能固定開放", 400)
        cursor.executemany("""INSERT INTO auth_feature_permissions(user_id,feature_key,allowed)
                              VALUES (%s,%s,%s) ON CONFLICT(user_id,feature_key)
                              DO UPDATE SET allowed=excluded.allowed,updated_at=now()""",
                           [(user["user_id"], key, value) for key, value in normalized.items()])
        _audit(cursor, admin["user_id"], "permissions_updated", True)
        return {"username": username, "permissions": normalized}
