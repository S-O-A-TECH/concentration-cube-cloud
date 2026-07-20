"""E1 DoD: server_client 전 메서드 (mock 대상) + 오류의 사용자 문구 변환 + 단일 경유."""
from pathlib import Path

import pytest

from app.server_client import OpsClient, OpsError
from tests.conftest import TEST_TOKEN


def test_full_wrapper_roundtrip(env):
    ops = OpsClient()
    assert ops.health()["ok"] is True
    ov = ops.overview()
    assert ov["labeled_realtime"] >= 10
    assert ov["active_param_set"]["version"] == "v1.0"

    sessions = ops.sessions(labeled=True, split="train")
    assert len(sessions) >= 10
    sid = sessions[0]["sid"]

    detail = ops.session_detail(sid)
    assert detail["labels"]["method"] == "realtime_instructed"
    assert set(detail["classifier_inputs"]) >= {"t_ms", "gaze_on_page_prob"}

    assert ops.labels_get(sid)["segments"]
    mist = ops.mistakes()
    assert mist["bins_total"] > 0
    assert "blank_stare" in mist["per_state"]

    ps = ops.param_sets()
    assert ps[0]["active"] is True
    assert ops.active_param_set()["id"] == ps[0]["id"]
    assert ops.jobs() == []
    assert ops.session_runs(sid)[0]["param_set_version"] == "v1.0"


def test_label_immutability_409(env):
    ops = OpsClient()
    sid = ops.sessions(labeled=True)[0]["sid"]
    with pytest.raises(OpsError) as e:
        ops.labels_post(sid, "admin", "realtime_instructed", "기본5분",
                        [{"t0": 0, "t1": 10, "label": "focus"}])
    assert e.value.status == 409


def test_exclude_restore_flow(env):
    ops = OpsClient()
    sid = ops.sessions(labeled=True, split="train")[0]["sid"]
    n_before = len(ops.sessions(labeled=True, split="train"))
    ops.exclude(sid, "테스트 제외")
    assert len(ops.sessions(labeled=True, split="train")) == n_before - 1
    # 제외 ≠ 삭제 — include_excluded 로는 보인다
    all_s = {s["sid"]: s for s in ops.sessions(include_excluded=True)}
    assert all_s[sid]["excluded"] is True
    ops.restore(sid, "오제외 복원")
    assert len(ops.sessions(labeled=True, split="train")) == n_before


def test_token_error_user_message(env):
    bad = OpsClient(token="wrong-token")
    with pytest.raises(OpsError) as e:
        bad.overview()
    assert "EVOLUTION_TOKEN" in e.value.user_msg


def test_connection_error_user_message(env, tmp_path):
    dead = OpsClient(base_url="http://127.0.0.1:1", token=TEST_TOKEN)
    with pytest.raises(OpsError) as e:
        dead.health()
    assert "Cannot connect" in e.value.user_msg


def test_single_gateway_no_direct_httpx(env):
    """E1 DoD: 래퍼 밖에서 운영 서버 httpx 직접 호출 0건 (grep 검사의 코드화).

    허용: server_client.py(래퍼), device_client.py(기기 — 운영 서버 아님),
          qwen_api.py(에이전트가 부르는 외부 LLM — 운영 서버 아님), run.py(자기 자신 health).
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for p in app_dir.rglob("*.py"):
        if p.name in ("server_client.py", "device_client.py", "qwen_api.py"):
            continue
        text = p.read_text(encoding="utf-8")
        if "httpx." in text:
            offenders.append(p.name)
    assert offenders == [], f"운영 서버 호출은 server_client 단일 경유여야 합니다: {offenders}"
