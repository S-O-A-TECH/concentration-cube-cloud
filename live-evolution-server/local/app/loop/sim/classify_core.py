# -*- coding: utf-8 -*-
"""상태 분류 코어 — 웹캠 프로토 focus_scoring/events.py 의 sync 사본 (SPEC-06 §3).

[SYNC] 출처: web_cam_version_prototype/p0_webcam/focus_scoring/events.py
        (_classify_states, _sustained — proto-0.1.0, 2026-07-04 이식)
        운영 서버 채점 엔진과 "같은 코드"여야 게이트가 측정하는 지표를 같은 방식으로
        재현한다. 원본이 바뀌면 tests/test_sim_sync.py 가 실패한다 — 그때 이 파일을
        다시 동기화할 것. 임의 수정 금지.

이 모듈 하나를 세 곳이 공유한다 (드리프트 방지의 핵심):
  1) validate 4단계 — 에이전트 자가시험(train_after)의 재계산 대조
  2) mock 운영 서버 — mistakes/evaluate 계산 (S5 전까지의 대역)
  3) 에이전트 작업장의 tools/simulate.py — 이 파일의 소스 텍스트를 그대로 이어붙여 생성

표준 대조 규칙 (게이트·오답노트·자가시험 공통):
  - 10Hz 샘플 분류 → 10초 bin 으로 집계 (bin 내 다수결, invalid ≥50% 면 invalid)
  - 정답지 bin = 세그먼트가 bin 중앙시각을 덮을 때 (웹캠 evaluate.py 와 동일 규칙)
  - truth 없음(중단/기타 공백) 또는 predicted invalid 인 bin 은 대조 제외
    (측정 실패 ≠ 이탈 — 전 프로젝트 공통 원칙)
  - sens/spec 은 전 세션 bin 을 합산(pooled)해 상태별 계산
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SAMPLE_RATE_HZ = 10
BIN_SEC = 10
STATES = ("focus", "off_task", "blank_stare", "invalid")
LABEL_STATES = ("focus", "off_task", "blank_stare")

# 분류기가 실제로 소비하는 컬럼 — train_lite.parquet 의 계약 (+ sid, truth)
CLASSIFIER_COLUMNS = ("t_ms", "face_valid", "gaze_valid", "gaze_on_page_prob",
                      "gaze_dispersion_1s", "saccade_count_1s")


# ---------------------------------------------------------------- [SYNC] 분류 코어

def classify_states(df: pd.DataFrame, params: dict) -> np.ndarray:
    """10Hz record → 샘플별 상태. 원본: events._classify_states (수정 금지)."""
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
    """min_len 이상 연속인 구간만 남긴다. 원본: events._sustained (수정 금지)."""
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


# ---------------------------------------------------------------- bin 대조 규칙

def predicted_bins(df: pd.DataFrame, params: dict) -> list:
    """세션 record → 10초 bin 별 predicted 상태 목록.

    bin 판정: invalid 샘플 ≥50% → invalid, 아니면 non-invalid 샘플의 다수결
    (동률이면 STATES 순서 우선 — 결정적).
    """
    states = classify_states(df, params)
    t_sec = df["t_ms"].to_numpy() / 1000.0
    n_bins = int(t_sec[-1] // BIN_SEC) + 1 if len(df) else 0
    out = []
    for b in range(n_bins):
        m = (t_sec >= b * BIN_SEC) & (t_sec < (b + 1) * BIN_SEC)
        st = states[m]
        if len(st) == 0:
            out.append("invalid")
            continue
        n_invalid = int(np.sum(st == "invalid"))
        if n_invalid * 2 >= len(st):
            out.append("invalid")
            continue
        counts = {s: int(np.sum(st == s)) for s in LABEL_STATES}
        out.append(max(LABEL_STATES, key=lambda s: (counts[s], -LABEL_STATES.index(s))))
    return out


def bins_from_segments(segments: list, total_sec: float, key: str = "label") -> list:
    """정답지 세그먼트 → bin 별 truth (bin 중앙시각 커버 규칙 — 웹캠 evaluate.py 동일)."""
    n = int(total_sec // BIN_SEC)
    out = [None] * n
    for seg in segments:
        for b in range(int(seg["t0"] // BIN_SEC), min(int(seg["t1"] // BIN_SEC) + 1, n)):
            mid = b * BIN_SEC + BIN_SEC / 2
            if seg["t0"] <= mid < seg["t1"]:
                out[b] = seg[key]
    return out


def confusion_and_metrics(pairs: list) -> dict:
    """(truth, predicted) bin 쌍 목록 → 혼동행렬 + 상태별 sens/spec.

    pairs 에는 이미 '대조 제외' 규칙이 적용돼 있어야 한다
    (truth None / predicted invalid 제거). 상태별 1-vs-rest 로 계산.
    """
    confusion = {t: {p: 0 for p in LABEL_STATES} for t in LABEL_STATES}
    for t, p in pairs:
        if t in confusion and p in LABEL_STATES:
            confusion[t][p] += 1
    per_state = {}
    for st in LABEL_STATES:
        tp = confusion[st][st]
        fn = sum(confusion[st][p] for p in LABEL_STATES if p != st)
        fp = sum(confusion[t][st] for t in LABEL_STATES if t != st)
        tn = sum(confusion[t][p] for t in LABEL_STATES for p in LABEL_STATES
                 if t != st and p != st)
        per_state[st] = {
            "sens": round(tp / (tp + fn), 4) if tp + fn else None,
            "spec": round(tn / (tn + fp), 4) if tn + fp else None,
            "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        }
    return {"confusion": confusion, "per_state": per_state, "n_bins": len(pairs)}


def eval_sessions(sessions: list, params: dict) -> dict:
    """[{df, segments}] → pooled 대조. df 는 CLASSIFIER_COLUMNS 를 갖는 DataFrame."""
    pairs = []
    for s in sessions:
        df = s["df"]
        if len(df) == 0:
            continue
        total = float(df["t_ms"].iloc[-1]) / 1000.0
        truth = bins_from_segments(s["segments"], total)
        pred = predicted_bins(df, params)
        for t, p in zip(truth, pred):
            if t is None or p == "invalid":
                continue
            pairs.append((t, p))
    return confusion_and_metrics(pairs)


# ---------------------------------------------------------------- train_lite 평가

def truth_bins_from_samples(t_sec: np.ndarray, truth: np.ndarray, n_bins: int) -> list:
    """샘플별 truth (세그먼트에서 유래) → bin truth. 중앙시각에 가장 가까운 샘플의
    truth 를 취한다 — 연속 세그먼트에서는 midpoint 커버 규칙과 동치."""
    out = [None] * n_bins
    if len(t_sec) == 0:
        return out
    for b in range(n_bins):
        mid = b * BIN_SEC + BIN_SEC / 2
        i = int(np.argmin(np.abs(t_sec - mid)))
        if abs(float(t_sec[i]) - mid) > 1.0:
            continue  # 샘플 공백 — 대조 제외
        v = truth[i]
        out[b] = v if isinstance(v, str) and v else None
    return out


def eval_train_lite(df_all: pd.DataFrame, params: dict) -> dict:
    """train_lite (sid + truth + CLASSIFIER_COLUMNS) 전체 → pooled 상태별 sens/spec.

    validate 4단계 재계산과 작업장 tools/simulate.py 가 똑같이 이 함수를 부른다 —
    ±0.5%p 대조가 성립하는 이유는 '같은 코드'이기 때문이다 (SPEC-06 §5).
    """
    pairs = []
    for sid in sorted(df_all["sid"].unique()):
        df = df_all[df_all["sid"] == sid].reset_index(drop=True)
        t_sec = df["t_ms"].to_numpy() / 1000.0
        n_bins = int(t_sec[-1] // BIN_SEC) + 1 if len(df) else 0
        truth = truth_bins_from_samples(t_sec, df["truth"].to_numpy(), n_bins)
        pred = predicted_bins(df, params)
        for t, p in zip(truth, pred):
            if t is None or p == "invalid":
                continue
            pairs.append((t, p))
    return confusion_and_metrics(pairs)
