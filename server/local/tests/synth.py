"""합성 10Hz record 생성기 — focus_scoring 유닛테스트용.

원본: web_cam_version_prototype/tests/synth.py (S3 이식 — 임포트만 app.* 로 조정).
시나리오를 (상태, 초) 목록으로 기술하면 그 상태답게 생긴 record 를 만든다.
결정적(deterministic) — 난수는 고정 시드.
"""
from __future__ import annotations

import numpy as np

from app.focus_scoring.records import SAMPLE_RATE_HZ, empty_record

_PROFILES = {
    "focus": dict(prob=0.92, disp=0.08, sacc=2.5, fix_ms=250.0, line=True,
                  ear=0.28, openness=0.9, alt=1.5),
    "off_task": dict(prob=0.05, disp=0.15, sacc=1.0, fix_ms=300.0, line=False,
                     ear=0.28, openness=0.9, alt=0.5),
    "blank_stare": dict(prob=0.92, disp=0.012, sacc=0.2, fix_ms=900.0, line=False,
                        ear=0.26, openness=0.8, alt=0.0),
    "drowsy": dict(prob=0.9, disp=0.02, sacc=0.4, fix_ms=700.0, line=False,
                   ear=0.16, openness=0.1, alt=0.0),
}


def make_records(scenario: list[tuple[str, float]], seed: int = 7) -> list[dict]:
    rng = np.random.default_rng(seed)
    records = []
    idx = 0
    t_ms = 0
    for state, seconds in scenario:
        for _ in range(int(seconds * SAMPLE_RATE_HZ)):
            idx += 1
            t_ms += 100
            r = empty_record(idx, t_ms)
            if state == "invalid":
                records.append(r)
                continue
            p = _PROFILES[state]
            jitter = rng.normal(0, 0.01)
            r.update(
                face_valid=True, both_eyes_valid=True, gaze_valid=True,
                gaze_x=0.5 + jitter, gaze_y=0.5 + jitter,
                gaze_on_page_prob=float(np.clip(p["prob"] + rng.normal(0, 0.03), 0, 1)),
                ear_mean=p["ear"], eye_openness=p["openness"],
                blink_count=0, long_blink_flag=False,
                head_yaw_deg=float(rng.normal(0, 2)),
                head_pitch_deg=float(rng.normal(-10, 2)),
                head_roll_deg=0.0,
                distance_cm=float(45 + rng.normal(0, 1.5)),
                frame_brightness=120.0, valid_frame_ratio=1.0,
                saccade_count_1s=max(0.0, p["sacc"] + float(rng.normal(0, 0.2))),
                mean_fixation_ms=p["fix_ms"],
                gaze_dispersion_1s=max(0.001, p["disp"] + float(rng.normal(0, 0.003))),
                line_progression_flag=bool(p["line"]),
                region_alternation_1s=p["alt"],
            )
            records.append(r)
    return records
