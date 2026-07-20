"""공용 의존성 — DB 세션, 저장소, 기기 JWT 인증, nonce+timestamp 검증 (SPEC-02 §2.1)."""
from __future__ import annotations

import time
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Device
from app.db.session import make_session_factory
from app.services.nonce import MemoryNonceStore, NonceStore, RedisNonceStore
from app.services.storage import SessionStorage

JWT_ALGO = "HS256"
DEVICE_TOKEN_TTL_SEC = 3600
NONCE_WINDOW_SEC = 300


@lru_cache
def _session_factory():
    return make_session_factory()


def get_db() -> Session:
    factory = _session_factory()
    with factory() as db:
        yield db


@lru_cache
def get_storage() -> SessionStorage:
    return SessionStorage(get_settings().storage_root)


# Redis 성공 시에만 고정 캐시 — 폴백을 영구화하지 않는다 (Redis 복구 시 자동 복귀)
_redis_nonce_store: RedisNonceStore | None = None
_memory_nonce_store = MemoryNonceStore()   # 폴백 싱글턴 (프로세스 로컬 — 개발 전용)


def get_nonce_store() -> NonceStore:
    global _redis_nonce_store
    if _redis_nonce_store is not None:
        return _redis_nonce_store
    try:
        store = RedisNonceStore(get_settings().redis_url)
        store.check_and_store("__boot_probe__", 1)
        _redis_nonce_store = store
        return store
    except Exception:
        # Redis 미가동 개발 환경 폴백 — 다음 요청에서 Redis 재시도.
        # 운영(compose)은 항상 Redis: 다중 워커에서 인메모리는 재전송 방지를 못 지킨다.
        return _memory_nonce_store


def make_device_token(device: Device) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(device.id), "serial": device.serial,
         "iat": now, "exp": now + DEVICE_TOKEN_TTL_SEC},
        get_settings().jwt_secret, algorithm=JWT_ALGO,
    )


def require_device(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Device:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = jwt.decode(authorization.removeprefix("Bearer "),
                            get_settings().jwt_secret, algorithms=[JWT_ALGO])
        device_id = int(claims["sub"])          # ops 쿠키 등 비-기기 JWT 는 여기서 걸러짐
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    device = db.execute(
        select(Device).where(Device.id == device_id)
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown device")
    return device


def check_nonce(
    x_nonce: str = Header(default=""),
    x_timestamp: int = Header(default=0),
    store: NonceStore = Depends(get_nonce_store),
) -> None:
    """재전송 공격 방지 — 5분 창 timestamp + nonce 1회성 (SPEC-02 §2.1)."""
    if not x_nonce:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing X-Nonce")
    if abs(time.time() - x_timestamp) > NONCE_WINDOW_SEC:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "timestamp out of window")
    if not store.check_and_store(x_nonce, NONCE_WINDOW_SEC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "nonce reused")
