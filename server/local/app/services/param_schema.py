"""json_params 검증 스키마 — S1 리스크 대응: "JSONB 의 스키마 없는 자유도".

웹캠 프로토 focus_scoring/params/default.json 구조를 pydantic 으로 고정한다.
extra="forbid" — Live Evolution 에이전트의 제안이 모르는 키를 들고 오면 저장 전에 거부된다
(새 키가 필요하면 이 스키마부터 바꾸는 것이 규율 — SPEC-03 경계값 검사와 같은 철학).
"""
from pydantic import BaseModel, ConfigDict


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureParams(_Section):
    camera_index: int
    width: int
    height: int
    target_fps: int


class BlinkParams(_Section):
    ear_close_th: float
    ear_open_th: float
    min_close_frames: int
    long_blink_ms: int
    openness_lo: float
    openness_hi: float


class GazeParams(_Section):
    offpage_prob_th_enter: float
    offpage_prob_th_exit: float
    offpage_min_sec: float
    onpage_min_sec: float
    page_margin: float
    prob_sigmoid_scale: float
    pose_offpage_yaw_deg: float
    pose_offpage_pitch_up_deg: float


class CalibrationParams(_Section):
    points: int
    countdown_sec: float
    dwell_sec: float
    min_samples_per_point: int
    max_residual: float


class SaccadeParams(_Section):
    velocity_th_units_per_s: float
    min_fixation_ms: int


class RhythmParams(_Section):
    line_progression_window_sec: float
    line_slope_min_units_per_s: float
    line_slope_max_units_per_s: float
    healthy_saccade_min_1s: float
    healthy_saccade_max_1s: float


class BlankStareParams(_Section):
    stare_dispersion_th: float
    max_saccade_count_1s: float
    min_sec: float


class DrowsyParams(_Section):
    perclos_window_sec: int
    perclos_th: float
    eye_closed_openness: float


class QcParams(_Section):
    coverage_min: float


class TimelineParams(_Section):
    min_segment_sec: float


class SfiWeights(_Section):
    gaze_on_page: float
    return_latency: float
    effort: float
    fatigue: float
    rhythm: float
    posture: float


class ScoringParams(_Section):
    return_latency_good_sec: float
    return_latency_bad_sec: float
    offpage_rate_bad_per_10min: float
    posture_yaw_std_bad_deg: float
    posture_dist_std_bad_cm: float
    fatigue_perclos_bad: float
    confidence_high_coverage: float


class FocusScoringParams(_Section):
    """params/default.json 전체 — json_params 컬럼에 저장되는 값의 계약."""
    param_set_version: str
    capture: CaptureParams
    blink: BlinkParams
    gaze: GazeParams
    calibration: CalibrationParams
    saccade: SaccadeParams
    rhythm: RhythmParams
    blank_stare: BlankStareParams
    drowsy: DrowsyParams
    qc: QcParams
    timeline: TimelineParams
    sfi_weights: SfiWeights
    scoring: ScoringParams


def validate_params(raw: dict) -> dict:
    """검증 통과 시 정규화된 dict 반환, 실패 시 pydantic.ValidationError."""
    return FocusScoringParams.model_validate(raw).model_dump()
