"""승격 게이트 — 서버가 계산한다 (SPEC-03 §2: live-evolution 버그로 잘못 승격되는 일 방지).

기본 규칙: holdout 에서 모든 상태의 sens·spec 이 baseline 대비 2%p 이상
나빠지지 않고, 최소 한 지표는 유의미하게 개선(>0)되어야 한다.
"""
from __future__ import annotations

MAX_DROP = 0.02

GATE_RULE = ("holdout 모든 상태 sens·spec 이 baseline 대비 -2%p 이내 저하 "
             "& 최소 1개 지표 개선")


def evaluate_gate(baseline: dict, candidate: dict, max_drop: float = MAX_DROP,
                  targets: list[str] | None = None) -> dict:
    """상태별 {sens, spec} 두 벌(holdout) → {rule, passed, targets, notes}.

    저하 검사는 전 상태, 개선 요구는 표적 상태(targets — 파라미터 변경에서 유추,
    없으면 전 상태)에서만 본다. notes 는 사람이 읽는 한 문장짜리 문자열.
    """
    notes: list[str] = []
    passed = True
    check_states = targets or list(candidate.keys())
    improved = False
    for st, cand in candidate.items():
        base = baseline.get(st, {})
        for metric in ("sens", "spec"):
            b, c = base.get(metric), cand.get(metric)
            if b is None or c is None:
                continue                       # 데이터 부족 상태는 판정 제외
            delta = c - b
            if delta < -max_drop:
                passed = False
                notes.append(f"{st}.{metric} 저하 {round(-delta * 100, 1)}%p (> {max_drop * 100:.0f}%p)")
            elif delta > 1e-9 and st in check_states:
                improved = True
                notes.append(f"{st}.{metric} {b:.3f}→{c:.3f} 개선")
    if not improved:
        passed = False
        notes.append(f"표적 상태({'/'.join(check_states)}) 개선 없음")
    return {"rule": GATE_RULE, "passed": passed, "targets": check_states,
            "notes": "; ".join(notes) if notes else "통과"}
