"""Admin sessions: one password, one signed cookie.

The listener side is public on purpose. Everything that writes needs the cookie,
and the only secret is SPICA_ADMIN_PASSWORD from the environment; the signing key
is generated once and kept in the state directory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request

from . import config

COOKIE = "radio_admin"


def _secret_file() -> Path:
    return config.SESSION_SECRET_FILE


def _secret() -> bytes:
    path = _secret_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip().encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def issue() -> str:
    expiry = int(time.time()) + config.SESSION_HOURS * 3600
    payload = f"admin|{expiry}"
    return f"{payload}|{_sign(payload)}"


def valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        kind, expiry, sig = token.rsplit("|", 2)
    except ValueError:
        return False
    if kind != "admin" or not _sign(f"{kind}|{expiry}") == sig:
        return False
    try:
        return int(expiry) > int(time.time())
    except ValueError:
        return False


def check_password(candidate: str) -> bool:
    expected = config.ADMIN_PASSWORD
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def is_admin(request: Request) -> bool:
    return valid(request.cookies.get(COOKIE))


def require_admin(request: Request) -> None:
    """Dependency for write endpoints: 401 instead of leaking a redirect to htmx."""
    if not is_admin(request):
        raise HTTPException(401, "admin login required")
