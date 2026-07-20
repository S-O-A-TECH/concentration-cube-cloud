"""상태→파라미터 표적 매핑 + 키별 경계 (SPEC-06 §2.1 — targets_bounds.json 의 원천).

경계값은 웹캠 프로토 params/default.json (proto-0.1.0) 의 값을 중심으로 잡은 초안(E2 §1.6).
검증기는 "변경 키 ⊆ 관측된 실패 모드들의 표적 합집합"을 강제한다 — 오답과 무관한
파라미터를 건드리면 기각.
"""
from __future__ import annotations

# 실패 모드(상태) → 손댈 수 있는 파라미터 그룹
_OFF_TASK_KEYS = [
    "gaze.offpage_prob_th_enter", "gaze.offpage_prob_th_exit",
    "gaze.offpage_min_sec", "gaze.onpage_min_sec",
    "gaze.pose_offpage_yaw_deg", "gaze.pose_offpage_pitch_up_deg",
]
_BLANK_KEYS = [
    "blank_stare.stare_dispersion_th", "blank_stare.max_saccade_count_1s",
    "blank_stare.min_sec",
]
TARGETS: dict[str, list[str]] = {
    "blank_stare": _BLANK_KEYS,
    "off_task": _OFF_TASK_KEYS,
    # "focus 특이도 저하 → 위 두 그룹의 재조정" (SPEC-06 §2.1)
    "focus": _BLANK_KEYS + _OFF_TASK_KEYS,
    "fatigue": [
        "drowsy.perclos_th", "drowsy.perclos_window_sec", "drowsy.eye_closed_openness",
        "blink.ear_close_th", "blink.ear_open_th", "blink.min_close_frames",
        "blink.long_blink_ms", "blink.openness_lo", "blink.openness_hi",
    ],
    "sfi": [f"sfi_weights.{k}" for k in
            ("gaze_on_page", "return_latency", "effort", "fatigue", "rhythm", "posture")],
}

# 키별 {min, max, step} — step 은 안내용 (강제는 min/max 만)
BOUNDS: dict[str, dict] = {
    "blank_stare.stare_dispersion_th": {"min": 0.010, "max": 0.100, "step": 0.001},
    "blank_stare.max_saccade_count_1s": {"min": 0.0, "max": 2.0, "step": 0.1},
    "blank_stare.min_sec": {"min": 3.0, "max": 20.0, "step": 0.5},
    "gaze.offpage_prob_th_enter": {"min": 0.05, "max": 0.60, "step": 0.01},
    "gaze.offpage_prob_th_exit": {"min": 0.20, "max": 0.90, "step": 0.01},
    "gaze.offpage_min_sec": {"min": 0.5, "max": 6.0, "step": 0.1},
    "gaze.onpage_min_sec": {"min": 0.3, "max": 5.0, "step": 0.1},
    "gaze.pose_offpage_yaw_deg": {"min": 15.0, "max": 45.0, "step": 1.0},
    "gaze.pose_offpage_pitch_up_deg": {"min": 10.0, "max": 35.0, "step": 1.0},
    "drowsy.perclos_th": {"min": 0.10, "max": 0.50, "step": 0.01},
    "drowsy.perclos_window_sec": {"min": 30, "max": 120, "step": 5},
    "drowsy.eye_closed_openness": {"min": 0.05, "max": 0.40, "step": 0.01},
    "blink.ear_close_th": {"min": 0.12, "max": 0.25, "step": 0.01},
    "blink.ear_open_th": {"min": 0.20, "max": 0.35, "step": 0.01},
    "blink.min_close_frames": {"min": 1, "max": 5, "step": 1},
    "blink.long_blink_ms": {"min": 200, "max": 800, "step": 50},
    "blink.openness_lo": {"min": 0.05, "max": 0.30, "step": 0.01},
    "blink.openness_hi": {"min": 0.20, "max": 0.50, "step": 0.01},
    **{f"sfi_weights.{k}": {"min": 0, "max": 60, "step": 1} for k in
       ("gaze_on_page", "return_latency", "effort", "fatigue", "rhythm", "posture")},
}

# 짝 제약 — bounds 만으로 못 막는 관계 위반 (검증 2단계에서 함께 검사)
PAIR_CONSTRAINTS = [
    ("gaze.offpage_prob_th_enter", "<", "gaze.offpage_prob_th_exit"),
    ("blink.ear_close_th", "<", "blink.ear_open_th"),
    ("blink.openness_lo", "<", "blink.openness_hi"),
]

FAILURE_METRIC_TH = 0.85  # sens/spec 이 이보다 낮으면 그 상태를 '관측된 실패 모드'로 본다


def flatten(params: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in params.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, f"{key}."))
        else:
            out[key] = v
    return out


def unflatten_get(params: dict, dotkey: str):
    cur = params
    for part in dotkey.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def observed_failure_modes(per_state: dict) -> list[str]:
    """confusion 의 상태별 sens/spec → 실패 모드 목록. 전부 양호하면 최저 sens 상태 1개."""
    modes = []
    for st, m in per_state.items():
        sens = m.get("sens")
        spec = m.get("spec")
        if (sens is not None and sens < FAILURE_METRIC_TH) or \
           (spec is not None and spec < FAILURE_METRIC_TH):
            modes.append(st)
    if not modes and per_state:
        worst = min(per_state, key=lambda s: (per_state[s].get("sens") if per_state[s].get("sens") is not None else 1.0))
        modes = [worst]
    return modes


def build_targets_bounds(failure_modes: list[str]) -> dict:
    """작업장 input/targets_bounds.json 의 내용."""
    allowed: list[str] = []
    for m in failure_modes:
        for k in TARGETS.get(m, []):
            if k not in allowed:
                allowed.append(k)
    return {
        "schema": "targets_bounds.v1",
        "observed_failure_modes": failure_modes,
        "targets": TARGETS,
        "allowed_keys": allowed,
        "bounds": BOUNDS,
        "pair_constraints": [list(c) for c in PAIR_CONSTRAINTS],
        "notes": "변경 키 ≤8, allowed_keys 밖 금지, bounds 밖 금지, sfi_weights 합=100 유지",
    }
