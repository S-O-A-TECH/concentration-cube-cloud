"""S6 DoD: 운영 콘솔 — 로그인, 기기 발급 E2E, 리포트 뷰, 재채점, 권한 분리 (S6 §2.4, §4)."""
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db, get_nonce_store, get_storage
from app.config import get_settings
from app.db import models
from app.db.base import Base
from app.db.seed import seed, seed_demo
from app.jobs.privacy import delete_profile_data
from app.jobs.score_session import score_session
from app.main import app
from app.services import queue as queue_service
from app.services.nonce import MemoryNonceStore
from app.services.storage import SessionStorage
from tests.device_sim import DeviceSim

SERIAL = "SIM_DEV_OPS"
FACTORY_TOKEN = "sim-ops-token"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage = SessionStorage(tmp_path / "storage")

    with factory() as db:
        seed(db)
        seed_demo(db)
        db.add(models.Device(
            serial=SERIAL,
            factory_token_hash=hashlib.sha256(FACTORY_TOKEN.encode()).hexdigest(),
            research_flag=True))
        db.commit()

    def _db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_nonce_store] = lambda: MemoryNonceStore()
    monkeypatch.setattr(queue_service, "enqueue_score_session",
                        lambda sid: score_session(sid, _factory=factory, _storage=storage))
    monkeypatch.setattr(queue_service, "enqueue_llm_report", lambda run_id: None)
    monkeypatch.setattr(queue_service, "enqueue_delete_profile",
                        lambda pid: delete_profile_data(pid, _factory=factory,
                                                        _storage=storage))

    client = TestClient(app)
    yield {"client": client, "factory": factory, "storage": storage}
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client) -> None:
    s = get_settings()
    r = client.post("/v1/ops/login", json={"id": s.ops_admin_id, "pw": s.ops_admin_pw})
    assert r.status_code == 200


def test_login_guard_and_ui_redirect(env):
    client = env["client"]
    assert client.get("/v1/ops/dashboard").status_code == 401
    r = client.get("/ops/dashboard", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/ops/login"
    assert client.post("/v1/ops/login",
                       json={"id": "admin", "pw": "wrong"}).status_code == 401
    _login(client)
    assert client.get("/v1/ops/dashboard").status_code == 200
    assert client.get("/ops/dashboard").status_code == 200
    assert "운영 콘솔" in client.get("/ops/dashboard").text
    client.post("/v1/ops/logout")
    assert client.get("/v1/ops/dashboard").status_code == 401


def test_device_registration_upload_e2e_and_report_view(env):
    """기기 등록 → 발급 토큰으로 device_sim 업로드 → 리포트 뷰 (운영 업무 E2E DoD)."""
    client = env["client"]
    _login(client)

    r = client.post("/v1/ops/devices", json={"serial": "OPS_NEW_DEV_001",
                                             "research_flag": True})
    assert r.status_code == 200
    token = r.json()["factory_token"]

    sim = DeviceSim(client, "OPS_NEW_DEV_001", token)
    out = sim.run_session([("focus", 90), ("blank_stare", 30)])
    assert out["finish"].json()["count_match"] is True
    sid = out["sid"]

    sessions = client.get("/v1/ops/sessions?device=OPS_NEW_DEV_001").json()["sessions"]
    assert len(sessions) == 1 and sessions[0]["sid"] == sid
    report = client.get(f"/v1/ops/sessions/{sid}/report").json()
    assert report["result"]["sfi"] is not None

    html = client.get(f"/ops/sessions/{sid}").text     # 리포트 뷰 (관리자 선확인 경로)
    assert "SFI" in html and "타임라인" in html and "코칭 문구" in html

    # 중복 시리얼 409
    assert client.post("/v1/ops/devices",
                       json={"serial": "OPS_NEW_DEV_001"}).status_code == 409


def test_token_reissue_invalidates_old(env):
    client = env["client"]
    _login(client)
    r = client.post("/v1/ops/devices", json={"serial": "OPS_REISSUE_DEV"})
    dev_id, old_token = r.json()["id"], r.json()["factory_token"]
    new_token = client.post(f"/v1/ops/devices/{dev_id}/reissue_token").json()["factory_token"]

    assert DeviceSim(client, "OPS_REISSUE_DEV", old_token).auth().status_code == 401
    assert DeviceSim(client, "OPS_REISSUE_DEV", new_token).auth().status_code == 200


def test_rescore_creates_new_run(env):
    client = env["client"]
    _login(client)
    sim = DeviceSim(client, SERIAL, FACTORY_TOKEN)
    sid = sim.run_session([("focus", 120)])["sid"]

    r = client.post(f"/v1/ops/sessions/{sid}/rescore")
    assert r.status_code == 200
    with env["factory"]() as db:
        runs = db.execute(select(models.ScoringRun)
                          .where(models.ScoringRun.session_id == uuid.UUID(sid))).scalars().all()
        assert len(runs) == 2                          # INSERT only 재채점


def test_cross_permission_isolation(env):
    """ops↔evolution 완전 분리 (S6 DoD 권한 교차 2건)."""
    client = env["client"]
    evo = {"X-Evolution-Token": get_settings().evolution_token}

    # EVOLUTION_TOKEN 으로 /v1/ops/* → 401
    assert client.get("/v1/ops/dashboard", headers=evo).status_code == 401
    # 운영 쿠키(로그인)로 /v1/evolution/* → 401
    _login(client)
    assert client.get("/v1/evolution/overview").status_code == 401
    # 각자 자기 영역은 정상
    assert client.get("/v1/ops/dashboard").status_code == 200
    assert client.get("/v1/evolution/overview", headers=evo).status_code == 200


def test_privacy_delete_two_step(env):
    client = env["client"]
    _login(client)
    profiles = client.get("/v1/ops/profiles").json()["profiles"]
    target = profiles[0]["id"]

    assert client.post("/v1/ops/privacy/delete_profile",
                       json={"profile_id": target}).status_code == 422   # 2단 확인
    r = client.post("/v1/ops/privacy/delete_profile",
                    json={"profile_id": target, "confirm": True})
    assert r.status_code == 200

    with env["factory"]() as db:
        assert db.get(models.Profile, target) is None
        actions = {a for (a,) in db.execute(select(models.AuditLog.action)).all()}
        assert {"privacy_delete_requested", "profile_deleted"} <= actions


def test_demo_seed_visible_in_users(env):
    client = env["client"]
    _login(client)
    users = client.get("/v1/ops/users").json()["users"]
    assert any(u["email"] == "demo@example.com" and u["profiles"] == 2 for u in users)
    assert "demo@example.com" in client.get("/ops/users").text
