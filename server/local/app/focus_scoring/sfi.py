"""SFI — 고정 가중치 합산 (35/20/15/15/10/5).

7A 규칙 (SPEC §4.4): effort(동공) 성분은 산출 불가 → None 으로 두고
가용 가중치로 재정규화한다. 재정규화 사실은 quality 에 기록.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import rhythm


def compute(df: pd.DataFrame, params: dict, states: np.ndarray,
            events: dict, quality: dict) -> tuple[dict, int, str]:
    s = params["scoring"]
    measurable = np.isin(states, ("focus", "off_task", "blank_stare"))
    n_meas = int(measurable.sum())

    # --- gaze_on_page: 측정 가능 시간 중 on-page(집중+멍때림) 비율
    on_page = np.isin(states, ("focus", "blank_stare"))
    gaze_score = float(on_page.sum() / n_meas * 100) if n_meas else None

    # --- return_latency: 이탈 후 복귀 속도 (+ 이탈 빈도 페널티)
    rets = [e["return_sec"] for e in events["off_page"] if e["return_sec"] is not None]
    if not events["off_page"]:
        ret_score = 100.0  # 이탈 자체가 없음
    elif rets:
        mean_ret = float(np.mean(rets))
        ret_score = float(np.interp(
            mean_ret,
            [s["return_latency_good_sec"], s["return_latency_bad_sec"]],
            [100.0, 0.0]))
        dur_min = max(df["t_ms"].iloc[-1] / 60000.0, 1e-9)
        rate = len(events["off_page"]) / dur_min * 10  # 회/10분
        ret_score *= float(np.interp(rate, [0, s["offpage_rate_bad_per_10min"]], [1.0, 0.5]))
    else:
        ret_score = 0.0  # 이탈 후 세션 끝까지 미복귀

    # --- effort: 동공 필요 → 7A 산출 불가
    effort_score = None

    # --- fatigue: PERCLOS 기반
    fatigue_score = float(np.interp(
        events["perclos_max"], [0.0, s["fatigue_perclos_bad"]], [100.0, 0.0]))

    # --- rhythm
    rhythm_score = rhythm.component_score(df, states, params)

    # --- posture: head yaw / 거리 안정성
    meas_df = df.loc[measurable]
    if len(meas_df) >= 10:
        yaw_std = float(meas_df["head_yaw_deg"].std())
        dist_std = float(meas_df["distance_cm"].std())
        posture_score = float(np.mean([
            np.interp(yaw_std, [0, s["posture_yaw_std_bad_deg"]], [100, 0]),
            np.interp(dist_std, [0, s["posture_dist_std_bad_cm"]], [100, 0]),
        ]))
    else:
        posture_score = None

    components = {
        "gaze_on_page": _r(gaze_score),
        "return_latency": _r(ret_score),
        "effort": None,          # 정식 기기(NIR)에서 측정
        "fatigue": _r(fatigue_score),
        "rhythm": _r(rhythm_score),
        "posture": _r(posture_score),
    }

    # --- 가중 합산 (None 성분 제외 재정규화)
    weights = params["sfi_weights"]
    avail = {k: v for k, v in components.items() if v is not None}
    total_w = sum(weights[k] for k in avail)
    sfi = int(round(sum(v * weights[k] for k, v in avail.items()) / total_w)) if total_w else 0

    cov = quality["coverage"]
    confidence = ("high" if cov >= s["confidence_high_coverage"]
                  else "medium" if cov >= params["qc"]["coverage_min"] else "low")
    return components, sfi, confidence


def _r(v):
    """성분 점수 정규화 — None/NaN 은 '측정 불가'로 통일 (NaN 이 가중합에 새면 crash)."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(np.clip(v, 0, 100)), 1)
