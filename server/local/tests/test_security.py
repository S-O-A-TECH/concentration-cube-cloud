"""S7 §1.1 보안 시나리오 — S2/S6 테스트에서 못 다룬 나머지: 만료 JWT, 쿠키 위조,
로그인 잠금(429), 요청 크기 413. (401/nonce/415/권한 교차는 test_ingest·test_ops)"""
import hashlib
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import JWT_ALGO, get_db, get_nonce_store, get_storage
from app.api.ops import _login_fails
from app.config import get_settings
from app.db import models
from app.db.base import Base
from app.main import app
from app.services.nonce import MemoryNonceStore
from app.services.storage import SessionStorage

SERIAL = "SIM_DEV_SEC"
FACTORY_TOKEN = "sim-sec-token"


@pytest.fixture()
def client(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(models.Device(
            serial=SERIAL,
            factory_token_hash=hashlib.sha256(FACTORY_TOKEN.encode()).hexdigest()))
        db.commit()

    def _db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = lambda: SessionStorage(tmp_path)
    app.dependency_overrides[get_nonce_store] = lambda: MemoryNonceStore()
    _login_fails.clear()
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    _login_fails.clear()
    engine.dispose()


def _device_headers(token: str) -> dict:
    import uuid as _uuid
    return {"Authorization": f"Bearer {token}", "X-Nonce": _uuid.uuid4().hex,
            "X-Timestamp": str(int(time.time()))}


def test_expired_device_jwt_401(client):
    now = int(time.time())
    expired = jwt.encode({"sub": "1", "serial": SERIAL,
                          "iat": now - 7200, "exp": now - 3600},
                         get_settings().jwt_secret, algorithm=JWT_ALGO)
    r = client.get("/v1/sessions/00000000-0000-0000-0000-000000000000/status",
                   headers=_device_headers(expired))
    assert r.status_code == 401


def test_wrong_secret_jwt_401(client):
    forged = jwt.encode({"sub": "1", "serial": SERIAL,
                         "iat": int(time.time()), "exp": int(time.time()) + 3600},
                        "attacker-secret", algorithm=JWT_ALGO)
    r = client.get("/v1/sessions/00000000-0000-0000-0000-000000000000/status",
                   headers=_device_headers(forged))
    assert r.status_code == 401


def test_forged_ops_cookie_rejected(client):
    forged = jwt.encode({"sub": "ops", "role": "ops",
                         "iat": int(time.time()), "exp": int(time.time()) + 3600},
                        "attacker-secret", algorithm=JWT_ALGO)
    client.cookies.set("ops_session", forged)
    assert client.get("/v1/ops/dashboard").status_code == 401
    # role 없는 쿠키(기기 JWT 재사용 시도)도 거부
    device_jwt = jwt.encode({"sub": "1", "serial": SERIAL,
                             "iat": int(time.time()), "exp": int(time.time()) + 3600},
                            get_settings().jwt_secret, algorithm=JWT_ALGO)
    client.cookies.set("ops_session", device_jwt)
    assert client.get("/v1/ops/dashboard").status_code == 401


def test_login_lockout_after_5_failures(client):
    for _ in range(5):
        assert client.post("/v1/ops/login",
                           json={"id": "admin", "pw": "wrong"}).status_code == 401
    # 6번째부터는 맞는 비밀번호여도 429
    s = get_settings()
    r = client.post("/v1/ops/login", json={"id": "admin", "pw": s.ops_admin_pw})
    assert r.status_code == 429


def test_oversize_body_413(client):
    r = client.post("/v1/sessions/start", content=b"x",
                    headers={"content-type": "application/json",
                             "content-length": str(11 * 1024 * 1024)})
    assert r.status_code == 413
