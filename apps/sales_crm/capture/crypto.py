"""Encrypts OAuth tokens at rest (README §4.2).

Key: CAPTURE_TOKEN_KEY in .env (a Fernet key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").
If it is missing, a key is derived from JWT_SECRET_KEY so development works — but then rotating the
JWT secret makes stored tokens unreadable (users simply reconnect). A warning is printed once.
"""

import base64
import hashlib
import json
import os
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken

_fernet = None
_warned = False


def _get() -> Fernet:
    global _fernet, _warned
    if _fernet is None:
        key = os.getenv("CAPTURE_TOKEN_KEY", "").strip()
        if not key:
            secret = os.getenv("JWT_SECRET_KEY", "")
            if not secret:
                raise RuntimeError("Neither CAPTURE_TOKEN_KEY nor JWT_SECRET_KEY is set")
            key = base64.urlsafe_b64encode(hashlib.sha256(("capture-tokens:" + secret).encode()).digest()).decode()
            if not _warned:
                print("[crm] CAPTURE_TOKEN_KEY not set — deriving the token key from JWT_SECRET_KEY. Set CAPTURE_TOKEN_KEY in production.")
                _warned = True
        _fernet = Fernet(key.encode())
    return _fernet


def seal(data: Dict[str, Any]) -> bytes:
    return _get().encrypt(json.dumps(data).encode())


def unseal(blob: bytes) -> Dict[str, Any]:
    try:
        return json.loads(_get().decrypt(bytes(blob)))
    except InvalidToken:
        raise ValueError("Stored token can't be decrypted (key changed) — reconnect required")


def seal_text(s: str) -> str:
    return _get().encrypt(s.encode()).decode()


def unseal_text(s: str, ttl: int) -> str:
    return _get().decrypt(s.encode(), ttl=ttl).decode()
