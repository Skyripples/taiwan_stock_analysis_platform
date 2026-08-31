"""Password + WebAuthn authentication service."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash
from webauthn import (
    generate_authentication_options, generate_registration_options,
    options_to_json, verify_authentication_response, verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment, AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor, ResidentKeyRequirement, UserVerificationRequirement,
)

from api.db import pool

PASSWORDS = PasswordHash.recommended()
DUMMY_HASH = PASSWORDS.hash("not-a-real-password-value")
RP_ID = os.environ.get("AUTH_WEBAUTHN_RP_ID", "skyripples.github.io")
ORIGIN = os.environ.get("AUTH_WEBAUTHN_ORIGIN", "https://skyripples.github.io")
CHALLENGE_TTL = int(os.environ.get("AUTH_CHALLENGE_TTL_SECONDS", "300"))
SESSION_TTL = int(os.environ.get("AUTH_SESSION_TTL_SECONDS", "900"))


class AuthError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message); self.code = code; self.status = status


def _audit(cursor, user_id, event: str, success: bool) -> None:
    cursor.execute("INSERT INTO auth_audit_log(user_id,event,success) VALUES (%s,%s,%s)", (user_id, event, success))


def begin(username: str, password: str) -> dict:
    now = datetime.now(timezone.utc)
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SELECT * FROM auth_users WHERE lower(username)=lower(%s)", (username,))
        user = cursor.fetchone()
        valid = bool(user and user["is_active"] and PASSWORDS.verify(password, user["password_hash"]))
        if not user:
            PASSWORDS.verify(password, DUMMY_HASH)
        if user and user["locked_until"] and user["locked_until"] > now:
            valid = False
        if not valid:
            if user:
                attempts = user["failed_attempts"] + 1
                locked = now + timedelta(minutes=15) if attempts >= 5 else None
                cursor.execute("UPDATE auth_users SET failed_attempts=%s, locked_until=%s WHERE user_id=%s", (attempts, locked, user["user_id"]))
                _audit(cursor, user["user_id"], "password", False)
            raise AuthError("INVALID_CREDENTIALS", "帳號或密碼錯誤")
        cursor.execute("UPDATE auth_users SET failed_attempts=0, locked_until=NULL WHERE user_id=%s", (user["user_id"],))
        cursor.execute("SELECT * FROM auth_webauthn_credentials WHERE user_id=%s ORDER BY created_at LIMIT 1", (user["user_id"],))
        credential = cursor.fetchone()
        ceremony = "authentication" if credential else "registration"
        challenge = secrets.token_bytes(32); flow_id = uuid.uuid4()
        cursor.execute("DELETE FROM auth_challenges WHERE expires_at < now() OR consumed_at IS NOT NULL")
        cursor.execute("INSERT INTO auth_challenges(flow_id,user_id,ceremony,challenge,expires_at) VALUES (%s,%s,%s,%s,%s)",
                       (flow_id, user["user_id"], ceremony, challenge, now + timedelta(seconds=CHALLENGE_TTL)))
        if credential:
            options = generate_authentication_options(
                rp_id=RP_ID, challenge=challenge,
                allow_credentials=[PublicKeyCredentialDescriptor(id=bytes(credential["credential_id"]))],
                user_verification=UserVerificationRequirement.REQUIRED,
            )
        else:
            options = generate_registration_options(
                rp_id=RP_ID, rp_name="台股投資分析平台", user_name=user["username"],
                user_display_name=user["username"], user_id=user["user_id"].bytes,
                challenge=challenge,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
            )
        _audit(cursor, user["user_id"], f"{ceremony}_begin", True)
        return {"flow_id": str(flow_id), "ceremony": ceremony, "public_key": json.loads(options_to_json(options))}


def complete(flow_id: str, credential: dict) -> dict:
    try: flow_uuid = uuid.UUID(flow_id)
    except ValueError as exc: raise AuthError("INVALID_FLOW", "登入流程無效", 400) from exc
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SELECT c.*,u.username,u.role FROM auth_challenges c JOIN auth_users u USING(user_id) WHERE flow_id=%s FOR UPDATE", (flow_uuid,))
        flow = cursor.fetchone()
        if not flow or flow["consumed_at"] or flow["expires_at"] <= datetime.now(timezone.utc):
            raise AuthError("EXPIRED_FLOW", "驗證已逾時，請重新登入", 400)
        try:
            if flow["ceremony"] == "registration":
                cursor.execute("SELECT count(*) AS count FROM auth_webauthn_credentials WHERE user_id=%s", (flow["user_id"],))
                if cursor.fetchone()["count"]:
                    raise AuthError("DEVICE_ALREADY_BOUND", "此帳號已綁定裝置", 409)
                result = verify_registration_response(credential=credential, expected_challenge=bytes(flow["challenge"]), expected_rp_id=RP_ID, expected_origin=ORIGIN, require_user_verification=True)
                if result.credential_device_type.value != "single_device" or result.credential_backed_up:
                    raise AuthError("DEVICE_BOUND_REQUIRED", "請使用未同步至雲端的本機裝置 Passkey", 400)
                transports = ((credential.get("response") or {}).get("transports") or [])
                cursor.execute("INSERT INTO auth_webauthn_credentials(credential_id,user_id,public_key,sign_count,transports,device_type,backed_up) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                               (result.credential_id, flow["user_id"], result.credential_public_key, result.sign_count, transports, str(result.credential_device_type.value), result.credential_backed_up))
            else:
                raw_id = credential.get("id") or ""
                from webauthn.helpers import base64url_to_bytes
                credential_id = base64url_to_bytes(raw_id)
                cursor.execute("SELECT * FROM auth_webauthn_credentials WHERE credential_id=%s AND user_id=%s", (credential_id, flow["user_id"]))
                saved = cursor.fetchone()
                if not saved: raise AuthError("UNKNOWN_CREDENTIAL", "找不到此裝置憑證")
                result = verify_authentication_response(credential=credential, expected_challenge=bytes(flow["challenge"]), expected_rp_id=RP_ID, expected_origin=ORIGIN, credential_public_key=bytes(saved["public_key"]), credential_current_sign_count=saved["sign_count"], require_user_verification=True)
                cursor.execute("UPDATE auth_webauthn_credentials SET sign_count=%s,last_used_at=now() WHERE credential_id=%s", (result.new_sign_count, credential_id))
        except AuthError: raise
        except Exception as exc:
            _audit(cursor, flow["user_id"], f"{flow['ceremony']}_complete", False)
            raise AuthError("PASSKEY_FAILED", "Passkey 驗證失敗") from exc
        cursor.execute("UPDATE auth_challenges SET consumed_at=now() WHERE flow_id=%s", (flow_uuid,))
        token = secrets.token_urlsafe(32); token_hash = hashlib.sha256(token.encode()).digest()
        expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL)
        cursor.execute("INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES (%s,%s,%s)", (token_hash, flow["user_id"], expires))
        _audit(cursor, flow["user_id"], "login", True)
        return {"access_token": token, "token_type": "bearer", "expires_at": expires.isoformat(), "user": {"username": flow["username"], "role": flow["role"]}, "device_bound": True}


def session(token: str) -> dict:
    digest = hashlib.sha256(token.encode()).digest()
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SELECT u.username,u.role,s.expires_at FROM auth_sessions s JOIN auth_users u USING(user_id) WHERE token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>now() AND u.is_active", (digest,))
        row = cursor.fetchone()
        if not row: raise AuthError("SESSION_EXPIRED", "登入已逾時")
        cursor.execute("UPDATE auth_sessions SET last_seen_at=now() WHERE token_hash=%s", (digest,))
        return {"username": row["username"], "role": row["role"], "expires_at": row["expires_at"].isoformat()}


def logout(token: str) -> None:
    with pool.connection() as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute("UPDATE auth_sessions SET revoked_at=now() WHERE token_hash=%s", (hashlib.sha256(token.encode()).digest(),))
