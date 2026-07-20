"""10Hz record 스키마 (v4.2) + parquet IO — 정본(canonical).

원본: web_cam_version_prototype/p0_webcam/focus_scoring/records.py (S2 시점 이식).
S3 에서 focus_scoring 패키지 전체가 이 위치로 이식되면 이 파일이 그 일부가 된다.
COLUMNS 가 chunk 업로드 계약의 정본이다 (SPEC-02 §2.3).

스키마 원칙 (phase7 §2.2): 웹캠(7A)에서 미측정인 필드(pupil, glint)도
필드를 빼지 않고 invalid bit 로 기록한다 — 7B·제품과 스키마 동일.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SCHEMA_VERSION = "4.2"
SAMPLE_RATE_HZ = 10

# (컬럼, dtype) — 순서 고정. parquet 저장 시 이 순서를 강제한다.
COLUMNS = [
    ("sample_index", "int64"),
    ("t_ms", "int64"),               # 세션(recording) 시작 기준 경과 ms — 윈도 끝 시각
    # validity bits
    ("face_valid", "bool"),
    ("both_eyes_valid", "bool"),
    ("gaze_valid", "bool"),
    ("pupil_valid", "bool"),         # 7A: 항상 False (NIR 필요)
    ("glint_valid", "bool"),         # 7A: 항상 False
    # gaze (page 평면 정규화 좌표: 0..1, 페이지 밖은 범위 밖 값)
    ("gaze_x", "float64"),
    ("gaze_y", "float64"),
    ("gaze_on_page_prob", "float64"),
    # eye
    ("ear_mean", "float64"),
    ("eye_openness", "float64"),     # 0(감음)~1(뜸)
    ("blink_count", "int64"),        # 이 100ms 윈도에서 완료된 blink 수
    ("long_blink_flag", "bool"),
    # 7B 전용 (7A에서는 NaN/0 + invalid bit)
    ("pupil_diameter_mm", "float64"),
    ("glint_count", "int64"),
    # head / posture
    ("head_yaw_deg", "float64"),
    ("head_pitch_deg", "float64"),
    ("head_roll_deg", "float64"),
    ("distance_cm", "float64"),
    # 환경
    ("frame_brightness", "float64"),
    ("valid_frame_ratio", "float64"),  # 윈도 내 face_valid 프레임 비율
    # 읽기 리듬 (phase1 §5.3)
    ("saccade_count_1s", "float64"),
    ("mean_fixation_ms", "float64"),
    ("gaze_dispersion_1s", "float64"),
    ("line_progression_flag", "bool"),
    ("region_alternation_1s", "float64"),
]

COLUMN_NAMES = [c for c, _ in COLUMNS]


def empty_record(sample_index: int, t_ms: int) -> dict:
    """전 필드 invalid 인 record (얼굴 미검출 윈도)."""
    r = {name: (False if dt == "bool" else (0 if dt == "int64" else float("nan")))
         for name, dt in COLUMNS}
    r["sample_index"] = sample_index
    r["t_ms"] = t_ms
    return r


def to_dataframe(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(records, columns=COLUMN_NAMES)
    for name, dt in COLUMNS:
        if dt == "bool":
            df[name] = df[name].fillna(False).astype(bool)
        elif dt == "int64":
            df[name] = df[name].fillna(0).astype("int64")
        else:
            df[name] = df[name].astype("float64")
    return df


def write_parquet(records: list[dict] | pd.DataFrame, path) -> pd.DataFrame:
    df = records if isinstance(records, pd.DataFrame) else to_dataframe(records)
    df.to_parquet(path, engine="pyarrow", index=False)
    return df


def read_parquet(path) -> pd.DataFrame:
    return pd.read_parquet(path, engine="pyarrow")


def integrity_check(df: pd.DataFrame, duration_sec: float | None = None) -> dict:
    """무결성 검증 (phase7 §3.2 항목 6): 개수·단조성·연속성."""
    n = len(df)
    issues = []
    if n == 0:
        return {"ok": False, "n_records": 0, "issues": ["empty"]}
    t = df["t_ms"].to_numpy()
    idx = df["sample_index"].to_numpy()
    if not np.all(np.diff(t) > 0):
        issues.append("t_ms not strictly monotonic")
    gaps = np.diff(idx)
    n_gap_samples = int(np.sum(gaps[gaps > 1] - 1)) if len(gaps) else 0
    if np.any(gaps < 1):
        issues.append("sample_index not increasing")
    expected = int(duration_sec * SAMPLE_RATE_HZ) if duration_sec else None
    if expected is not None and abs(n + n_gap_samples - expected) > SAMPLE_RATE_HZ:
        issues.append(f"count mismatch: got {n} (+{n_gap_samples} gap) expected ~{expected}")
    return {
        "ok": not issues,
        "n_records": n,
        "n_gap_samples": n_gap_samples,
        "expected": expected,
        "issues": issues,
    }
