"""S3 DoD: 채점 파이프라인 — 자동 채점, result 계약, 결정성, 실패, coverage 규칙.

sqlite + tmp 스토리지에서 잡을 동기 실행(RQ 대신 직접 호출)해 컨테이너 없이 검증한다.
실제 RQ worker 경유는 tools/s3_dod_e2e.py (compose DoD).
"""
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db, get_nonce_store, get_storage
from app.db import models
from app.db.base import Base
from app.db.seed import seed
from app.jobs.score_session import score_session
from app.main import app
from app.services import queue as queue_service
from app.services.nonce import MemoryNonceStore
from app.services.storage import SessionStorage
from tests.device_sim import DeviceSim

SERIAL = "SIM_DEV_002"
FACTORY_TOKEN = "sim-factory-token-2"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """sqlite DB(시드 포함) + tmp 스토리지 + 동기 채점 잡 연결."""
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage = SessionStorage(tmp_path / "storage")

    with factory() as db:
        seed(db)                                    # param_set v1.0 (adopted)
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

    # finish 큐잉 → 동기 실행 (테스트에선 worker 가 없다). LLM 큐잉도 차단.
    monkeypatch.setattr(queue_service, "enqueue_score_session",
                        lambda sid: score_session(sid, _factory=factory, _storage=storage))
    monkeypatch.setattr(queue_service, "enqueue_llm_report", lambda run_id: None)

    client = TestClient(app)
    yield {"client": client, "factory": factory, "storage": storage}
    app.dependency_overrides.clear()
    engine.dispose()


def _upload(env, scenario):
    sim = DeviceSim(env["client"], SERIAL, FACTORY_TOKEN)
    out = sim.run_session(scenario)
    assert out["finish"].status_code == 200, out["finish"].text
    return sim, out


def test_auto_scoring_and_result_contract(env):
    """업로드 → 자동 채점 → result 200 (웹캠 SPEC §4.4 계약)."""
    sim, out = _upload(env, [("focus", 60), ("blank_stare", 30), ("focus", 30)])
    sid = out["sid"]

    status = env["client"].get(f"/v1/sessions/{sid}/status", headers=sim._headers()).json()
    assert status["scoring"] == "done"

    r = env["client"].get(f"/v1/sessions/{sid}/result", headers=sim._headers())
    assert r.status_code == 200
    result = r.json()
    for key in ("schema", "algo_version", "param_set_version", "confidence", "sfi",
                "components", "focused_minutes", "max_focus_streak_min",
                "timeline", "events", "coach_text", "quality"):
        assert key in result, key
    assert result["sfi"] is not None
    assert result["param_set_version"] == "proto-0.1.0"

    with env["factory"]() as db:
        sess = db.get(models.StudySession, uuid.UUID(sid))
        assert sess.scoring_state == "done"
        assert sess.coverage is not None            # qc 결과가 세션에 반영됨
        promoted = db.get(models.PromotedResult, uuid.UUID(sid))
        assert promoted is not None


def test_rescore_is_deterministic_and_insert_only(env):
    """같은 세션 재채점 → 새 run INSERT + result JSON 완전 동일 (순수 함수 E2E)."""
    import json
    _, out = _upload(env, [("focus", 90), ("off_task", 30)])
    sid = out["sid"]

    # R1 가드: done 상태의 중복 큐잉(경합 재큐잉 등)은 조기 반환 — 새 run 을 만들지 않는다
    skipped = score_session(sid, _factory=env["factory"], _storage=env["storage"])
    assert skipped.get("skipped") == "already scored"

    # 정식 재채점 경로(ops rescore)는 queued 로 되돌린 뒤 실행된다
    with env["factory"]() as db:
        db.get(models.StudySession, uuid.UUID(sid)).scoring_state = "queued"
        db.commit()
    score_session(sid, _factory=env["factory"], _storage=env["storage"])   # 2회째

    with env["factory"]() as db:
        runs = db.execute(
            select(models.ScoringRun)
            .where(models.ScoringRun.session_id == uuid.UUID(sid))
            .order_by(models.ScoringRun.id)
        ).scalars().all()
        assert len(runs) == 2                       # INSERT only
        j1 = json.dumps(runs[0].result_json, sort_keys=True, ensure_ascii=False)
        j2 = json.dumps(runs[1].result_json, sort_keys=True, ensure_ascii=False)
        assert j1 == j2
        promoted = db.get(models.PromotedResult, uuid.UUID(sid))
        assert promoted.scoring_run_id == runs[-1].id   # 최신 run 을 가리킴


def test_low_coverage_gives_null_sfi(env):
    """coverage<70% → sfi=null + 안내 (Layer 0 규칙 서버 재현)."""
    sim, out = _upload(env, [("focus", 30), ("invalid", 60), ("focus", 30)])
    r = env["client"].get(f"/v1/sessions/{out['sid']}/result", headers=sim._headers())
    assert r.status_code == 200
    result = r.json()
    assert result["sfi"] is None
    assert result["quality"]["unscorable_reason"] == "low_coverage"


def test_scoring_failure_recorded(env, monkeypatch):
    """잡 실패(parquet 소실) → scoring_state=failed + result 409."""
    # 큐잉을 no-op 으로 바꿔 수동 실행 시나리오 구성
    monkeypatch.setattr(queue_service, "enqueue_score_session", lambda sid: None)
    sim, out = _upload(env, [("focus", 120)])
    sid = out["sid"]

    env["storage"].record_path(sid).unlink()        # 파일 삭제 후 채점
    with pytest.raises(Exception):
        score_session(sid, _factory=env["factory"], _storage=env["storage"])

    with env["factory"]() as db:
        sess = db.get(models.StudySession, uuid.UUID(sid))
        assert sess.scoring_state == "failed"
        assert sess.scoring_error

    r = env["client"].get(f"/v1/sessions/{sid}/result", headers=sim._headers())
    assert r.status_code == 409


def test_result_202_while_queued(env, monkeypatch):
    monkeypatch.setattr(queue_service, "enqueue_score_session", lambda sid: None)
    sim, out = _upload(env, [("focus", 120)])
    with env["factory"]() as db:
        sess = db.get(models.StudySession, uuid.UUID(out["sid"]))
        sess.scoring_state = "queued"
        db.commit()
    r = env["client"].get(f"/v1/sessions/{out['sid']}/result", headers=sim._headers())
    assert r.status_code == 202
    assert r.json()["scoring"] == "queued"
