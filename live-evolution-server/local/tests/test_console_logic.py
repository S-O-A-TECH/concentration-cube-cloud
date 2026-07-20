"""E3 DoD: 클릭→구간 변환(공백 포함), 세션 시계 보간 정확도."""
from app.console import clicks_to_segments
from app.device_client import SessionClock


def test_clicks_to_segments_basic():
    clicks = [{"t": 0.0, "button": "focus"},
              {"t": 118.0, "button": "blank_stare"},
              {"t": 176.0, "button": "off_task"}]
    segs = clicks_to_segments(clicks, 300.0)
    assert segs == [{"t0": 0.0, "t1": 118.0, "label": "focus"},
                    {"t0": 118.0, "t1": 176.0, "label": "blank_stare"},
                    {"t0": 176.0, "t1": 300.0, "label": "off_task"}]


def test_pause_creates_gap():
    """[중단/기타] = segments 에서 빠진 시간 (라벨 없음 = 평가 제외 — SPEC-05 §4)."""
    clicks = [{"t": 0.0, "button": "focus"},
              {"t": 60.0, "button": "pause"},
              {"t": 90.0, "button": "focus"}]
    segs = clicks_to_segments(clicks, 120.0)
    assert segs == [{"t0": 0.0, "t1": 60.0, "label": "focus"},
                    {"t0": 90.0, "t1": 120.0, "label": "focus"}]
    covered = sum(s["t1"] - s["t0"] for s in segs)
    assert covered == 90.0  # 60~90 공백


def test_unsorted_and_zero_length_clicks():
    clicks = [{"t": 50.0, "button": "off_task"},
              {"t": 0.0, "button": "focus"},
              {"t": 50.0, "button": "off_task"}]   # 같은 t 중복 → 길이 0 은 버림
    segs = clicks_to_segments(clicks, 60.0)
    assert segs == [{"t0": 0.0, "t1": 50.0, "label": "focus"},
                    {"t0": 50.0, "t1": 60.0, "label": "off_task"}]


def test_click_after_end_ignored():
    clicks = [{"t": 0.0, "button": "focus"}, {"t": 400.0, "button": "off_task"}]
    segs = clicks_to_segments(clicks, 300.0)
    assert segs == [{"t0": 0.0, "t1": 300.0, "label": "focus"}]


def test_empty_clicks():
    assert clicks_to_segments([], 300.0) == []


def test_clock_interpolation_accuracy():
    """수신 t_sec + 경과 보간 — ±0.5s 요구 (SPEC-05 §3)."""
    c = SessionClock()
    c.feed(10.0, recording=True, mono_now=1000.0)
    t, warn = c.now_t(mono_now=1001.4)
    assert abs(t - 11.4) < 0.05
    assert warn is False


def test_clock_stale_warning():
    c = SessionClock()
    c.feed(10.0, recording=True, mono_now=1000.0)
    t, warn = c.now_t(mono_now=1004.5)   # 3초 초과 공백 (E3 리스크: WS 끊김 중 클릭)
    assert warn is True
    assert abs(t - 14.5) < 0.05          # 그래도 보간값으로 기록한다


def test_clock_not_recording_does_not_advance():
    c = SessionClock()
    c.feed(0.0, recording=False, mono_now=1000.0)  # 보정 중
    t, _ = c.now_t(mono_now=1002.0)
    assert t == 0.0


def test_clock_before_first_feed():
    t, warn = SessionClock().now_t(mono_now=1.0)
    assert t is None and warn is True
