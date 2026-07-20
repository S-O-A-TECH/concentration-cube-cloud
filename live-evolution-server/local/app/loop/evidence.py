"""Evidence Pack 빌더 (SPEC-06 §2) — AI 가 받는 증거는 전부 이 결정적 코드가 계산한다.

원천은 전부 운영 서버 조회 (이 서버는 원천 데이터를 갖지 않는다):
  overview / mistakes / sessions(labeled, split=train) / sessions/{sid}/detail

★ holdout 격리: 여기서 train 분할만 요청하므로 작업장에는 holdout 이 물리적으로
  존재하지 않는다 (workspace.verify_no_holdout 이 재확인).
★ 개인정보 0: classifier_inputs 는 수치 배열뿐 — 이름·생년월일·영상 없음 (SPEC-04 §3).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import db
from ..server_client import OpsClient
from . import bounds as bounds_mod
from .sim import classify_core as core

GATE_RULE_FALLBACK = ("holdout 모든 상태의 sens·spec 이 baseline 대비 -2%p 이내 저하이고 "
                      "표적 상태가 개선되어야 통과")
FEATURES = ("gaze_on_page_prob", "gaze_dispersion_1s", "saccade_count_1s")
_PCTS = (10, 25, 50, 75, 90)


def _percentiles(arr: np.ndarray) -> dict | None:
    if len(arr) == 0:
        return None
    ps = np.percentile(arr, _PCTS)
    return {f"p{p}": round(float(v), 4) for p, v in zip(_PCTS, ps)}


def _train_lite_frame(details: list[dict]) -> pd.DataFrame:
    """detail 응답들 → train_lite DataFrame (sid, truth, CLASSIFIER_COLUMNS)."""
    frames = []
    for d in sorted(details, key=lambda x: x["sid"]):
        ci = d["classifier_inputs"]
        df = pd.DataFrame({c: ci[c] for c in core.CLASSIFIER_COLUMNS})
        df["face_valid"] = df["face_valid"].astype(bool)
        df["gaze_valid"] = df["gaze_valid"].astype(bool)
        df["sid"] = d["sid"]
        t_sec = df["t_ms"].to_numpy() / 1000.0
        truth = np.full(len(df), "", dtype=object)
        for seg in d.get("labels", {}).get("segments", []):
            truth[(t_sec >= seg["t0"]) & (t_sec < seg["t1"])] = seg["label"]
        df["truth"] = truth
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["sid", "truth", *core.CLASSIFIER_COLUMNS])
    return pd.concat(frames, ignore_index=True)


def _feature_stats(train_lite: pd.DataFrame, params: dict) -> dict:
    """상태×피처 분포 — 맞은 bin vs 틀린 bin 분리 백분위 (SPEC-06 §2 feature_stats)."""
    out: dict = {"schema": "feature_stats.v1", "bin_sec": core.BIN_SEC, "states": {}}
    if len(train_lite) == 0:
        return out
    per_state: dict[str, dict[str, dict[str, list]]] = {
        st: {f: {"correct": [], "wrong": []} for f in FEATURES} for st in core.LABEL_STATES}
    for sid in sorted(train_lite["sid"].unique()):
        df = train_lite[train_lite["sid"] == sid].reset_index(drop=True)
        t_sec = df["t_ms"].to_numpy() / 1000.0
        n_bins = int(t_sec[-1] // core.BIN_SEC) + 1 if len(df) else 0
        truth = core.truth_bins_from_samples(t_sec, df["truth"].to_numpy(), n_bins)
        pred = core.predicted_bins(df, params)
        bin_idx = (t_sec // core.BIN_SEC).astype(int)
        for b, (t, p) in enumerate(zip(truth, pred)):
            if t is None or p == "invalid" or t not in per_state:
                continue
            bucket = "correct" if p == t else "wrong"
            m = bin_idx == b
            for f in FEATURES:
                per_state[t][f][bucket].extend(df.loc[m, f].dropna().tolist())
    for st, feats in per_state.items():
        out["states"][st] = {}
        for f, buckets in feats.items():
            out["states"][st][f] = {
                "correct_bins": _percentiles(np.asarray(buckets["correct"], dtype=float)),
                "wrong_bins": _percentiles(np.asarray(buckets["wrong"], dtype=float)),
                "n_correct_samples": len(buckets["correct"]),
                "n_wrong_samples": len(buckets["wrong"]),
            }
    return out


def _history_entries(limit: int = 5) -> list[dict]:
    """최근 ≤5 평가 세대 — train 자가시험 vs holdout 실제 (과적합 격차 학습, SPEC-06 §6)."""
    import json
    entries = []
    for p in db.list_proposals(limit=30):
        if p["status"] not in ("passed", "rejected", "adopted", "rolled_back"):
            continue
        prop = json.loads(p["proposal_json"] or "{}")
        report = json.loads(p["report_json"] or "{}")
        holdout_after = {}
        for st, m in (report.get("holdout", {}).get("per_state") or {}).items():
            after = m.get("sens", {})
            holdout_after[st] = {"sens": (after.get("after") if isinstance(after, dict) else None),
                                 "spec": (m.get("spec", {}) or {}).get("after") if isinstance(m.get("spec"), dict) else None}
        entries.append({
            "gen_id": p["gen_id"], "version": p["version"], "verdict": p["status"],
            "changes": prop.get("changes", []),
            "train_self_test_after": (prop.get("self_test") or {}).get("train_after"),
            "holdout_actual_after": holdout_after or None,
            "reject_reason": p["reject_reason"],
            "human_note": p["human_note"],
        })
        if len(entries) >= limit:
            break
    return entries


def build_evidence(ops: OpsClient) -> dict:
    """→ {files: {이름: 내용(dict|jsonl str)}, train_lite: DataFrame, meta: {...}}

    workspace.py 가 files 를 input/ 에, train_lite 를 data/ 에 쓴다.
    """
    overview = ops.overview()
    active = ops.active_param_set()
    if not active:
        raise RuntimeError("운영 서버에 활성 param_set 이 없습니다.")
    current_params = active["json_params"]

    mist = ops.mistakes(param_set_id=active["id"])
    per_state = mist.get("per_state") or {}
    failure_modes = bounds_mod.observed_failure_modes(per_state)

    sessions = ops.sessions(labeled=True, split="train")
    details = [ops.session_detail(s["sid"]) for s in sessions]
    train_lite = _train_lite_frame(details)

    confusion_json = {
        "schema": "confusion.v1",
        "param_set_id": active["id"], "param_set_version": active["version"],
        "n_train_sessions": len(sessions), "bins_total": mist.get("bins_total"),
        "confusion": mist.get("confusion"), "per_state": per_state,
        "observed_failure_modes": failure_modes,
    }
    mistakes_jsonl = "\n".join(
        __import__("json").dumps(m, ensure_ascii=False) for m in mist.get("mistakes", []))

    files = {
        "confusion.json": confusion_json,
        "mistakes.jsonl": mistakes_jsonl,
        "feature_stats.json": _feature_stats(train_lite, current_params),
        "current_params.json": current_params,
        "targets_bounds.json": bounds_mod.build_targets_bounds(failure_modes),
        "gate_rules.json": {
            "schema": "gate_rules.v1",
            "rule": overview.get("gate_rule") or GATE_RULE_FALLBACK,
            "max_drop_pp": 2.0,
            "recompute_tolerance_pp": 0.5,
        },
        "history.json": {"schema": "history.v1", "generations": _history_entries()},
    }
    holdout_sids = [s["sid"] for s in ops.sessions(labeled=True, split="holdout")]
    return {
        "files": files,
        "train_lite": train_lite,
        "meta": {
            "active_param_set": {"id": active["id"], "version": active["version"]},
            "n_train_sessions": len(sessions),
            "n_train_bins": mist.get("bins_total"),
            "failure_modes": failure_modes,
            "holdout_sids": holdout_sids,     # 작업장 밖 검증용 — 작업장에는 안 들어감
            "labeled_total": overview.get("labeled_realtime", 0),
        },
    }
