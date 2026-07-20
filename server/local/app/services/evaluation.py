"""라벨 vs 판정 대조 — 10초 bin 민감도/특이도 + 오답노트 (S5 §1.4, §1.6).

원본 로직: 웹캠 프로토 p0_webcam/evaluate.py (검증 하네스) 를 서버 서비스로 승격.
라벨 공백 구간과 invalid 판정 구간은 평가에서 제외한다 (측정 실패 ≠ 오답).
"""
from __future__ import annotations

import pandas as pd

BIN_SEC = 10
STATES = ("focus", "off_task", "blank_stare")


def bins_from_segments(segments: list[dict], total_sec: float, key: str) -> list[str | None]:
    """구간 목록 → 10초 bin 라벨 배열 (bin 중심이 구간 안에 있을 때만)."""
    n = int(total_sec // BIN_SEC)
    out: list[str | None] = [None] * n
    for seg in segments:
        for b in range(int(seg["t0"] // BIN_SEC), min(int(seg["t1"] // BIN_SEC) + 1, n)):
            mid = b * BIN_SEC + BIN_SEC / 2
            if seg["t0"] <= mid < seg["t1"]:
                out[b] = seg[key]
    return out


def session_counts(labels: list[dict], timeline: list[dict]) -> dict:
    """한 세션의 상태별 tp/fn/fp/tn 카운트 (세션 간 합산용)."""
    if not labels or not timeline:
        return {st: {"tp": 0, "fn": 0, "fp": 0, "tn": 0} for st in STATES}
    total = max(max(lb["t1"] for lb in labels), timeline[-1]["t1"])
    truth = bins_from_segments(labels, total, "label")
    pred = bins_from_segments(timeline, total, "state")
    counts = {st: {"tp": 0, "fn": 0, "fp": 0, "tn": 0} for st in STATES}
    for t, p in zip(truth, pred):
        if t is None or p is None or p == "invalid":
            continue
        for st in STATES:
            if t == st and p == st:
                counts[st]["tp"] += 1
            elif t == st:
                counts[st]["fn"] += 1
            elif p == st:
                counts[st]["fp"] += 1
            else:
                counts[st]["tn"] += 1
    return counts


def merge_counts(count_list: list[dict]) -> dict:
    merged = {st: {"tp": 0, "fn": 0, "fp": 0, "tn": 0} for st in STATES}
    for c in count_list:
        for st in STATES:
            for k in ("tp", "fn", "fp", "tn"):
                merged[st][k] += c[st][k]
    return merged


def metrics_from_counts(merged: dict) -> dict:
    """합산 카운트 → 상태별 sens/spec (분모 0 이면 None)."""
    out = {}
    for st in STATES:
        c = merged[st]
        sens = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else None
        spec = c["tn"] / (c["tn"] + c["fp"]) if c["tn"] + c["fp"] else None
        out[st] = {"sens": round(sens, 3) if sens is not None else None,
                   "spec": round(spec, 3) if spec is not None else None,
                   **c}
    return out


def session_mistakes(sid: str, labels: list[dict], timeline: list[dict],
                     df: pd.DataFrame | None) -> tuple[list[dict], dict, int]:
    """오답 bin 목록 + confusion 누적분 + 평가 bin 수 (SPEC-02 §2.5)."""
    if not labels or not timeline:
        return [], {}, 0
    total = max(max(lb["t1"] for lb in labels), timeline[-1]["t1"])
    truth = bins_from_segments(labels, total, "label")
    pred = bins_from_segments(timeline, total, "state")

    t_sec = (df["t_ms"].to_numpy() / 1000.0) if df is not None else None
    mistakes = []
    confusion: dict = {}
    n_bins = 0
    for b, (t, p) in enumerate(zip(truth, pred)):
        if t is None or p is None or p == "invalid":
            continue
        n_bins += 1
        confusion.setdefault(t, {})
        confusion[t][p] = confusion[t].get(p, 0) + 1
        if t == p:
            continue
        t0, t1 = b * BIN_SEC, (b + 1) * BIN_SEC
        feat = None
        if df is not None:
            m = (t_sec >= t0) & (t_sec < t1)
            if m.any():
                sub = df.loc[m]
                feat = {
                    "gaze_dispersion_1s": round(float(sub["gaze_dispersion_1s"].mean()), 4),
                    "saccade_count_1s": round(float(sub["saccade_count_1s"].mean()), 2),
                    "gaze_on_page_prob": round(float(sub["gaze_on_page_prob"].mean()), 3),
                }
        mistakes.append({"session_id": sid, "t0": t0, "t1": t1,
                         "truth": t, "predicted": p, "features_summary": feat})
    return mistakes, confusion, n_bins
