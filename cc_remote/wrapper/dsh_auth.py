"""DSH 0.1.2 browser-session cookie generation for the wrapper's loopback client.

DSH 0.1.2 (dsh-client-connection) requires every /api request to carry a
signed browser-session cookie.  The signing secret is persisted by DSH in
``~/.dsh/.credentials.yaml`` under the ``client-connection/browser-session``
grant; the wrapper reads it and mints its own cookie for the loopback
authority so it can talk to DSH without a human browser exchange.

Cookie format (mirrors @deepseek-ai/dsh-client-connection):
    name  = "dsh-auth-" + base64url(sha256(authority))
    value = "v1." + base64url(json{version:1,authority,issuedAt,expiresAt})
                 + "." + base64url(hmac_sha256(secret, body))
authority is the Host header value (e.g. "127.0.0.1:3080").
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

_COOKIE_PREFIX = "dsh-auth-"
_COOKIE_PAYLOAD_VERSION = 1
_SECRET_BYTES = 32
_CREDENTIAL_PATH = Path.home() / ".dsh" / ".credentials.yaml"
_GRANT_KEY = "client-connection/browser-session"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _canonical_secret(value: object) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = _b64url_decode(value)
    except (ValueError, TypeError):
        return None
    if len(decoded) != _SECRET_BYTES:
        return None
    return decoded


def load_dsh_browser_secret(
    path: Path | None = None,
) -> bytes | None:
    """Read the DSH browser-session HMAC secret from credentials.yaml."""
    credentials_path = path or _CREDENTIAL_PATH
    try:
        text = credentials_path.read_text(encoding="utf-8")
    except OSError:
        return None
    # Minimal YAML subset: find `secret: <token>` inside the grant payload.
    # The file is DSH-owned; a full YAML parser would add a dependency for a
    # structure this flat.  Guard against reading a secret from another grant.
    grant_block = text.split(_GRANT_KEY, 1)
    if len(grant_block) != 2:
        return None
    after_key = grant_block[1]
    match = re.search(r"secret:\s*(\S+)", after_key)
    if match is None:
        return None
    return _canonical_secret(match.group(1))


def make_dsh_browser_cookie(
    authority: str,
    secret: bytes | None = None,
    *,
    now_ms: int | None = None,
    ttl_days: int = 30,
) -> str | None:
    """Build the ``dsh-auth-...=v1....`` cookie for one Host authority."""
    if secret is None:
        secret = load_dsh_browser_secret()
        if secret is None:
            return None
    current = int(time.time() * 1000) if now_ms is None else now_ms
    expires = current + ttl_days * 86400 * 1000
    body_json = json.dumps(
        {
            "version": _COOKIE_PAYLOAD_VERSION,
            "authority": authority,
            "issuedAt": current,
            "expiresAt": expires,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    body = _b64url_encode(body_json)
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    name = _COOKIE_PREFIX + _b64url_encode(
        hashlib.sha256(authority.encode("utf-8")).digest()
    )
    value = f"v1.{body}.{_b64url_encode(signature)}"
    return f"{name}={value}"


def dsh_credentials_hint() -> str:
    """Short description used in logs when the credential file is missing."""
    explicit = os.environ.get("DSH_CREDENTIALS_PATH")
    if explicit:
        return explicit
    return str(_CREDENTIAL_PATH)
