"""자체 로그인 — 서명 쿠키 24h, 실패 5회 5분 잠금 (SPEC-04 §1).

운영 서버의 OPS 계정과는 완전히 별개 시스템이다 (같은 값이어도 분리).
단일 사용자(원장님) 전제 — 잠금 카운터는 전역 1개면 충분하다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time

from fastapi import HTTPException, Request

from .config import get_config

COOKIE_NAME = "lev_session"
SESSION_HOURS = 24
MAX_FAILS = 5
LOCK_SEC = 300

_lock = threading.Lock()
_fails = 0
_locked_until = 0.0


def _sign(payload: str) -> str:
    key = get_config().secret_key.encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def issue_cookie(user: str) -> str:
    exp = int(time.time()) + SESSION_HOURS * 3600
    payload = base64.urlsafe_b64encode(f"{user}|{exp}".encode()).decode()
    return f"{payload}.{_sign(payload)}"


def verify_cookie(value: str | None) -> str | None:
    if not value or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        user, exp = base64.urlsafe_b64decode(payload.encode()).decode().split("|")
    except Exception:
        return None
    if int(exp) < time.time():
        return None
    return user


def lock_remaining() -> int:
    with _lock:
        return max(0, int(_locked_until - time.time()))


def try_login(user_id: str, password: str) -> tuple[bool, str]:
    """→ (성공 여부, 실패 시 사용자 안내 문구)"""
    global _fails, _locked_until
    cfg = get_config()
    with _lock:
        remain = int(_locked_until - time.time())
        if remain > 0:
            return False, f"Login is locked. Please try again in {remain}s."
        ok = hmac.compare_digest(user_id, cfg.admin_id) and hmac.compare_digest(password, cfg.admin_pw)
        if ok:
            _fails = 0
            return True, ""
        _fails += 1
        if _fails >= MAX_FAILS:
            _locked_until = time.time() + LOCK_SEC
            _fails = 0
            return False, f"{MAX_FAILS} failures — locking for {LOCK_SEC // 60} min."
        return False, f"Wrong username or password. (failure {_fails}/{MAX_FAILS})"


def reset_lock() -> None:
    """테스트 전용."""
    global _fails, _locked_until
    with _lock:
        _fails = 0
        _locked_until = 0.0


def current_user(request: Request) -> str | None:
    return verify_cookie(request.cookies.get(COOKIE_NAME))


def require_api_auth(request: Request) -> str:
    """/api/* 의존성 — 미인증 401 (프론트 api.js 가 /login 으로 보냄)."""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Login required.")
    return user
