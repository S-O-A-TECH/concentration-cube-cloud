"""nonce 재사용 방지 저장소 — 5분 창 (SPEC-02 §2.1).

운영: Redis SET NX EX. Redis 미가동 개발·단위테스트: 인메모리(단일 프로세스 한정).
"""
from __future__ import annotations

import time
from typing import Protocol

import redis as redis_lib


class NonceStore(Protocol):
    def check_and_store(self, nonce: str, ttl_sec: int) -> bool:
        """처음 보는 nonce 면 저장 후 True, 재사용이면 False."""
        ...


class MemoryNonceStore:
    """개발·테스트용 — 프로세스 로컬. 운영은 반드시 Redis."""

    def __init__(self):
        self._seen: dict[str, float] = {}

    def check_and_store(self, nonce: str, ttl_sec: int) -> bool:
        now = time.monotonic()
        self._seen = {k: exp for k, exp in self._seen.items() if exp > now}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now + ttl_sec
        return True


class RedisNonceStore:
    def __init__(self, url: str):
        self._client = redis_lib.Redis.from_url(url, socket_connect_timeout=2,
                                                socket_timeout=2)

    def check_and_store(self, nonce: str, ttl_sec: int) -> bool:
        return bool(self._client.set(f"nonce:{nonce}", 1, nx=True, ex=ttl_sec))
