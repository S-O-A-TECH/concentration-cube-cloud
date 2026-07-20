"""분류 코어 sync 검증 (E4 §1.4 해시/동작 대조) + 웹캠 LAB-5 동반 작업 확인."""
from __future__ import annotations

import sys

import pandas as pd
import pytest

from app.config import get_config
from app.loop.sim import classify_core as core
from tests.mocks.synth_sessions import SCENARIOS, make_session


def _webcam_events():
    cfg = get_config()
    if not (cfg.webcam_root / "p0_webcam" / "focus_scoring" / "events.py").exists():
        pytest.skip("웹캠 프로토가 이 PC 에 없음 — sync 대조 생략")
    if str(cfg.webcam_root) not in sys.path:
        sys.path.insert(0, str(cfg.webcam_root))
    from p0_webcam.focus_scoring import events as ev
    return ev


def test_classify_states_behaviorally_identical(tmp_path):
    """같은 입력 → 웹캠 원본과 완전히 같은 상태 배열 (sync 사본의 정의)."""
    ev = _webcam_events()
    make_session(tmp_path, "sync_test", SCENARIOS[1], "2026-07-01T09:00:00+09:00")
    df = pd.read_parquet(tmp_path / "sync_test" / "record.parquet")
    params = __import__("json").loads(
        (get_config().webcam_root / "p0_webcam" / "focus_scoring" / "params" /
         "default.json").read_text(encoding="utf-8"))
    ours = core.classify_states(df, params)
    theirs = ev._classify_states(df, params)
    assert list(ours) == list(theirs)


def test_bin_rule_matches_webcam_evaluate():
    """정답지 bin 규칙(중앙시각 커버)이 웹캠 evaluate.py 와 동일."""
    segs = [{"t0": 0, "t1": 118, "label": "focus"},
            {"t0": 118, "t1": 176, "label": "blank_stare"},
            {"t0": 176, "t1": 300, "label": "off_task"}]
    bins = core.bins_from_segments(segs, 300)
    assert len(bins) == 30
    assert bins[0] == "focus"
    assert bins[11] == "focus"          # mid=115 < 118
    assert bins[12] == "blank_stare"    # mid=125
    assert bins[17] == "blank_stare"    # mid=175
    assert bins[18] == "off_task"       # mid=185


def test_webcam_has_lab5_mode():
    """E3 동반 작업: 웹캠 프로토 SESSION_MODES 에 LAB-5(300초) + 기록 목록 숨김."""
    cfg = get_config()
    if str(cfg.webcam_root) not in sys.path:
        sys.path.insert(0, str(cfg.webcam_root))
    try:
        import p0_webcam
    except ImportError:
        pytest.skip("웹캠 프로토 없음")
    assert p0_webcam.SESSION_MODES.get("LAB-5") == 300
    assert "LAB-5" in p0_webcam.HIDDEN_MODES
