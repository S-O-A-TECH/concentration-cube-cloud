"""E2 DoD: validate 1~3단계 5계열 / E4 DoD: ★4단계 재계산 대조 불일치 기각."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.loop.bounds import build_targets_bounds
from app.loop.sim import classify_core as core
from app.loop.validate import validate_proposal
from tests.mocks.synth_sessions import generate_batch

_PARAMS = json.loads(
    (Path(__file__).parent / "mocks" / "params_v1.json").read_text(encoding="utf-8"))
_TB = build_targets_bounds(["blank_stare"])


def _good(key="blank_stare.stare_dispersion_th", to=0.05) -> dict:
    new = copy.deepcopy(_PARAMS)
    node = new
    parts = key.split(".")
    for p in parts[:-1]:
        node = node[p]
    frm = node[parts[-1]]
    node[parts[-1]] = to
    st = {s: {"sens": 0.7, "spec": 0.9} for s in core.LABEL_STATES}
    return {"schema": "proposal.v1", "level": 1,
            "diagnosis": "blank_stare 미검출 — dispersion 임계가 낮다",
            "new_params": new,
            "changes": [{"key": key, "from": frm, "to": to, "reason": "feature_stats 근거"}],
            "self_test": {"n_simulate_runs": 2, "train_before": st, "train_after": st},
            "risk": "낮음", "rationale": "단일 키 상향"}


def _run(p, history=None, tb=_TB):
    return validate_proposal(p, _PARAMS, tb, history or [], workspace=None, do_recompute=False)


def test_good_proposal_passes_1to3():
    r = _run(_good())
    assert r.ok, r.errors


def test_stage1_schema_missing_fields():
    p = _good()
    del p["diagnosis"]
    del p["self_test"]
    r = _run(p)
    assert not r.ok and r.stage == 1
    assert any("diagnosis" in e for e in r.errors)
    assert any("self_test" in e for e in r.errors)


def test_stage2_bounds_violation():
    r = _run(_good(to=0.5))   # max 0.10
    assert not r.ok and r.stage == 2
    assert any("out of bounds" in e for e in r.errors)


def test_stage2_keyset_mismatch():
    p = _good()
    del p["new_params"]["qc"]
    r = _run(p)
    assert not r.ok and r.stage == 2
    assert any("missing key" in e for e in r.errors)


def test_stage2_sfi_sum_must_be_100():
    p = _good()
    p["new_params"]["sfi_weights"]["rhythm"] = 20   # 합 110
    p["changes"].append({"key": "sfi_weights.rhythm", "from": 10, "to": 20, "reason": "x"})
    r = _run(p)
    assert not r.ok and r.stage == 2
    assert any("100" in e for e in r.errors)


def test_stage2_pair_constraint_enter_lt_exit():
    tb = build_targets_bounds(["off_task"])
    p = _good(key="gaze.offpage_prob_th_enter", to=0.58)  # exit 0.55 보다 큼
    r = _run(p, tb=tb)
    assert not r.ok and r.stage == 2
    assert any("constraint violated" in e for e in r.errors)


def test_stage3_hidden_change_detected():
    p = _good()
    p["new_params"]["gaze"]["page_margin"] = 0.2   # changes 에 미신고
    r = _run(p)
    assert not r.ok and r.stage == 3
    assert any("hidden" in e for e in r.errors)


def test_stage3_outside_targets():
    p = _good(key="drowsy.perclos_th", to=0.3)     # 관측 실패 모드는 blank_stare 뿐
    r = _run(p)
    assert not r.ok and r.stage == 3
    assert any("target" in e for e in r.errors)


def test_stage3_too_many_changes():
    tb = build_targets_bounds(["blank_stare", "off_task", "fatigue", "sfi"])
    p = _good()
    keys = ["blank_stare.max_saccade_count_1s", "blank_stare.min_sec",
            "gaze.offpage_min_sec", "gaze.onpage_min_sec",
            "gaze.pose_offpage_yaw_deg", "gaze.pose_offpage_pitch_up_deg",
            "drowsy.perclos_th", "blink.long_blink_ms"]
    for k in keys:
        g, last = p["new_params"], k.split(".")
        for part in last[:-1]:
            g = g[part]
        frm = g[last[-1]]
        g[last[-1]] = frm * 1.01 if isinstance(frm, float) else frm + 1
        p["changes"].append({"key": k, "from": frm, "to": g[last[-1]], "reason": "x"})
    r = _run(p, tb=tb)
    assert not r.ok and r.stage == 3
    assert any("max 8" in e for e in r.errors)


def test_stage3_history_duplicate():
    p = _good()
    hist = [{"gen_id": "genX", "changes": [{"key": p["changes"][0]["key"],
                                            "to": p["changes"][0]["to"]}]}]
    r = _run(p, history=hist)
    assert not r.ok and r.stage == 3
    assert any("duplicate" in e for e in r.errors)


# ------------------------------------------------------------------ 4단계 (재계산 대조)

@pytest.fixture(scope="module")
def mini_workspace(tmp_path_factory):
    """train_lite + current_params 만 있는 미니 작업장."""
    import numpy as np
    import pandas as pd

    tmp = tmp_path_factory.mktemp("ws")
    (tmp / "data").mkdir()
    (tmp / "input").mkdir()
    sess_dir = tmp_path_factory.mktemp("sess")
    batch = generate_batch(sess_dir, n_train_min=3, n_holdout_min=0)
    frames = []
    for b in batch[:3]:
        df = pd.read_parquet(sess_dir / b["sid"] / "record.parquet",
                             columns=list(core.CLASSIFIER_COLUMNS))
        df["sid"] = b["sid"]
        t = df["t_ms"].to_numpy() / 1000.0
        truth = np.full(len(df), "", dtype=object)
        for seg in b["segments"]:
            truth[(t >= seg["t0"]) & (t < seg["t1"])] = seg["label"]
        df["truth"] = truth
        frames.append(df)
    pd.concat(frames, ignore_index=True).to_parquet(tmp / "data" / "train_lite.parquet",
                                                    index=False)
    (tmp / "input" / "current_params.json").write_text(
        json.dumps(_PARAMS), encoding="utf-8")
    return tmp


def _honest_self_test(ws: Path, params: dict) -> dict:
    import pandas as pd
    df = pd.read_parquet(ws / "data" / "train_lite.parquet")
    m = core.eval_train_lite(df, params)["per_state"]
    return {s: {"sens": v["sens"], "spec": v["spec"]} for s, v in m.items()}


def test_stage4_honest_self_test_passes(mini_workspace):
    p = _good(to=0.055)
    p["self_test"]["train_before"] = _honest_self_test(mini_workspace, _PARAMS)
    p["self_test"]["train_after"] = _honest_self_test(mini_workspace, p["new_params"])
    r = validate_proposal(p, _PARAMS, _TB, [], workspace=mini_workspace, do_recompute=True)
    assert r.ok, r.errors
    assert r.stage == 4
    assert r.recompute["train_after_recomputed"]


def test_stage4_forged_self_test_rejected(mini_workspace):
    """★수치 위조 = 재계산 대조에서 즉시 기각 (E4 DoD — 4단계 검증의 실증)."""
    p = _good(to=0.055)
    p["self_test"]["train_before"] = _honest_self_test(mini_workspace, _PARAMS)
    honest = _honest_self_test(mini_workspace, p["new_params"])
    forged = {s: {"sens": None if v["sens"] is None else
                  round(v["sens"] - 0.2 if v["sens"] >= 0.5 else v["sens"] + 0.2, 4),
                  "spec": v["spec"]} for s, v in honest.items()}
    p["self_test"]["train_after"] = forged
    r = validate_proposal(p, _PARAMS, _TB, [], workspace=mini_workspace, do_recompute=True)
    assert not r.ok and r.stage == 4
    assert any("mismatch" in e for e in r.errors)
