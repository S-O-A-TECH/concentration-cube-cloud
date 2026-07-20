"""GET /health — S0 DoD: db / redis / storage / version 4항목 (S0 구현계획 §1.5)."""
import uuid
from pathlib import Path

import redis as redis_lib
from fastapi import APIRouter
from sqlalchemy import create_engine, text

from app.config import get_settings

router = APIRouter(tags=["health"])


def check_db(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        finally:
            engine.dispose()
    except Exception:
        return False


def check_redis(url: str) -> bool:
    try:
        client = redis_lib.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


def check_storage(root: str) -> bool:
    try:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".health-probe-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


@router.get("/health")
def health() -> dict:
    s = get_settings()
    db = check_db(s.database_url)
    redis_ok = check_redis(s.redis_url)
    storage = check_storage(s.storage_root)
    return {
        "ok": db and redis_ok and storage,
        "db": db,
        "redis": redis_ok,
        "storage": storage,
        "version": s.app_version,
    }
