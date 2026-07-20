"""Layer 0 — QC 게이트: invalid bits → coverage, 점수 가능 여부.

규칙 (phase2 §4.1): coverage < coverage_min 이면 점수 없음 + 안내.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def assess(df: pd.DataFrame, params: dict) -> dict:
    n = len(df)
    if n == 0:
        return {
            "scorable": False, "coverage": 0.0, "n_records": 0,
            "invalid_summary": {"empty": True},
            "reason": "no_data",
        }
    face = df["face_valid"].to_numpy()
    gaze = df["gaze_valid"].to_numpy()
    measurable = face & gaze
    coverage = float(np.mean(measurable))

    invalid_summary = {
        "no_face_pct": round(float(np.mean(~face)) * 100, 1),
        "face_no_gaze_pct": round(float(np.mean(face & ~gaze)) * 100, 1),
        "pupil_unavailable": bool(not df["pupil_valid"].any()),   # 7A에서는 항상 True
        "glint_unavailable": bool(not df["glint_valid"].any()),
    }
    scorable = coverage >= params["qc"]["coverage_min"]
    return {
        "scorable": scorable,
        "coverage": round(coverage, 4),
        "n_records": n,
        "measurable_mask": measurable,
        "invalid_summary": invalid_summary,
        "reason": None if scorable else "low_coverage",
    }
