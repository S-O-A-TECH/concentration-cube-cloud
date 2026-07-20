"""E4/E5/E6 DoD: 상태 머신 전 전이, 락, FAILED 경로, 재계산 기각,
자동 모드 PASSED 정지, promote/rollback, 채택 이중 방어."""
from __future__ import annotations

import time

import pytest

from app import db
from app.loop import machine as machine_mod
from app.loop.machine import get_loop
from app.server_client import OpsClient, OpsError
from tests.mocks import agent_fake


@pytest.fixture
def loop(env, monkeypatch):
    monkeypatch.setattr(machine_mod, "get_adapter",
                        lambda name: agent_fake.FakeGoodAgent())
    return get_loop()


def _wait(loop, states: tuple[str, ...], timeout=60.0):
    t0 = time.monotonic()
    while loop.state not in states:
        if time.monotonic() - t0 > timeout:
            raise TimeoutError(f"상태 대기 초과: {loop.state} (원함: {states}) "
                               f"log={loop.data.get('log', [])[-3:]}")
        time.sleep(0.1)


def test_full_generation_lifecycle(loop):
    """COLLECT→PROPOSE→REVIEW_DIFF→REGISTERED→EVALUATING→PASSED→(사람)promote→IDLE→rollback."""
    assert loop.can_run()[0], loop.can_run()[1]
    loop.run()
    _wait(loop, ("REVIEW_DIFF", "FAILED"))
    assert loop.state == "REVIEW_DIFF", loop.data
    st = loop.status()
    assert st["proposal"]["schema"] == "proposal.v1"
    assert st["validation"]["ok"] is True

    loop.register()
    _wait(loop, ("PASSED", "REJECTED", "FAILED"))
    assert loop.state == "PASSED", loop.status()["report"]
    pid = loop.status()["param_set_id"]
    report = loop.status()["report"]
    assert report["gate"]["passed"] is True
    # holdout 에서 blank_stare 개선 확인 (시드 데이터의 의도)
    bs = report["holdout"]["per_state"]["blank_stare"]["sens"]
    assert bs["after"] > bs["before"]

    # 채택은 사람 버튼 — ops promote + mark_adopted (routes 흐름 재현)
    ops = OpsClient()
    res = ops.promote(pid, confirm=True)
    assert res["n_rescored"] > 0
    loop.mark_adopted(pid)
    assert loop.state == "IDLE"
    assert db.get_proposal(loop.gen_id)["status"] == "adopted"
    assert ops.active_param_set()["id"] == pid

    # 재채점이 runs 아카이브에 쌓였다 (세대별 재채점 비교표 재료)
    sid = ops.sessions(labeled=True)[0]["sid"]
    runs = ops.session_runs(sid)
    assert any(r["param_set_id"] == pid for r in runs)

    # 롤백 → 직전 세대(v1.0) 재활성
    ops.rollback(pid, "리허설 롤백")
    loop.mark_rolled_back(pid)
    assert ops.active_param_set()["version"] == "v1.0"
    assert db.get_proposal(loop.gen_id)["status"] == "rolled_back"


def test_lock_rejects_second_run(loop):
    loop.run()
    with pytest.raises(RuntimeError, match="in progress"):
        loop.run()
    _wait(loop, ("REVIEW_DIFF", "FAILED"))
    loop.dismiss("테스트 정리")
    assert loop.state == "IDLE"


def test_auto_mode_stops_at_passed(env, monkeypatch):
    """E6 DoD: 자동 모드에서도 ADOPTED 로 가는 유일한 경로는 [채택] 버튼."""
    monkeypatch.setattr(machine_mod, "get_adapter",
                        lambda name: agent_fake.FakeGoodAgent())
    loop = get_loop()
    loop.run(auto=True)
    _wait(loop, ("PASSED", "REJECTED", "FAILED"))
    assert loop.state == "PASSED"
    ops = OpsClient()
    ps = next(p for p in ops.param_sets() if p["id"] == loop.status()["param_set_id"])
    assert ps["status"] == "passed"      # adopted 가 아니다 — 자동은 여기까지
    assert ps["active"] is False


def test_forged_self_test_fails_generation(env, monkeypatch):
    monkeypatch.setattr(machine_mod, "get_adapter",
                        lambda name: agent_fake.FakeForgedAgent())
    loop = get_loop()
    loop.run()
    _wait(loop, ("FAILED", "REVIEW_DIFF"), timeout=120)
    assert loop.state == "FAILED"
    row = db.get_proposal(loop.gen_id)
    assert row["status"] == "failed"
    runs = db.agent_runs(gen_id=loop.gen_id, purpose="propose")
    assert len(runs) == machine_mod.MAX_ATTEMPTS      # 재시도 3회 전부 기록 (원문 보존)


def test_out_of_targets_fails(env, monkeypatch):
    monkeypatch.setattr(machine_mod, "get_adapter",
                        lambda name: agent_fake.FakeOutOfTargetsAgent())
    loop = get_loop()
    loop.run()
    _wait(loop, ("FAILED",), timeout=120)
    v = loop.status()["validation"]
    assert v["stage"] == 3 and any("target" in e for e in v["errors"])


def test_timeout_agent_fails_generation(env, monkeypatch):
    monkeypatch.setattr(machine_mod, "get_adapter",
                        lambda name: agent_fake.FakeTimeoutAgent())
    loop = get_loop()
    loop.run()
    _wait(loop, ("FAILED",), timeout=120)
    assert "타임아웃" in (db.agent_runs(gen_id=loop.gen_id)[0]["error"] or "")


def test_labels_guard_blocks_run(mock_server, tmp_path, monkeypatch):
    """라벨 <10 → 실행 불가 + 사유 (E4 §1.10)."""
    from app import config, db as db_mod, server_client
    mock_server.reset(tmp_path, labeled_count=3)
    monkeypatch.setenv("LEV_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LEV_SERVER_URL", mock_server.base_url)
    monkeypatch.setenv("LEV_EVOLUTION_TOKEN", "test-token")
    db_mod.close()
    config.reset_config()
    server_client.reset_ops()
    machine_mod.reset_loop()
    loop = get_loop()
    ok, reason = loop.can_run()
    assert not ok and "Not enough labeled sessions" in reason
    with pytest.raises(RuntimeError, match="labeled"):
        loop.run()
    db_mod.close()
    config.reset_config()
    server_client.reset_ops()
    machine_mod.reset_loop()


def test_promote_double_defense_409(loop):
    """게이트 미통과(candidate) 채택 시도 → 운영 서버 409 (E5 DoD 이중 방어)."""
    ops = OpsClient()
    active = ops.active_param_set()
    res = ops.param_sets_post(json_params=active["json_params"], origin="manual",
                              agent_name=None, rationale="미평가 후보",
                              parent_id=active["id"], version="v-test")
    with pytest.raises(OpsError) as e:
        ops.promote(res["id"], confirm=True)
    assert e.value.status == 409
