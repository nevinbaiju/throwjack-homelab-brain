"""Username/password for the browser, bearer token for everything else.

Stdlib only — no new dependency for something this small. The password is
stored as a scrypt hash, never in the clear, and the session cookie is signed
with the brain token so a forged one cannot be minted.

NOTE ON TRANSPORT: the brain listens on 0.0.0.0, so on plain HTTP the password
crosses your LAN readable. Put it behind `tailscale serve` (as Radicale already
is) if that matters — the cookie is marked Secure automatically when the
request arrives over HTTPS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

COOKIE = "brain_session"
SESSION_HOURS = int(os.environ.get("BRAIN_SESSION_HOURS", "720"))   # 30 days

SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


# ":" not "$". docker-compose performs variable interpolation on env_file
# values, so a "$" in the hash is silently eaten and the stored hash arrives
# in the container truncated — the password then never verifies. Base64 uses
# A-Za-z0-9+/= so ":" cannot collide with the payload.
SEP = ":"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return SEP.join(("scrypt", base64.b64encode(salt).decode(),
                     base64.b64encode(digest).decode()))


def looks_valid(stored: str) -> bool:
    """Is this a hash we can even compare against? Distinguishes a broken
    config from a wrong password, which otherwise look identical."""
    parts = (stored or "").split(SEP)
    return len(parts) == 3 and parts[0] == "scrypt" and all(parts[1:])


def verify_password(password: str, stored: str) -> bool:
    stored = (stored or "").replace("$", SEP)          # tolerate the old format
    try:
        kind, salt_b64, digest_b64 = stored.split(SEP, 2)
        if kind != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    got = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return hmac.compare_digest(got, expected)


def _sign(payload: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return payload + "." + base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_session(user: str, secret: str) -> str:
    expires = int(time.time()) + SESSION_HOURS * 3600
    return _sign(f"{user}|{expires}", secret)


def read_session(cookie: str | None, secret: str) -> str | None:
    """-> username, or None if missing, tampered with, or expired."""
    if not cookie or "." not in cookie:
        return None
    payload, _, _ = cookie.rpartition(".")
    if not hmac.compare_digest(_sign(payload, secret), cookie):
        return None
    try:
        user, expires = payload.rsplit("|", 1)
        if int(expires) < time.time():
            return None
    except ValueError:
        return None
    return user
