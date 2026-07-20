"""제안 4중 검증 (SPEC-06 §5) — 통과한 것만 후보로 등록된다.

1 스키마: proposal.v1 전 필드 (diagnosis·self_test 누락 = 기각)
2 범위:   키 allowlist(현 params 와 동일 키셋) + bounds + 짝 제약 + sfi 합 100
3 규율:   변경 키 ≤8 + targets(관측 실패 모드) 안 + history 중복 세트 아님
4 ★자가시험 재계산 대조: 우리가 simulate(new_params) 를 직접 재실행해
  self_test.train_after 와 ±0.5%p 대조 — 불일치 = 환각/조작 = 즉시 기각.
  (train_before 불일치는 경고 — 기각 근거는 after 만, SPEC-06 §5 문언 그대로)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .bounds import flatten
from .sim import classify_core as core

MAX_CHANGES = 8
RECOMPUTE_TOL = 0.005  # ±0.5%p

_REQUIRED_FIELDS = ("schema", "level", "diagnosis", "new_params", "changes",
                    "self_test", "risk", "rationale")


@dataclass
class ValidationResult:
    ok: bool
    stage: int                    # 실패한 단계 (통과 시 4)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recompute: dict | None = None  # 4단계 재계산 결과 (per_state)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "stage": self.stage, "errors": self.errors,
                "warnings": self.warnings, "recompute": self.recompute}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ------------------------------------------------------------------ 1단계: 스키마

def _stage1_schema(p: dict) -> list[str]:
    errs = []
    if not isinstance(p, dict):
        return ["proposal is not a JSON object"]
    for f in _REQUIRED_FIELDS:
        if f not in p:
            errs.append(f"Missing required field: {f}")
    if errs:
        return errs
    if p["schema"] != "proposal.v1":
        errs.append(f"schema must be proposal.v1 (got: {p['schema']})")
    if p["level"] != 1:
        errs.append("v0 only supports level 1")
    if not isinstance(p["new_params"], dict) or not p["new_params"]:
        errs.append("new_params must be a non-empty object")
    if not isinstance(p["changes"], list) or not p["changes"]:
        errs.append("changes must be a non-empty array")
    else:
        for i, ch in enumerate(p["changes"]):
            if not isinstance(ch, dict) or not {"key", "from", "to", "reason"} <= set(ch):
                errs.append(f"changes[{i}] must have key/from/to/reason")
    st = p["self_test"]
    if not isinstance(st, dict) or "train_before" not in st or "train_after" not in st:
        errs.append("self_test must have train_before / train_after")
    else:
        for side in ("train_before", "train_after"):
            block = st[side]
            if not isinstance(block, dict):
                errs.append(f"self_test.{side} format error")
                continue
            for stt, m in block.items():
                if stt not in core.LABEL_STATES:
                    errs.append(f"self_test.{side} has unknown state: {stt}")
                elif not isinstance(m, dict) or not {"sens", "spec"} <= set(m):
                    errs.append(f"self_test.{side}.{stt} must have sens/spec")
    if not str(p.get("diagnosis", "")).strip():
        errs.append("diagnosis is empty")
    return errs


# ------------------------------------------------------------------ 2단계: 범위

def _stage2_bounds(p: dict, current_params: dict, tb: dict) -> list[str]:
    errs = []
    cur_flat = flatten(current_params)
    new_flat = flatten(p["new_params"])
    if set(cur_flat) != set(new_flat):
        # 에이전트 재시도 피드백이 온전하도록 넉넉히 (5개 잘림 → 재시도 낭비, 리뷰 지적)
        missing = sorted(set(cur_flat) - set(new_flat))[:20]
        extra = sorted(set(new_flat) - set(cur_flat))[:20]
        if missing:
            errs.append(f"missing key(s) in new_params: {missing}")
        if extra:
            errs.append(f"disallowed new key(s) in new_params: {extra}")
        return errs
    bounds = tb.get("bounds", {})
    for key, newv in new_flat.items():
        if newv == cur_flat[key]:
            continue
        if not _num(newv) and not isinstance(newv, str):
            errs.append(f"{key}: value format error ({type(newv).__name__})")
            continue
        if _num(cur_flat[key]) != _num(newv):
            errs.append(f"{key}: type change not allowed ({cur_flat[key]!r} → {newv!r})")
            continue
        b = bounds.get(key)
        if b and _num(newv):
            if not (b["min"] <= newv <= b["max"]):
                errs.append(f"{key}: {newv} is out of bounds [{b['min']}, {b['max']}]")
    for a, op, b_key in tb.get("pair_constraints", []):
        va, vb = new_flat.get(a), new_flat.get(b_key)
        if _num(va) and _num(vb) and op == "<" and not va < vb:
            errs.append(f"constraint violated: {a}({va}) < {b_key}({vb}) required")
    sfi = {k: v for k, v in new_flat.items() if k.startswith("sfi_weights.")}
    if sfi and abs(sum(sfi.values()) - 100) > 1e-6:
        errs.append(f"sfi_weights do not sum to 100 (currently {sum(sfi.values())})")
    return errs


# ------------------------------------------------------------------ 3단계: 변경 규율

def _changed_set(p: dict, current_params: dict) -> dict[str, object]:
    cur_flat = flatten(current_params)
    new_flat = flatten(p["new_params"])
    return {k: new_flat[k] for k in new_flat if k in cur_flat and new_flat[k] != cur_flat[k]}


def _stage3_discipline(p: dict, current_params: dict, tb: dict,
                       history: list[dict]) -> list[str]:
    errs = []
    cur_flat = flatten(current_params)
    changed = _changed_set(p, current_params)
    declared = {}
    for ch in p["changes"]:
        declared[ch["key"]] = ch["to"]
        if ch["key"] not in cur_flat:
            errs.append(f"unknown key in changes: {ch['key']}")
            continue
        if _num(ch["from"]) and _num(cur_flat[ch["key"]]):
            if abs(ch["from"] - cur_flat[ch["key"]]) > 1e-9:
                errs.append(f"{ch['key']}: from({ch['from']}) differs from the current value ({cur_flat[ch['key']]})")
        elif ch["from"] != cur_flat[ch["key"]]:
            errs.append(f"{ch['key']}: from differs from the current value")
    undeclared = sorted(set(changed) - set(declared))
    if undeclared:
        errs.append(f"undeclared hidden change(s) in changes: {undeclared}")
    for k, v in declared.items():
        if k in changed and changed[k] != v:
            errs.append(f"{k}: changes.to({v}) mismatches new_params({changed[k]})")
        if k not in changed:
            errs.append(f"{k}: present in changes but new_params keeps the current value")
    if len(changed) > MAX_CHANGES:
        errs.append(f"{len(changed)} changed keys — max {MAX_CHANGES}")
    allowed = set(tb.get("allowed_keys", []))
    outside = sorted(set(changed) - allowed)
    if outside:
        errs.append(f"change(s) outside the observed failure-mode targets: {outside}")
    this_set = {(k, json.dumps(v)) for k, v in changed.items()}
    for h in history:
        h_set = {(c["key"], json.dumps(c["to"])) for c in h.get("changes", [])}
        if h_set and h_set == this_set:
            errs.append(f"same change set as a past generation ({h.get('gen_id') or h.get('version')}) — duplicate attempt not allowed")
            break
    return errs


# ------------------------------------------------------------------ 4단계: 재계산 대조

def _stage4_recompute(p: dict, workspace: Path) -> tuple[list[str], list[str], dict | None]:
    lite_path = workspace / "data" / "train_lite.parquet"
    if not lite_path.exists():
        return ["train_lite.parquet not in workspace — cannot recompute"], [], None
    df = pd.read_parquet(lite_path)
    ours = core.eval_train_lite(df, p["new_params"])["per_state"]
    errs: list[str] = []
    warns: list[str] = []
    claimed_after = p["self_test"]["train_after"]
    for st in core.LABEL_STATES:
        ours_m = ours.get(st, {})
        claim_m = claimed_after.get(st, {})
        for metric in ("sens", "spec"):
            ov, cv = ours_m.get(metric), claim_m.get(metric)
            if ov is None and cv is None:
                continue
            if ov is None or cv is None or abs(float(ov) - float(cv)) > RECOMPUTE_TOL:
                errs.append(
                    f"self-test mismatch [{st}.{metric}]: agent claims {cv} vs recomputed {ov} "
                    f"(tolerance ±{RECOMPUTE_TOL * 100:.1f}%p) — suspected hallucination/fabrication, rejected")
    # train_before 는 경고만 — 기각 근거는 after (SPEC-06 §5)
    ours_before = None
    claimed_before = p["self_test"].get("train_before") or {}
    if claimed_before:
        cur_path = workspace / "input" / "current_params.json"
        if cur_path.exists():
            with open(cur_path, encoding="utf-8") as f:
                cur_params = json.load(f)
            ours_before = core.eval_train_lite(df, cur_params)["per_state"]
            for st in core.LABEL_STATES:
                for metric in ("sens", "spec"):
                    ov = (ours_before.get(st) or {}).get(metric)
                    cv = (claimed_before.get(st) or {}).get(metric)
                    if ov is not None and cv is not None and abs(float(ov) - float(cv)) > RECOMPUTE_TOL:
                        warns.append(f"train_before mismatch [{st}.{metric}]: claims {cv} vs recomputed {ov}")
    return errs, warns, {"train_after_recomputed": ours,
                         "train_before_recomputed": ours_before}


# ------------------------------------------------------------------ 진입점

def validate_proposal(proposal: dict, current_params: dict, targets_bounds: dict,
                      history: list[dict], workspace: Path | None = None,
                      do_recompute: bool = True) -> ValidationResult:
    errs = _stage1_schema(proposal)
    if errs:
        return ValidationResult(False, 1, errs)
    errs = _stage2_bounds(proposal, current_params, targets_bounds)
    if errs:
        return ValidationResult(False, 2, errs)
    errs = _stage3_discipline(proposal, current_params, targets_bounds, history)
    if errs:
        return ValidationResult(False, 3, errs)
    if not do_recompute or workspace is None:
        return ValidationResult(True, 3, warnings=["stage-4 recomputation was not run"])
    errs, warns, recompute = _stage4_recompute(proposal, workspace)
    return ValidationResult(not errs, 4, errs, warns, recompute)
