"""읽기 리듬 — 세션 수준 리듬 피처와 rhythm 성분 점수.

phase7 검증 항목 4(집중 vs 멍때림의 리듬 피처 분리도)의 분석 대상이기도 하다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def session_features(df: pd.DataFrame, states: np.ndarray) -> dict:
    """상태군별 리듬 피처 분포 요약 (evaluate.py 의 분리도 분석 입력)."""
    out = {}
    for st in ("focus", "blank_stare"):
        m = states == st
        if m.sum() < 10:
            out[st] = None
            continue
        sub = df.loc[m]
        out[st] = {
            "n": int(m.sum()),
            "saccade_count_1s_mean": round(float(sub["saccade_count_1s"].mean()), 3),
            "gaze_dispersion_1s_mean": round(float(sub["gaze_dispersion_1s"].mean()), 4),
            "mean_fixation_ms_mean": round(float(sub["mean_fixation_ms"].mean()), 1),
            "line_progression_pct": round(float(sub["line_progression_flag"].mean()) * 100, 1),
        }
    return out


def component_score(df: pd.DataFrame, states: np.ndarray, params: dict) -> float | None:
    """rhythm 성분 (0~100): on-page 시간 중 '건강한 읽기 리듬' 비율."""
    r = params["rhythm"]
    on_page = np.isin(states, ("focus", "blank_stare"))
    if on_page.sum() < 10:
        return None
    sub = df.loc[on_page]
    sacc = sub["saccade_count_1s"].to_numpy()
    healthy = (
        (sacc >= r["healthy_saccade_min_1s"])
        & (sacc <= r["healthy_saccade_max_1s"])
    ) | sub["line_progression_flag"].to_numpy()
    return float(np.clip(np.mean(healthy) * 100.0, 0, 100))
