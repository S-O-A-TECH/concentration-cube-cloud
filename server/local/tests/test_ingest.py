"""S2 DoD: 세션 수집 시나리오 7종 (S2 구현계획 §1.8).

정상 / 뒤섞임 / 중복 재전송 / 유실(count_match=false 로 저장 성공) /
잘못된 토큰 / nonce 재사용 / 영상 업로드 거부.
sqlite(StaticPool) + tmp 스토리지 + 인메모리 nonce 로 컨테이너 없이 돈다.
"""
import hashlib
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db, get_nonce_store, get_storage
from app.db import models
from app.db.base import Base
from app.main import app
from app.services.nonce import MemoryNonceStore
from app.services.storage import SessionStorage
from tests.device_sim import DeviceSim

SERIAL = "SIM_DEV_001"
FACTORY_TOKEN = "sim-factory-token"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # finish 의 채점 큐잉은 ingest 테스트 범위 밖 — no-op (채점은 test_scoring_pipeline)
    from app.services import queue as queue_service
    monkeypatch.setattr(queue_service, "enqueue_score_session", lambda sid: None)
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(models.Device(
            serial=SERIAL, hw_rev="sim", fw_ver="sim-0.1",
            factory_token_hash=hashlib.sha256(FACTORY_TOKEN.encode()).hexdigest(),
            research_flag=True))
        db.commit()

    storage = SessionStorage(tmp_path / "storage")
    nonce_store = MemoryNonceStore()

    def _db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_nonce_store] = lambda: nonce_store
    c = TestClient(app)
    c.storage = storage          # 테스트에서 검증용
    c.session_factory = factory
    yield c
    app.dependency_overrides.clear()
    engine.dispose()


def _sim(client) -> DeviceSim:
    return DeviceSim(client, SERIAL, FACTORY_TOKEN)


def _db_session(client, sid) -> models.StudySession:
    with client.session_factory() as db:
        return db.get(models.StudySession, uuid.UUID(sid))


# ---------- ① 정상 ----------

def test_normal_upload_e2e(client):
    out = _sim(client).run_session([("focus", 60), ("off_task", 30), ("focus", 30)])
    fin = out["finish"]
    assert fin.status_code == 200, fin.text
    body = fin.json()
    assert body["count_match"] is True
    assert body["session_crc_match"] is True
    assert body["missing_ranges"] == []
    assert body["uploaded_samples"] == 1200

    sess = _db_session(client, out["sid"])
    assert sess.upload_state == "complete"
    assert sess.count_match is True
    assert sess.split in ("train", "holdout")
    assert sess.research_mode is True          # 기기 research_flag 로 강제

    from app.focus_scoring import records as rec
    df = rec.read_parquet(client.storage.record_path(out["sid"]))
    assert len(df) == 1200
    assert rec.integrity_check(df, duration_sec=120)["ok"] is True


# ---------- ② chunk 순서 뒤섞임 ----------

def test_out_of_order_chunks(client):
    out = _sim(client).run_session([("focus", 120)], chunk_order=[1, 0])
    assert out["finish"].json()["count_match"] is True


# ---------- ③ 중복 재전송 (멱등) ----------

def test_duplicate_resend_idempotent(client):
    out = _sim(client).run_session([("focus", 120)], duplicate_chunks={0})
    dup = [r for r in out["chunks"] if r.json().get("duplicate")]
    assert len(dup) == 1                        # 두 번째 전송이 duplicate 로 무시됨
    assert out["finish"].json()["count_match"] is True
    assert out["finish"].json()["uploaded_samples"] == 1200


def test_duplicate_index_different_crc_409(client):
    sim = _sim(client)
    assert sim.auth().status_code == 200
    r = sim.start(duration_sec=120)
    sid = r.json()["session_id"]
    synth_mod = __import__("tests.device_sim", fromlist=["load_synth"]).load_synth()
    records = synth_mod.make_records([("focus", 120)])
    chunks = DeviceSim.make_chunks(records, 600)
    assert sim.send_chunk(sid, chunks[0]).status_code == 200
    forged = dict(chunks[1], chunk_index=0)     # 같은 index, 다른 내용
    assert sim.send_chunk(sid, forged).status_code == 409


# ---------- ④ 유실 → count_match=false 지만 저장은 성공 ----------

def test_missing_chunk_saved_with_count_match_false(client):
    out = _sim(client).run_session([("focus", 180)], chunk_size=600, drop_chunks={1})
    fin = out["finish"]
    assert fin.status_code == 200               # 오류가 아니라 데이터
    body = fin.json()
    assert body["count_match"] is False
    assert body["missing_ranges"] == [[601, 1200]]
    sess = _db_session(client, out["sid"])
    assert sess.upload_state == "complete"
    assert sess.count_match is False
    assert client.storage.has_record(out["sid"])


# ---------- ④-1 C1 회귀: finalize 후 커밋 전 크래시 → 재시도가 확정본을 파괴하지 않는다 ----------

def test_finish_retry_after_crash_between_finalize_and_commit(client):
    sim = _sim(client)
    out = sim.run_session([("focus", 120)])
    sid = out["sid"]
    # 크래시 재현: parquet 확정 + chunks 정리 완료됐지만 DB 는 open 으로 남은 상태
    with client.session_factory() as db:
        sess = db.get(models.StudySession, uuid.UUID(sid))
        sess.upload_state = "open"
        sess.count_match = None
        db.commit()
    assert not client.storage.chunks_dir(sid).exists()

    fin = sim.finish(sid, out["records"], expected_samples=1200)   # 기기 재시도
    assert fin.status_code == 200
    body = fin.json()
    assert body["count_match"] is True          # 확정본 기준 재판정 (빈 merge 아님)
    from app.focus_scoring import records as rec
    df = rec.read_parquet(client.storage.record_path(sid))
    assert len(df) == 1200                      # 확정 parquet 무사


def test_finalize_never_overwrites_with_empty(client, tmp_path):
    """C1 가드 단위 검증: 빈 df 로는 기존 확정본을 덮어쓸 수 없다."""
    from app.focus_scoring import records as rec
    from tests.synth import make_records
    storage = client.storage
    sid = "guard-test"
    storage.finalize(sid, rec.to_dataframe(make_records([("focus", 10)])))
    assert len(rec.read_parquet(storage.record_path(sid))) == 100
    storage.finalize(sid, rec.to_dataframe([]))          # 빈 덮어쓰기 시도
    assert len(rec.read_parquet(storage.record_path(sid))) == 100


# ---------- ⑤ 인증 실패 ----------

def test_bad_factory_token_401(client):
    sim = DeviceSim(client, SERIAL, "wrong-token")
    assert sim.auth().status_code == 401


def test_bad_bearer_401(client):
    sim = _sim(client)
    sim.token = "garbage.jwt.token"
    r = sim.start()
    assert r.status_code == 401


# ---------- ⑥ nonce 재사용 ----------

def test_nonce_reuse_401(client):
    sim = _sim(client)
    assert sim.auth().status_code == 200
    nonce = uuid.uuid4().hex
    ts = int(time.time())
    body = {"firmware_version": "sim", "schema_version": "4.2",
            "session_mode": "DEV-2", "duration_sec": 120}
    r1 = client.post("/v1/sessions/start", json=body,
                     headers=sim._headers(nonce=nonce, ts=ts))
    r2 = client.post("/v1/sessions/start", json=body,
                     headers=sim._headers(nonce=nonce, ts=ts))
    assert r1.status_code == 200
    assert r2.status_code == 401
    assert "nonce" in r2.json()["detail"]


def test_stale_timestamp_401(client):
    sim = _sim(client)
    assert sim.auth().status_code == 200
    r = client.post("/v1/sessions/start", json={"schema_version": "4.2",
                                                "session_mode": "DEV-2",
                                                "duration_sec": 120},
                    headers=sim._headers(ts=int(time.time()) - 3600))
    assert r.status_code == 401


# ---------- ⑦ 영상 거부 ----------

def test_video_mime_415(client):
    r = client.post("/v1/sessions/start", content=b"fake",
                    headers={"content-type": "video/mp4"})
    assert r.status_code == 415
    r = client.post("/v1/sessions/start", content=b"fake",
                    headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 415


def test_raw_video_claim_rejected(client):
    sim = _sim(client)
    assert sim.auth().status_code == 200
    r = client.post("/v1/sessions/start", json={"schema_version": "4.2",
                                                "session_mode": "DEV-2",
                                                "duration_sec": 120,
                                                "raw_video_uploaded": True},
                    headers=sim._headers())
    assert r.status_code == 415


# ---------- 부가: 계약 검증 ----------

def test_wrong_schema_version_422(client):
    sim = _sim(client)
    assert sim.auth().status_code == 200
    r = sim.start(schema_version="4.1")
    assert r.status_code == 422


def test_chunk_crc_mismatch_400(client):
    sim = _sim(client)
    assert sim.auth().status_code == 200
    sid = sim.start(duration_sec=120).json()["session_id"]
    synth_mod = __import__("tests.device_sim", fromlist=["load_synth"]).load_synth()
    chunks = DeviceSim.make_chunks(synth_mod.make_records([("focus", 60)]), 600)
    bad = dict(chunks[0], crc32=(chunks[0]["crc32"] + 1) & 0xFFFFFFFF)
    assert sim.send_chunk(sid, bad).status_code == 400
