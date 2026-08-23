"""Optional single password for the whole camera.

Router-admin-page tier by design (DESIGN.md, "Access control"): one shared
secret, no accounts, no OAuth, no API keys. It defends against the rest of the
local network, and it is off by default — a camera on your own LAN that nobody
else touches should not demand a login to look at the sky.

The session is a signed token in a cookie rather than server-side state, so it
survives an api restart and a reboot. Someone who logged in from the shed door
in October should not be logged out because the daemon was updated in November.
"""
from __future__ import annotations

import base64
import hmac
import secrets
import time
from hashlib import sha256

SESSION_COOKIE = "skylapse_session"
SESSION_DAYS = 30
SESSION_SECONDS = SESSION_DAYS * 24 * 3600

# bcrypt truncates silently at 72 bytes, which would make two different long
# passwords equivalent without saying so. Refusing is the honest answer.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_CHARS = 4


class PasswordTooLong(ValueError):
    """Raised rather than letting bcrypt silently ignore the tail."""


def hash_password(plain: str) -> str:
    import bcrypt

    raw = plain.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        raise PasswordTooLong(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time check. A wrong password and an unset one are different
    answers, and only the caller knows which it is asking about."""
    if not hashed:
        return False
    import bcrypt

    try:
        return bcrypt.checkpw(plain.encode()[:MAX_PASSWORD_BYTES], hashed.encode())
    except (ValueError, TypeError):
        return False           # a corrupt hash locks nobody out of a decision


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def issue_token(secret: str, now: float | None = None,
                lifetime: int = SESSION_SECONDS) -> str:
    """A cookie value of `<expiry>.<signature>`.

    Stateless on purpose: nothing to store, nothing to lose on restart, and no
    session table to grow unbounded on a device with a 64 GB card.
    """
    expiry = int((now if now is not None else time.time()) + lifetime)
    return f"{expiry}.{_sign(expiry, secret)}"


def token_valid(token: str, secret: str, now: float | None = None) -> bool:
    if not token or not secret:
        return False
    expiry_text, _, signature = token.partition(".")
    if not signature:
        return False
    try:
        expiry = int(expiry_text)
    except ValueError:
        return False
    # Signature first, then expiry: checking expiry first would let an
    # unsigned token influence the answer before it had earned the right to.
    if not hmac.compare_digest(signature, _sign(expiry, secret)):
        return False
    return expiry > (now if now is not None else time.time())


def _sign(expiry: int, secret: str) -> str:
    mac = hmac.new(secret.encode(), str(expiry).encode(), sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")
