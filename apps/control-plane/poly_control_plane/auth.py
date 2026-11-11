"""Cookie auth + password hashing.

Reuses the existing `data/.secret` Fernet key (see ``secret.py``) for cookie
tokens, so no new secret file and no new dependency. Passwords are pbkdf2-hashed
with stdlib only (``hashlib.pbkdf2_hmac``).

Token = Fernet-encrypted JSON ``{"u": username, "exp": ts}``. The cookie is
HttpOnly + SameSite=Strict. ``secure`` is intentionally False: the control plane
serves plain HTTP on the LAN (``--host 0.0.0.0``), and a Secure cookie would
not be sent over HTTP, breaking login. Flip to True if HTTPS is ever fronted.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import secrets
import time
from base64 import b64decode, b64encode

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from . import secret as secret_store
from .db import UserRow, get_session
from .system import data_dir

COOKIE_NAME = "poly_auth"
COOKIE_MAX_AGE = 8 * 3600  # 8 hours; re-login after that

# Set by the auth middleware, read by `_audit` so call sites don't each have
# to thread the username through. Starlette copies contextvars into sync route
# handlers run on the threadpool, so this works for `def` routes too.
_current_user: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user", default="local"
)

PBKDF2_ITERATIONS = 200_000
DEFAULT_PASSWORD = "123456"


# --------------------------------------------------------------------- passwords
def hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${b64encode(salt).decode()}${b64encode(dk).decode()}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = b64decode(salt_b64)
        expected = b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# --------------------------------------------------------------------- cookie token
def _make_token(username: str) -> str:
    payload = json.dumps({"u": username, "exp": int(time.time()) + COOKIE_MAX_AGE})
    return secret_store.encrypt_secret(data_dir(), payload)


def _parse_token(token: str) -> str | None:
    if not token:
        return None
    try:
        payload = secret_store.decrypt_secret(data_dir(), token)
        if not payload:
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data.get("u")
    except Exception:
        return None


def login_response(username: str) -> Response:
    resp = Response(status_code=204)
    resp.set_cookie(
        COOKIE_NAME,
        _make_token(username),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=False,
    )
    return resp


def logout_response() -> Response:
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --------------------------------------------------------------------- deps
def current_user(request: Request, session: Session = Depends(get_session)) -> UserRow:
    token = request.cookies.get(COOKIE_NAME)
    username = _parse_token(token)
    if username is None:
        raise HTTPException(status_code=401, detail="未登录")
    row = session.exec(select(UserRow).where(UserRow.username == username)).first()
    if row is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return row


def require_admin(user: UserRow = Depends(current_user)) -> UserRow:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def user_access(row: UserRow) -> dict[str, object]:
    """Safe projection for the API/UI: never expose the password hash."""
    return {
        "id": row.id,
        "username": row.username,
        "is_admin": row.is_admin,
        "must_change_password": row.must_change_password,
        "created_at": row.created_at.isoformat(),
    }


# --------------------------------------------------------------------- request bodies
class LoginBody(BaseModel):
    username: str
    password: str


class UserCreateBody(BaseModel):
    username: str


class PasswordChangeBody(BaseModel):
    password: str
