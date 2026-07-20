"""S7 §1.2~1.3 — 삭제권 완전 삭제(parquet 포함 잔존 0) + 방치 세션 정리."""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db, get_nonce_store, get_storage
from app.config import get_settings
from app.db import models
from app.db.base import Base
from app.db.seed import seed
from app.jobs.maintenance import cleanup_stale_sessions
from app.jobs.privacy import delete_profile_data
from app.jobs.score_session import score_session
from app.main import app
from app.services import queue as queue_service
from app.services.nonce import MemoryNonceStore
from app.services.storage import SessionStorage
from tests.device_sim import DeviceSim

SERIAL = "SIM_DEV_PRIV"
FACTORY_TOKEN = "sim-priv-token"


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
        user = models.User(email="priv@example.com", auth_provider="google")
        db.add(user)
        db.flush()
        profile = models.Profile(user_id=user.id, nickname="삭제대상")
        db.add(profile)
        db.flush()
        db.add(models.Device(
            serial=SERIAL,
            factory_token_hash=hashlib.sha256(FACTORY_TOKEN.encode()).hexdigest(),
            research_flag=True, default_profile_id=profile.id))
        db.commit()
        profile_id = profile.id

    def _db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_nonce_store] = lambda: MemoryNonceStore()
    monkeypatch.setattr(queue_service, "enqueue_score_session",
                        lambda sid: score_session(sid, _factory=factory, _storage=storage))
    monkeypatch.setattr(queue_service, "enqueue_llm_report", lambda run_id: None)

    client = TestClient(app)
    yield {"client": client, "factory": factory, "storage": storage,
           "profile_id": profile_id}
    app.dependency_overrides.clear()
    engine.dispose()


def test_privacy_full_delete_leaves_zero_traces(env):
    client, storage = env["client"], env["storage"]
    sim = DeviceSim(client, SERIAL, FACTORY_TOKEN)
    sids = [sim.run_session([("focus", 60)])["sid"] for _ in range(2)]

    evo = {"X-Evolution-Token": get_settings().evolution_token}
    r = client.post(f"/v1/evolution/sessions/{sids[0]}/labels", headers=evo,
                    json={"labeler": "t", "segments": [{"t0": 0, "t1": 60, "label": "focus"}]})
    assert r.status_code == 200
    assert storage.has_record(sids[0])

    out = delete_profile_data(env["profile_id"], _factory=env["factory"],
                              _storage=storage)
    assert out["sessions_deleted"] == 2

    with env["factory"]() as db:
        assert db.get(models.Profile, env["profile_id"]) is None
        for sid in sids:
            u = uuid.UUID(sid)
            assert db.get(models.StudySession, u) is None
            assert db.get(models.PromotedResult, u) is None
            assert db.execute(select(models.ScoringRun)
                              .where(models.ScoringRun.session_id == u)).first() is None
            assert db.execute(select(models.Label)
                              .where(models.Label.session_id == u)).first() is None
        actions = {a for (a,) in db.execute(select(models.AuditLog.action)).all()}
        assert "profile_deleted" in actions
    for sid in sids:
        assert not storage.session_dir(sid).exists()     # parquet 잔존 0건


def test_stale_open_session_marked_failed(env):
    client, storage = env["client"], env["storage"]
    sim = DeviceSim(client, SERIAL, FACTORY_TOKEN)
    assert sim.auth().status_code == 200
    sid = sim.start(duration_sec=120).json()["session_id"]
    synth = __import__("tests.device_sim", fromlist=["load_synth"]).load_synth()
    chunk = DeviceSim.make_chunks(synth.make_records([("focus", 60)]), 600)[0]
    assert sim.send_chunk(sid, chunk).status_code == 200   # chunks/ 임시 생성

    with env["factory"]() as db:
        sess = db.get(models.StudySession, uuid.UUID(sid))
        sess.started_at = datetime.now(timezone.utc) - timedelta(hours=25)
        db.commit()

    out = cleanup_stale_sessions(_factory=env["factory"], _storage=storage)
    assert out["stale_failed"] == 1
    with env["factory"]() as db:
        assert db.get(models.StudySession, uuid.UUID(sid)).upload_state == "failed"
    assert not storage.chunks_dir(sid).exists()            # 임시 chunk 청소됨
