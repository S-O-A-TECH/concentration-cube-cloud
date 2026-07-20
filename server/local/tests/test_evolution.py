"""S5 DoD: Evolution 한 세대의 전 생애주기 통합 테스트 (S5 구현계획 §1.10).

시나리오: 합성 세션 10개 업로드+라벨 → 나쁜 후보 → evaluate → rejected →
나쁜 후보 promote 409 → 좋은 후보(멍때림 임계 개선) → passed → promote →
result 가 새 세대로 → rollback → 원복. 전 과정 audit 기록.

파라미터는 실증값: 6초 멍때림은 baseline(min_sec=8) 미검출,
good(min_sec=4, min_segment=4) 검출, bad(전부 blank 오판) — 경계 침식 ~1.2초.
"""
import copy
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import focus_scoring
from app.api.deps import get_db, get_nonce_store, get_storage
from app.config import get_settings
from app.db import models
from app.db.base import Base
from app.db.seed import seed
from app.jobs.rescore_paramset import evaluate_paramset, rescore_all
from app.jobs.score_session import score_session
from app.main import app
from app.services import queue as queue_service
from app.services.nonce import MemoryNonceStore
from app.services.storage import SessionStorage
from tests.device_sim import DeviceSim

SERIAL = "SIM_DEV_EVO"
FACTORY_TOKEN = "sim-evo-token"

SCENARIO = [("focus", 120), ("blank_stare", 6), ("focus", 60),
            ("blank_stare", 6), ("focus", 48)]           # 240초 (DEV-4)
LABELS = [{"t0": 0, "t1": 120, "label": "focus"},
          {"t0": 120, "t1": 126, "label": "blank_stare"},
          {"t0": 126, "t1": 186, "label": "focus"},
          {"t0": 186, "t1": 192, "label": "blank_stare"},
          {"t0": 192, "t1": 240, "label": "focus"}]


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
    monkeypatch.setattr(queue_service, "enqueue_evaluate",
                        lambda pid, jid, retro: evaluate_paramset(
                            pid, jid, retro, _factory=factory, _storage=storage))
    monkeypatch.setattr(queue_service, "enqueue_rescore_all",
                        lambda pid, jid: rescore_all(
                            pid, jid, _factory=factory, _storage=storage))

    client = TestClient(app)
    yield {"client": client, "factory": factory, "storage": storage,
           "evo": {"X-Evolution-Token": get_settings().evolution_token}}
    app.dependency_overrides.clear()
    engine.dispose()


def _upload_labeled_sessions(env, n=10) -> list[str]:
    client, evo = env["client"], env["evo"]
    sim = DeviceSim(client, SERIAL, FACTORY_TOKEN)
    sids = [sim.run_session(SCENARIO, mode="DEV-4")["sid"] for _ in range(n)]
    # 결정적 split (해시 우연으로 holdout 0개가 되는 flake 방지 — 테스트 전용 직접 배정)
    with env["factory"]() as db:
        for i, sid in enumerate(sids):
            db.get(models.StudySession, uuid.UUID(sid)).split = \
                "holdout" if i >= 7 else "train"
        db.commit()
    for sid in sids:
        r = client.post(f"/v1/evolution/sessions/{sid}/labels", headers=evo,
                        json={"labeler": "tester", "method": "realtime_instructed",
                              "protocol": "TEST-6S", "segments": LABELS})
        assert r.status_code == 200, r.text
    return sids


def _bad_params() -> dict:
    p = copy.deepcopy(focus_scoring.load_params())
    p["blank_stare"].update(min_sec=0.5, max_saccade_count_1s=5.0,
                            stare_dispersion_th=0.15)   # 전부 멍때림으로 오판
    return p


def _good_params() -> dict:
    p = copy.deepcopy(focus_scoring.load_params())
    p["blank_stare"]["min_sec"] = 4.0
    p["timeline"]["min_segment_sec"] = 4.0              # 6초 멍때림 검출 (실증)
    return p


def test_evolution_generation_lifecycle(env):
    client, evo = env["client"], env["evo"]

    # 토큰 없으면 401
    assert client.get("/v1/evolution/overview").status_code == 401

    sids = _upload_labeled_sessions(env)

    # 라벨 불변 — 재-POST 409
    r = client.post(f"/v1/evolution/sessions/{sids[0]}/labels", headers=evo,
                    json={"labeler": "tester", "segments": LABELS})
    assert r.status_code == 409

    ov = client.get("/v1/evolution/overview", headers=evo).json()
    assert ov["sessions"]["labeled"] == 10
    assert ov["sessions"]["split"] == {"train": 7, "holdout": 3}
    assert ov["active_param_set"]["version"] == "v1.0"

    # ---------- 나쁜 후보: evaluate → rejected, promote → 409 ----------
    r = client.post("/v1/evolution/param_sets", headers=evo,
                    json={"json_params": _bad_params(), "origin": "agent",
                          "agent_name": "test-agent",
                          "rationale": "나쁜 후보 — 전부 blank 오판", "parent_id": 1})
    assert r.status_code == 200, r.text
    bad_id = r.json()["id"]
    r = client.post(f"/v1/evolution/param_sets/{bad_id}/evaluate", headers=evo, json={})
    assert r.status_code == 200
    report = client.get(f"/v1/evolution/param_sets/{bad_id}/report", headers=evo).json()
    assert report["gate"]["passed"] is False
    ps_list = {p["id"]: p for p in
               client.get("/v1/evolution/param_sets", headers=evo).json()["param_sets"]}
    assert ps_list[bad_id]["status"] == "rejected"
    # 게이트 미통과는 사람이 눌러도 promote 불가 (DoD)
    r = client.post(f"/v1/evolution/param_sets/{bad_id}/promote", headers=evo,
                    json={"confirm": True})
    assert r.status_code == 409

    # ---------- 좋은 후보: passed → promote → 새 세대 ----------
    r = client.post("/v1/evolution/param_sets", headers=evo,
                    json={"json_params": _good_params(), "origin": "agent",
                          "agent_name": "test-agent",
                          "rationale": "6초 멍때림 검출 개선", "parent_id": 1})
    good_id = r.json()["id"]
    good_version = r.json()["version"]
    r = client.post(f"/v1/evolution/param_sets/{good_id}/evaluate", headers=evo, json={})
    assert r.status_code == 200
    report = client.get(f"/v1/evolution/param_sets/{good_id}/report", headers=evo).json()
    assert report["gate"]["passed"] is True, report["gate"]
    hb = report["holdout"]["per_state"]["blank_stare"]["sens"]
    assert hb["before"] == 0.0 and hb["after"] == 1.0   # 멍때림 검출 개선 실증

    # confirm 없으면 거부
    assert client.post(f"/v1/evolution/param_sets/{good_id}/promote", headers=evo,
                       json={"confirm": False}).status_code == 422
    r = client.post(f"/v1/evolution/param_sets/{good_id}/promote", headers=evo,
                    json={"confirm": True, "decided_by": "원장님"})
    assert r.status_code == 200
    assert r.json()["active_version"] == good_version

    # result 가 새 세대로 바뀌었나 (기기 API 로 확인)
    sim = DeviceSim(client, SERIAL, FACTORY_TOKEN)
    assert sim.auth().status_code == 200
    result = client.get(f"/v1/sessions/{sids[0]}/result", headers=sim._headers()).json()
    assert result["param_set_version"] == good_version
    states = {seg["state"] for seg in result["timeline"]}
    assert "blank_stare" in states                       # 새 세대는 멍때림을 본다

    # runs 아카이브: 세대별 이력 + promoted 마킹
    runs = client.get(f"/v1/evolution/sessions/{sids[0]}/runs", headers=evo).json()["runs"]
    versions = [r_["param_set_version"] for r_ in runs]
    assert "v1.0" in versions and good_version in versions
    assert any(r_["promoted"] and r_["param_set_version"] == good_version for r_ in runs)

    # ---------- 오답노트 (나쁜 후보의 실수 목록) ----------
    mk = client.get(f"/v1/evolution/mistakes?param_set_id={bad_id}", headers=evo).json()
    assert mk["bins_total"] > 0
    assert any(m["truth"] == "focus" and m["predicted"] == "blank_stare"
               for m in mk["mistakes"])
    assert mk["confusion"]["focus"]["blank_stare"] > 0

    # ---------- rollback → 원복 ----------
    r = client.post(f"/v1/evolution/param_sets/{good_id}/rollback", headers=evo)
    assert r.status_code == 200
    assert r.json()["active_version"] == "v1.0"
    result = client.get(f"/v1/sessions/{sids[0]}/result", headers=sim._headers()).json()
    assert result["param_set_version"] == "proto-0.1.0"  # v1.0 시드의 내용 버전

    # ---------- audit 전이 기록 (DoD) ----------
    with env["factory"]() as db:
        actions = {a for (a,) in db.execute(select(models.AuditLog.action)).all()}
    assert {"label_created", "param_set_created", "evaluate_started",
            "param_set_promoted", "param_set_rolled_back",
            "param_set_rejected"} - actions == set()

    # jobs 노출
    jobs = client.get("/v1/evolution/jobs", headers=evo).json()["jobs"]
    assert any(j["kind"] == "evaluate" and j["state"] == "done" for j in jobs)
    assert any(j["kind"] == "promote_rescore" and j["state"] == "done" for j in jobs)


def test_exclude_removes_from_evaluate_and_mistakes(env):
    client, evo = env["client"], env["evo"]
    sids = _upload_labeled_sessions(env)

    # train 세션 1개 제외 → mistakes 에서 사라진다 (사유 필수)
    sid = sids[0]
    assert client.post(f"/v1/evolution/sessions/{sid}/exclude", headers=evo,
                       json={}).status_code == 422       # 사유 없으면 거부
    r = client.post(f"/v1/evolution/sessions/{sid}/exclude", headers=evo,
                    json={"reason": "검증 실패 — 재시도 예정"})
    assert r.status_code == 200
    mk = client.get("/v1/evolution/mistakes", headers=evo).json()
    assert all(m["session_id"] != sid for m in mk["mistakes"])

    # 복원 (아카이브 보존 — 삭제된 적 없음)
    r = client.post(f"/v1/evolution/sessions/{sid}/restore", headers=evo,
                    json={"reason": "환경 문제 해소 확인"})
    assert r.status_code == 200
    detail = client.get(f"/v1/evolution/sessions/{sid}/detail", headers=evo).json()
    assert detail["session"]["excluded"] is False
    assert detail["result"] is not None                  # 데이터는 계속 존재
