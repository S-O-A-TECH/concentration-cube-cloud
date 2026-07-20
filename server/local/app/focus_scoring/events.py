"""Layer 1 — 이벤트 엔진: 샘플별 상태 분류 + 이벤트 추출.

상태 4종 (SPEC §4.3):
  focus / off_task(이탈) / blank_stare(멍때림) / invalid(측정 낮음)

해석 금지 규칙 (phase2 §4.3 — 코드로 강제):
- 눈 미검출 구간은 invalid — off_task 로 집계 금지
- 시선이 책에 있어도 리듬 소실 + 응시 고착이면 focus 아님 → blank_stare 후보
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .records import SAMPLE_RATE_HZ

STATES = ("focus", "off_task", "blank_stare", "invalid")


def analyze(df: pd.DataFrame, params: dict, quality: dict) -> tuple[np.ndarray, dict]:
    """→ (샘플별 상태 배열, 이벤트 dict)"""
    states = _classify_states(df, params)
    events = _extract_events(df, params, states)
    return states, events


# ---------------------------------------------------------------- states

def _classify_states(df: pd.DataFrame, params: dict) -> np.ndarray:
    n = len(df)
    hz = SAMPLE_RATE_HZ
    g = params["gaze"]
    bs = params["blank_stare"]

    measurable = (df["face_valid"] & df["gaze_valid"]).to_numpy()
    prob = df["gaze_on_page_prob"].to_numpy()

    # --- off-page: 히스테리시스 (enter/exit 임계 분리 + 최소 지속)
    off = np.zeros(n, dtype=bool)
    enter_n = max(1, int(g["offpage_min_sec"] * hz))
    exit_n = max(1, int(g["onpage_min_sec"] * hz))
    state_off = False
    run = 0
    for i in range(n):
        if not measurable[i]:
            # invalid 샘플은 off-page 판단을 유예 (상태 유지, run 리셋)
            run = 0
            off[i] = state_off
            continue
        if state_off:
            if prob[i] >= g["offpage_prob_th_exit"]:
                run += 1
                if run >= exit_n:
                    state_off = False
                    run = 0
                    # 복귀 시점을 소급 반영
                    off[max(0, i - exit_n + 1): i + 1] = False
            else:
                run = 0
        else:
            if prob[i] < g["offpage_prob_th_enter"]:
                run += 1
                if run >= enter_n:
                    state_off = True
                    off[max(0, i - enter_n + 1): i + 1] = True
                    run = 0
            else:
                run = 0
        off[i] = state_off if measurable[i] else off[i]

    # --- blank stare: on-page 인데 리듬 소실 + 응시 고착이 min_sec 지속
    # 순간 노이즈로 연속성이 끊기지 않도록 1.5초 롤링 평활 후 임계 비교
    smooth_n = max(1, int(1.5 * hz))
    disp = df["gaze_dispersion_1s"].rolling(smooth_n, min_periods=1).mean().to_numpy()
    sacc = df["saccade_count_1s"].rolling(smooth_n, min_periods=1).mean().to_numpy()
    stare_cand = (
        measurable & ~off
        & (disp < bs["stare_dispersion_th"])
        & (sacc <= bs["max_saccade_count_1s"])
    )
    stare = _sustained(stare_cand, min_len=int(bs["min_sec"] * hz))

    states = np.full(n, "focus", dtype=object)
    states[~measurable] = "invalid"
    states[measurable & off] = "off_task"
    states[stare & measurable & ~off] = "blank_stare"
    return states


def _sustained(mask: np.ndarray, min_len: int) -> np.ndarray:
    """min_len 이상 연속인 구간만 남긴다."""
    out = np.zeros_like(mask)
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i >= min_len:
                out[i:j] = True
            i = j
        else:
            i += 1
    return out


# ---------------------------------------------------------------- events

def _extract_events(df: pd.DataFrame, params: dict, states: np.ndarray) -> dict:
    hz = SAMPLE_RATE_HZ
    n = len(df)
    t_sec = df["t_ms"].to_numpy() / 1000.0

    # off-page 에피소드 + return latency
    off_events = []
    i = 0
    while i < n:
        if states[i] == "off_task":
            j = i
            while j < n and states[j] == "off_task":
                j += 1
            ret = None
            if j < n and states[j] in ("focus", "blank_stare"):
                ret = round(float(t_sec[j] - t_sec[i]), 1)
            off_events.append({"type": "off_page", "t": round(float(t_sec[i]), 1),
                               "return_sec": ret})
            i = j
        else:
            i += 1

    # 졸음 후보: PERCLOS (rolling window)
    d = params["drowsy"]
    openness = df["eye_openness"].to_numpy()
    meas = (df["face_valid"] & df["both_eyes_valid"]).to_numpy()
    win = int(d["perclos_window_sec"] * hz)
    drowsy_onsets = []
    perclos_series = np.zeros(n)
    if n >= win:
        closed = (openness < d["eye_closed_openness"]) & meas
        csum_c = np.cumsum(np.concatenate([[0], closed.astype(float)]))
        csum_m = np.cumsum(np.concatenate([[0], meas.astype(float)]))
        was_drowsy = False
        for i in range(win, n):
            m = csum_m[i] - csum_m[i - win]
            p = (csum_c[i] - csum_c[i - win]) / m if m > win * 0.5 else 0.0
            perclos_series[i] = p
            if p >= d["perclos_th"] and not was_drowsy:
                drowsy_onsets.append({"type": "drowsy_onset", "t": round(float(t_sec[i]), 1)})
                was_drowsy = True
            elif p < d["perclos_th"] * 0.6:
                was_drowsy = False

    # blank stare 에피소드 (이벤트 목록용)
    blank_events = []
    i = 0
    while i < n:
        if states[i] == "blank_stare":
            j = i
            while j < n and states[j] == "blank_stare":
                j += 1
            blank_events.append({"type": "blank_stare", "t": round(float(t_sec[i]), 1),
                                 "duration_sec": round(float(t_sec[j - 1] - t_sec[i]), 1)})
            i = j
        else:
            i += 1

    return {
        "list": off_events + drowsy_onsets + blank_events,
        "off_page": off_events,
        "drowsy": drowsy_onsets,
        "blank_stare": blank_events,
        "perclos_max": round(float(perclos_series.max()), 3) if n else 0.0,
    }


# ---------------------------------------------------------------- timeline

def build_timeline(df: pd.DataFrame, states: np.ndarray, params: dict) -> list[dict]:
    """샘플 상태 → 병합된 타임라인 구간 (짧은 구간은 이웃에 흡수)."""
    n = len(states)
    if n == 0:
        return []
    t_sec = df["t_ms"].to_numpy() / 1000.0
    min_len = int(params["timeline"]["min_segment_sec"] * SAMPLE_RATE_HZ)

    # 1) 원시 구간
    segs = []
    start = 0
    for i in range(1, n + 1):
        if i == n or states[i] != states[start]:
            segs.append([start, i, states[start]])
            start = i
    # 2) 짧은 구간은 앞 구간에 흡수 (invalid 는 흡수하지 않고 유지 — 정직성)
    merged = []
    for s in segs:
        if merged and (s[1] - s[0]) < min_len and s[2] != "invalid" and merged[-1][2] != "invalid":
            merged[-1][1] = s[1]
        elif merged and merged[-1][2] == s[2]:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    # 3) 인접 동일 상태 재병합
    out = []
    for s in merged:
        if out and out[-1][2] == s[2]:
            out[-1][1] = s[1]
        else:
            out.append(s)
    return [
        {"t0": round(float(t_sec[a]), 1),
         "t1": round(float(t_sec[min(b, n - 1)]), 1),
         "state": st}
        for a, b, st in out
    ]


def live_state(recent: pd.DataFrame, params: dict) -> str:
    """세션 중 1Hz 상태 표시용 간이 분류 (최근 ~2초 records)."""
    if len(recent) == 0:
        return "invalid"
    meas = (recent["face_valid"] & recent["gaze_valid"]).to_numpy()
    if np.mean(meas) < 0.5:
        return "invalid"
    prob = float(recent.loc[meas, "gaze_on_page_prob"].mean())
    if prob < params["gaze"]["offpage_prob_th_enter"]:
        return "off_task"
    bs = params["blank_stare"]
    if (float(recent["gaze_dispersion_1s"].mean()) < bs["stare_dispersion_th"]
            and float(recent["saccade_count_1s"].mean()) <= bs["max_saccade_count_1s"]):
        return "blank_stare"
    return "focus"
