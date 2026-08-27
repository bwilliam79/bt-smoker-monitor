"""Public-host session login for smoker.tehkernel.com. LAN :8888 stays open."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from http.cookies import SimpleCookie

PUBLIC_HOST = 'smoker.tehkernel.com'
COOKIE_NAME = 'sid'
SESSION_TTL_SECS = 7 * 24 * 3600
PBKDF2_ROUNDS = 200_000
PBKDF2_DKLEN = 32

_sessions: dict[str, float] = {}


def request_host(host_header: str | None) -> str:
    raw = (host_header or '').split(',')[0].strip().lower()
    if not raw:
        return ''
    if raw.startswith('['):
        end = raw.find(']')
        return raw[1:end] if end > 0 else raw
    return raw.split(':')[0]


def host_is_public(host_header: str | None) -> bool:
    return request_host(host_header) == PUBLIC_HOST


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ROUNDS, dklen=PBKDF2_DKLEN)
    return salt.hex(), dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ROUNDS, dklen=PBKDF2_DKLEN)
    return hmac.compare_digest(dk, expected)


def parse_sid(cookie_header: str | None) -> str:
    if not cookie_header:
        return ''
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return ''
    morsel = jar.get(COOKIE_NAME)
    return (morsel.value or '').strip() if morsel else ''


def mint_session() -> str:
    sid = secrets.token_hex(16)
    _sessions[sid] = time.time() + SESSION_TTL_SECS
    return sid


def session_ok(cookie_header: str | None) -> bool:
    sid = parse_sid(cookie_header)
    if not sid:
        return False
    exp = _sessions.get(sid)
    if not exp:
        return False
    if exp < time.time():
        _sessions.pop(sid, None)
        return False
    return True


def drop_session(cookie_header: str | None) -> None:
    sid = parse_sid(cookie_header)
    if sid:
        _sessions.pop(sid, None)


def cookie_header(sid: str, secure: bool) -> str:
    parts = [
        f'{COOKIE_NAME}={sid}',
        'Path=/',
        'HttpOnly',
        'SameSite=Lax',
        f'Max-Age={SESSION_TTL_SECS}',
    ]
    if secure:
        parts.append('Secure')
    return '; '.join(parts)


def clear_cookie_header(secure: bool) -> str:
    parts = [f'{COOKIE_NAME}=', 'Path=/', 'HttpOnly', 'SameSite=Lax', 'Max-Age=0']
    if secure:
        parts.append('Secure')
    return '; '.join(parts)


class PublicWsGate:
    """Reject unauthenticated public-host WebSocket upgrades with HTTP 401."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'websocket':
            headers = {k.decode('latin1'): v.decode('latin1') for k, v in scope.get('headers', [])}
            if host_is_public(headers.get('host')) and not session_ok(headers.get('cookie')):
                await send({
                    'type': 'websocket.http.response.start',
                    'status': 401,
                    'headers': [(b'content-type', b'application/json')],
                })
                await send({
                    'type': 'websocket.http.response.body',
                    'body': b'{"error":"Unauthorized"}',
                })
                return
        await self.app(scope, receive, send)
