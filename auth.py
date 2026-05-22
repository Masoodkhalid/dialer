"""
HMAC-SHA256 token authentication — no external dependencies.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = uuid.uuid4().hex
    msg = f"{salt}:{password}".encode()
    h = hmac.new(settings.AUTH_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()
    return f"{salt}:{h}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        msg = f"{salt}:{password}".encode()
        expected = hmac.new(settings.AUTH_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(h, expected)
    except Exception:
        return False


def create_token(user_id: str, role: str, username: str) -> str:
    payload = {
        "sub": user_id,
        "role": role,
        "username": username,
        "exp": int(time.time()) + 60 * 60 * 24 * 7,  # 7 days
    }
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(settings.AUTH_SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def decode_token(token: str) -> Optional[dict]:
    try:
        data, sig = token.rsplit(".", 1)
        expected = hmac.new(settings.AUTH_SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = data + "=" * (4 - len(data) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def get_payload(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    payload = decode_token(creds.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return payload


def require_admin(payload: dict = Depends(get_payload)) -> dict:
    if payload.get("role") != "superadmin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Superadmin access required")
    return payload


def require_any(payload: dict = Depends(get_payload)) -> dict:
    return payload
