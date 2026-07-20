"""focus_scoring 유닛테스트 — 웹캠 프로토에서 이식 (S3, SPEC-03 §1: 서버가 정본).

원본: web_cam_version_prototype/tests/test_focus_scoring.py (임포트만 app.* 로 조정).
"""
import json

import pytest

from app import focus_scoring
from app.focus_scoring import records as rec
from tests.synth import make_records


@pytest.fixture(scope="module")
def params():
    return focus_scoring.load_params()


def _score(scenario, params):
    df = rec.to_dataframe(make_records(scenario))
    return focus_scoring.score(df, params), df


def test_perfect_focus_20min(params):
    result, df = _score([("focus", 1200)], params)
    assert result["sfi"] is not None and result["sfi"] >= 85
    assert result["focused_minutes"] >= 19.0
    assert result["max_focus_streak_min"] >= 19.0
    states = {seg["state"] for seg in result["timeline"]}
    assert states == {"focus"}
    assert result["components"]["effort"] is None          # 7A: 동공 미측정
    assert result["quality"]["sfi_renormalized"] is True


def test_off_task_episodes(params):
    scenario = [("focus", 300), ("off_task", 30), ("focus", 300),
                ("off_task", 60), ("focus", 300)]
    result, df = _score(scenario, params)
    states = [seg["state"] for seg in result["timeline"]]
    assert "off_task" in states
    offs = [e for e in result["events"] if e["type"] == "off_page"]
    assert len(offs) == 2
    assert all(e["return_sec"] is not None for e in offs)
    perfect, _ = _score([("focus", 990)], params)
    assert result["sfi"] < perfect["sfi"]


def test_blank_stare_detection(params):
    scenario = [("focus", 300), ("blank_stare", 60), ("focus", 300)]
    result, df = _score(scenario, params)
    states = [seg["state"] for seg in result["timeline"]]
    assert "blank_stare" in states
    blanks = [e for e in result["events"] if e["type"] == "blank_stare"]
    assert len(blanks) >= 1
    # 멍때림은 focused_minutes 에 포함되지 않는다
    assert result["focused_minutes"] < 10.5


def test_low_coverage_unscorable(params):
    scenario = [("focus", 300), ("invalid", 400), ("focus", 100)]
    result, df = _score(scenario, params)
    assert result["sfi"] is None
    assert result["quality"]["unscorable_reason"] == "low_coverage"
    assert "측정" in result["coach_text"]["ko"]["student"]


def test_invalid_not_counted_as_off_task(params):
    """해석 금지 규칙: 눈 미검출은 이탈로 집계 금지 (phase2 §4.3)."""
    scenario = [("focus", 600), ("invalid", 60), ("focus", 600)]
    result, df = _score(scenario, params)
    states = [seg["state"] for seg in result["timeline"]]
    assert "invalid" in states and "off_task" not in states


def test_determinism(params):
    """순수 함수 보증: 같은 parquet 2회 채점 → 완전 동일."""
    df = rec.to_dataframe(make_records([("focus", 300), ("off_task", 30), ("focus", 60)]))
    r1 = focus_scoring.score(df, params)
    r2 = focus_scoring.score(df, params)
    assert json.dumps(r1, sort_keys=True, ensure_ascii=False) == \
           json.dumps(r2, sort_keys=True, ensure_ascii=False)


def test_parquet_roundtrip_and_integrity(tmp_path, params):
    recs = make_records([("focus", 120)])
    p = tmp_path / "record.parquet"
    df = rec.write_parquet(recs, p)
    df2 = rec.read_parquet(p)
    assert list(df2.columns) == rec.COLUMN_NAMES
    chk = rec.integrity_check(df2, duration_sec=120)
    assert chk["ok"], chk


def test_result_contract_keys(params):
    """SPEC §4.4 계약 필드 존재 검증."""
    result, _ = _score([("focus", 120)], params)
    for key in ("algo_version", "confidence", "sfi", "components", "focused_minutes",
                "max_focus_streak_min", "timeline", "events", "coach_text", "quality"):
        assert key in result, key
    for comp in ("gaze_on_page", "return_latency", "effort", "fatigue", "rhythm", "posture"):
        assert comp in result["components"], comp
