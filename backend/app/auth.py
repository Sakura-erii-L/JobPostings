from __future__ import annotations

import json
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import config
from .db import connect, one, utc_now
from .security import hash_value, random_code, token


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_after(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _send_email(to_email: str, subject: str, body: str) -> None:
    row = one("SELECT value_json FROM system_settings WHERE key='smtp'")
    smtp = json.loads(row["value_json"]) if row else {"enabled": False}
    if not smtp.get("enabled"):
        if config.app_env == "development":
            print(f"[JobPostings DEV OTP] {to_email}: {body}")
            return
        raise RuntimeError("SMTP is not configured")
    message = EmailMessage()
    message["From"] = smtp["from_email"]
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587)), timeout=20) as server:
        if smtp.get("starttls", True):
            server.starttls(context=context)
        if smtp.get("username"):
            from .security import SecretVault

            password = smtp.get("password") or (SecretVault().decrypt(smtp["password_enc"]) if smtp.get("password_enc") else "")
            server.login(smtp["username"], password)
        server.send_message(message)


def local_bootstrap_allowed(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def current_user_from_request(request: Request) -> dict[str, Any] | None:
    session_token = request.cookies.get("jp_session")
    if session_token:
        row = one(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
            (hash_value(session_token), utc_now()),
        )
        if row:
            return dict(row)
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
        row = one(
            "SELECT u.* FROM api_tokens t JOIN users u ON u.id=t.user_id "
            "WHERE t.token_hash=? AND t.revoked_at IS NULL AND t.expires_at>? AND u.active=1",
            (hash_value(bearer), utc_now()),
        )
        if row:
            return dict(row)
    return None


def require_user(request: Request) -> dict[str, Any]:
    user = current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required")
    return user


def require_scope(scope: str):
    def dependency(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        auth = request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return user
        row = one("SELECT scopes_json FROM api_tokens WHERE token_hash=?", (hash_value(auth[7:].strip()),))
        if not row or scope not in json.loads(row["scopes_json"]):
            raise HTTPException(status_code=403, detail=f"Missing scope: {scope}")
        return user

    return dependency


def create_session(user_id: str) -> str:
    value = token()
    now = utc_now()
    with connect() as connection:
        connection.execute(
            "INSERT INTO sessions(id,user_id,token_hash,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            (str(uuid4()), user_id, hash_value(value), iso_after(60 * 24 * 7), now, now),
        )
    return value


def request_code(email: str) -> dict[str, Any]:
    email = email.strip().lower()
    user = one("SELECT id FROM users WHERE email=? AND active=1", (email,))
    invitation = one(
        "SELECT id FROM invitations WHERE email=? AND used_at IS NULL AND expires_at>?",
        (email, utc_now()),
    )
    if not user and not invitation:
        raise HTTPException(status_code=404, detail="This email has not been invited")
    recent = one(
        "SELECT sent_at FROM otp_challenges WHERE email=? ORDER BY sent_at DESC LIMIT 1", (email,)
    )
    if recent and (datetime.now(timezone.utc) - parse_time(recent["sent_at"])).total_seconds() < 60:
        raise HTTPException(status_code=429, detail="Please wait before requesting another code")
    code = random_code()
    challenge_id = str(uuid4())
    with connect() as connection:
        connection.execute(
            "INSERT INTO otp_challenges(id,email,code_hash,expires_at,sent_at) VALUES(?,?,?,?,?)",
            (challenge_id, email, hash_value(code), iso_after(10), utc_now()),
        )
    _send_email(email, "JobPostings 登录验证码", f"你的 JobPostings 登录验证码是：{code}\n10 分钟内有效。")
    response: dict[str, Any] = {"challenge_id": challenge_id, "expires_in_seconds": 600}
    if config.dev_show_otp and config.app_env == "development":
        response["debug_code"] = code
    return response


def verify_code(challenge_id: str, code: str) -> tuple[dict[str, Any], str]:
    row = one("SELECT * FROM otp_challenges WHERE id=?", (challenge_id,))
    if not row or row["consumed_at"] or parse_time(row["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Verification code expired")
    if row["attempts"] >= 5:
        raise HTTPException(status_code=429, detail="Too many verification attempts")
    with connect() as connection:
        connection.execute("UPDATE otp_challenges SET attempts=attempts+1 WHERE id=?", (challenge_id,))
    if hash_value(code.strip()) != row["code_hash"]:
        raise HTTPException(status_code=400, detail="Invalid verification code")
    user = one("SELECT * FROM users WHERE email=? AND active=1", (row["email"],))
    if not user:
        invitation = one(
            "SELECT * FROM invitations WHERE email=? AND used_at IS NULL AND expires_at>?",
            (row["email"], utc_now()),
        )
        if not invitation:
            raise HTTPException(status_code=404, detail="Invitation not found")
        user_id = str(uuid4())
        with connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,role,created_at) VALUES(?,?,?,?)",
                (user_id, row["email"], invitation["role"], utc_now()),
            )
            connection.execute("UPDATE invitations SET used_at=? WHERE id=?", (utc_now(), invitation["id"]))
        user = one("SELECT * FROM users WHERE id=?", (user_id,))
    with connect() as connection:
        connection.execute("UPDATE otp_challenges SET consumed_at=? WHERE id=?", (utc_now(), challenge_id))
        connection.execute("UPDATE users SET last_login_at=? WHERE id=?", (utc_now(), user["id"]))
    return dict(user), create_session(user["id"])
