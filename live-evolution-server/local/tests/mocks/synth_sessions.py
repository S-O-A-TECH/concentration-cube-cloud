"""합성 LAB-5 세션 생성기 — 시드 데이터·pytest 공용.

웹캠 프로토 tests/synth.py 의 프로파일 사상을 가져오되(출처 주석), 여기서는
'v1.0 이 놓치는 멍때림' (blank_hard: dispersion≈0.045 > 임계 0.035) 을 의도적으로
심는다 — SPEC-06 의 대표 개선 스토리("임계 이동의 직접 근거")를 로컬에서 재현하는 장치.

record.parquet 은 웹캠 record v4.2 전체 컬럼으로 쓴다 (mock 이 focus_scoring.score 로
SFI 를 계산할 수 있도록). 결정적 — sid 별 고정 시드.
"""
from __future__ import annotations

import datetime as dt
import json
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

SAMPLE_RATE_HZ = 10

# 웹캠 focus_scoring/records.py COLUMNS 의 사본 (v4.2 — 순서 고정)
COLUMNS = [
    ("sample_index", "int64"), ("t_ms", "int64"),
    ("face_valid", "bool"), ("both_eyes_valid", "bool"), ("gaze_valid", "bool"),
    ("pupil_valid", "bool"), ("glint_valid", "bool"),
    ("gaze_x", "float64"), ("gaze_y", "float64"), ("gaze_on_page_prob", "float64"),
    ("ear_mean", "float64"), ("eye_openness", "float64"),
    ("blink_count", "int64"), ("long_blink_flag", "bool"),
    ("pupil_diameter_mm", "float64"), ("glint_count", "int64"),
    ("head_yaw_deg", "float64"), ("head_pitch_deg", "float64"), ("head_roll_deg", "float64"),
    ("distance_cm", "float64"),
    ("frame_brightness", "float64"), ("valid_frame_ratio", "float64"),
    ("saccade_count_1s", "float64"), ("mean_fixation_ms", "float64"),
    ("gaze_dispersion_1s", "float64"), ("line_progression_flag", "bool"),
    ("region_alternation_1s", "float64"),
]

# (label, prob, disp, sacc, fix_ms, line) — blank_hard 가 이 시드의 주인공
PROFILES = {
    "focus":      ("focus",       0.92, 0.080, 2.5, 250.0, True),
    "off_task":   ("off_task",    0.05, 0.150, 1.0, 300.0, False),
    "blank_easy": ("blank_stare", 0.92, 0.015, 0.2, 900.0, False),
    "blank_hard": ("blank_stare", 0.92, 0.045, 0.3, 900.0, False),  # v1.0(0.035) 이 놓친다
    "invalid":    (None,          0.0,  0.0,   0.0, 0.0,   False),
}

# 5분 시나리오 3종 순환 — 데이터 다양성 (SPEC-05 §1 "여러 번 반복")
SCENARIOS = [
    [("focus", 120), ("blank_hard", 60), ("focus", 60), ("off_task", 60)],
    [("focus", 90), ("blank_easy", 30), ("blank_hard", 60), ("focus", 60), ("off_task", 60)],
    [("focus", 150), ("off_task", 60), ("blank_hard", 60), ("focus", 30)],
]


def make_session(out_dir: Path, sid: str, scenario: list[tuple[str, float]],
                 started_at: str) -> list[dict]:
    """세션 폴더(record.parquet + meta.json) 생성 → 정답지 세그먼트 반환."""
    rng = np.random.default_rng(zlib.crc32(sid.encode()))
    records: list[dict] = []
    segments: list[dict] = []
    idx = 0
    t_ms = 0
    for kind, seconds in scenario:
        label, prob, disp, sacc, fix_ms, line = PROFILES[kind]
        t0 = t_ms / 1000.0
        for _ in range(int(seconds * SAMPLE_RATE_HZ)):
            idx += 1
            t_ms += 100
            r = {name: (False if d == "bool" else (0 if d == "int64" else float("nan")))
                 for name, d in COLUMNS}
            r["sample_index"] = idx
            r["t_ms"] = t_ms
            if kind != "invalid":
                jitter = float(rng.normal(0, 0.01))
                r.update(
                    face_valid=True, both_eyes_valid=True, gaze_valid=True,
                    gaze_x=0.5 + jitter, gaze_y=0.5 + jitter,
                    gaze_on_page_prob=float(np.clip(prob + rng.normal(0, 0.03), 0, 1)),
                    ear_mean=0.28, eye_openness=0.9, blink_count=0, long_blink_flag=False,
                    head_yaw_deg=float(rng.normal(0, 2)), head_pitch_deg=float(rng.normal(-10, 2)),
                    head_roll_deg=0.0, distance_cm=float(45 + rng.normal(0, 1.5)),
                    frame_brightness=120.0, valid_frame_ratio=1.0,
                    saccade_count_1s=max(0.0, sacc + float(rng.normal(0, 0.1))),
                    mean_fixation_ms=fix_ms,
                    gaze_dispersion_1s=max(0.001, disp + float(rng.normal(0, 0.003))),
                    line_progression_flag=bool(line), region_alternation_1s=1.5 if line else 0.0,
                )
            records.append(r)
        if label:
            segments.append({"t0": round(t0, 1), "t1": round(t_ms / 1000.0, 1), "label": label})

    df = pd.DataFrame(records, columns=[c for c, _ in COLUMNS])
    for name, d in COLUMNS:
        if d == "bool":
            df[name] = df[name].fillna(False).astype(bool)
        elif d == "int64":
            df[name] = df[name].fillna(0).astype("int64")
        else:
            df[name] = df[name].astype("float64")
    d = out_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / "record.parquet", engine="pyarrow", index=False)
    duration = t_ms / 1000.0
    meta = {"sid": sid, "mode": "LAB-5", "duration_sec": duration,
            "started_at": started_at,
            "ended_at": started_at,  # 합성 — 시각 정밀도 불필요
            "schema_version": "4.2", "proto_version": "demo-synth",
            "param_set_version": "proto-0.1.0", "device_id": "WEBCAM_PROTO_001",
            "raw_video_saved": False}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    return segments


def generate_batch(out_dir: Path, n_train_min: int = 10, n_holdout_min: int = 3,
                   start_day: str = "2026-06-20") -> list[dict]:
    """train/holdout 최소 수를 만족할 때까지 결정적으로 생성 → [{sid, segments, split}]."""
    from app.loop.sim.classify_core import BIN_SEC  # noqa: F401 (경로 확인용)
    import hashlib

    def split_of(sid: str) -> str:
        return "holdout" if hashlib.sha1(sid.encode()).digest()[-1] % 4 == 3 else "train"

    day0 = dt.date.fromisoformat(start_day)
    out = []
    i = 0
    while True:
        n_train = len([s for s in out if s["split"] == "train"])
        n_hold = len([s for s in out if s["split"] == "holdout"])
        if (n_train >= n_train_min and n_hold >= n_holdout_min) or i >= 40:
            break
        day = day0 + dt.timedelta(days=i // 3)
        hh = 9 + (i % 3) * 2
        sid = f"demo_{day.strftime('%Y%m%d')}_{hh:02d}0000"
        started = f"{day.isoformat()}T{hh:02d}:00:00+09:00"
        segments = make_session(out_dir, sid, SCENARIOS[i % len(SCENARIOS)], started)
        out.append({"sid": sid, "segments": segments, "split": split_of(sid),
                    "protocol": f"기본5분 변형{i % len(SCENARIOS) + 1}"})
        i += 1
    return out
