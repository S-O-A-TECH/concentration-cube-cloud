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
        return ["proposal 이 JSON 오브젝트가 아닙니다"]
    for f in _REQUIRED_FIELDS:
        if f not in p:
            errs.append(f"필수 필드 누락: {f}")
    if errs:
        return errs
    if p["schema"] != "proposal.v1":
        errs.append(f"schema 는 proposal.v1 이어야 합니다 (받음: {p['schema']})")
    if p["level"] != 1:
        errs.append("v0 은 level 1 만 지원합니다")
    if not isinstance(p["new_params"], dict) or not p["new_params"]:
        errs.append("new_params 는 비어있지 않은 오브젝트여야 합니다")
    if not isinstance(p["changes"], list) or not p["changes"]:
        errs.append("changes 는 비어있지 않은 배열이어야 합니다")
    else:
        for i, ch in enumerate(p["changes"]):
            if not isinstance(ch, dict) or not {"key", "from", "to", "reason"} <= set(ch):
                errs.append(f"changes[{i}] 는 key/from/to/reason 을 가져야 합니다")
    st = p["self_test"]
    if not isinstance(st, dict) or "train_before" not in st or "train_after" not in st:
        errs.append("self_test 는 train_before / train_after 를 가져야 합니다")
    else:
        for side in ("train_before", "train_after"):
            block = st[side]
            if not isinstance(block, dict):
                errs.append(f"self_test.{side} 형식 오류")
                continue
            for stt, m in block.items():
                if stt not in core.LABEL_STATES:
                    errs.append(f"self_test.{side} 의 알 수 없는 상태: {stt}")
                elif not isinstance(m, dict) or not {"sens", "spec"} <= set(m):
                    errs.append(f"self_test.{side}.{stt} 는 sens/spec 을 가져야 합니다")
    if not str(p.get("diagnosis", "")).strip():
        errs.append("diagnosis 가 비어 있습니다")
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
            errs.append(f"new_params 에 빠진 키: {missing}")
        if extra:
            errs.append(f"new_params 에 허용되지 않은 새 키: {extra}")
        return errs
    bounds = tb.get("bounds", {})
    for key, newv in new_flat.items():
        if newv == cur_flat[key]:
            continue
        if not _num(newv) and not isinstance(newv, str):
            errs.append(f"{key}: 값 형식 오류 ({type(newv).__name__})")
            continue
        if _num(cur_flat[key]) != _num(newv):
            errs.append(f"{key}: 타입 변경 금지 ({cur_flat[key]!r} → {newv!r})")
            continue
        b = bounds.get(key)
        if b and _num(newv):
            if not (b["min"] <= newv <= b["max"]):
                errs.append(f"{key}: {newv} 가 경계 [{b['min']}, {b['max']}] 밖입니다")
    for a, op, b_key in tb.get("pair_constraints", []):
        va, vb = new_flat.get(a), new_flat.get(b_key)
        if _num(va) and _num(vb) and op == "<" and not va < vb:
            errs.append(f"제약 위반: {a}({va}) < {b_key}({vb}) 이어야 합니다")
    sfi = {k: v for k, v in new_flat.items() if k.startswith("sfi_weights.")}
    if sfi and abs(sum(sfi.values()) - 100) > 1e-6:
        errs.append(f"sfi_weights 합이 100 이 아닙니다 (현재 {sum(sfi.values())})")
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
            errs.append(f"changes 의 알 수 없는 키: {ch['key']}")
            continue
        if _num(ch["from"]) and _num(cur_flat[ch["key"]]):
            if abs(ch["from"] - cur_flat[ch["key"]]) > 1e-9:
                errs.append(f"{ch['key']}: from({ch['from']}) 이 현재값({cur_flat[ch['key']]})과 다릅니다")
        elif ch["from"] != cur_flat[ch["key"]]:
            errs.append(f"{ch['key']}: from 이 현재값과 다릅니다")
    undeclared = sorted(set(changed) - set(declared))
    if undeclared:
        errs.append(f"changes 에 신고되지 않은 은닉 변경: {undeclared}")
    for k, v in declared.items():
        if k in changed and changed[k] != v:
            errs.append(f"{k}: changes.to({v}) 와 new_params({changed[k]}) 불일치")
        if k not in changed:
            errs.append(f"{k}: changes 에 있으나 new_params 는 현재값 그대로입니다")
    if len(changed) > MAX_CHANGES:
        errs.append(f"변경 키 {len(changed)}개 — 최대 {MAX_CHANGES}개")
    allowed = set(tb.get("allowed_keys", []))
    outside = sorted(set(changed) - allowed)
    if outside:
        errs.append(f"관측된 실패 모드의 표적(targets) 밖 변경: {outside}")
    this_set = {(k, json.dumps(v)) for k, v in changed.items()}
    for h in history:
        h_set = {(c["key"], json.dumps(c["to"])) for c in h.get("changes", [])}
        if h_set and h_set == this_set:
            errs.append(f"과거 세대({h.get('gen_id') or h.get('version')})와 동일한 변경 세트 — 중복 시도 금지")
            break
    return errs


# ------------------------------------------------------------------ 4단계: 재계산 대조

def _stage4_recompute(p: dict, workspace: Path) -> tuple[list[str], list[str], dict | None]:
    lite_path = workspace / "data" / "train_lite.parquet"
    if not lite_path.exists():
        return ["작업장에 train_lite.parquet 이 없어 재계산할 수 없습니다"], [], None
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
                    f"자가시험 불일치 [{st}.{metric}]: 에이전트 주장 {cv} vs 재계산 {ov} "
                    f"(허용 ±{RECOMPUTE_TOL * 100:.1f}%p) — 환각/조작 의심, 기각")
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
                        warns.append(f"train_before 불일치 [{st}.{metric}]: 주장 {cv} vs 재계산 {ov}")
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
        return ValidationResult(True, 3, warnings=["4단계 재계산은 실행하지 않았습니다"])
    errs, warns, recompute = _stage4_recompute(proposal, workspace)
    return ValidationResult(not errs, 4, errs, warns, recompute)
