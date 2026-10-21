from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, Response, status

SESSION_COOKIE = "lead_session"
CSRF_COOKIE = "lead_csrf"
SESSION_SECONDS = 60 * 60 * 24 * 7
password_hasher = PasswordHasher()
DUMMY_PASSWORD_HASH = password_hasher.hash("constant dummy credential used for timing equalization")


def hash_token(token: str) -> str:
    secret = os.getenv("APP_SESSION_SECRET", "development-only-change-me")
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def password_hash(password: str) -> str:
    if len(password) < 10 or len(password) > 256:
        raise HTTPException(status_code=422, detail="Password must be between 10 and 256 characters.")
    return password_hasher.hash(password)


def password_matches(encoded: str, password: str) -> bool:
    try:
        return password_hasher.verify(encoded, password)
    except VerifyMismatchError:
        return False


def set_session_cookies(response: Response, session_token: str, csrf_token: str, secure: bool) -> None:
    response.set_cookie(SESSION_COOKIE, session_token, max_age=SESSION_SECONDS, httponly=True,
                        secure=secure, samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, csrf_token, max_age=SESSION_SECONDS, httponly=False,
                        secure=secure, samesite="lax", path="/")


def clear_session_cookies(response: Response, secure: bool) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, httponly=False, samesite="lax")


def validate_csrf(request: Request) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    supplied = request.headers.get("X-CSRF-Token")
    if not cookie or not supplied or not secrets.compare_digest(cookie, supplied):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")


async def current_user(request: Request) -> dict[str, Any]:
    session = request.cookies.get(SESSION_COOKIE)
    store = request.app.state.auth_store
    user = await store.get_user_for_session(hash_token(session)) if session else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required.")
    request.state.user = user
    return user


async def send_auth_email(request: Request, email: str, subject: str, token: str, purpose: str) -> None:
    """Send a single-use auth link. In local development, print the link to backend logs."""
    import os

    frontend = os.getenv("FRONTEND_PUBLIC_URL", "http://localhost:3000").rstrip("/")
    link = f"{frontend}/?{purpose}_token={token}"
    host = os.getenv("SMTP_HOST")
    if not host:
        if os.getenv("APP_ENV", "development").lower() == "production":
            raise HTTPException(status_code=503, detail="Email delivery is not configured.")
        print(f"[{purpose} link for {email}] {link}")
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.getenv("SMTP_FROM", "Lead Research <noreply@localhost>")
    message["To"] = email
    message.set_content(f"Complete this action using the link below. It expires in 30 minutes.\n\n{link}")
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.starttls()
        username = os.getenv("SMTP_USERNAME")
        if username:
            smtp.login(username, os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(message)


async def issue_session(store: Any, user_id: str, response: Response, secure: bool) -> None:
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    await store.create_session(user_id, hash_token(token), datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS))
    set_session_cookies(response, token, csrf, secure)


def new_user_id() -> str:
    return str(uuid4())
