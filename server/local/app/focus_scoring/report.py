"""result JSON 생성 — phase2 §2.4 result API 와 동일 계약 (SPEC §4.4)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import coach_templates, events as events_mod, rhythm
from .records import SAMPLE_RATE_HZ

ALGO_VERSION = "proto-0.1.0"

# 앱이 선택 가능한 코칭 문구 언어 (2026-07-16) — coach_text 는 이 셋 모두를 담아
# 두고, 실제 노출 언어 선택은 API 레이어(build_session_report)가 한다.
COACH_LANGS = ("ko", "en", "zh")


def build(df: pd.DataFrame, params: dict, states: np.ndarray, ev: dict,
          components: dict, sfi_value: int, confidence: str, quality: dict) -> dict:
    timeline = events_mod.build_timeline(df, states, params)
    focus_samples = int(np.sum(states == "focus"))
    focused_minutes = round(focus_samples / SAMPLE_RATE_HZ / 60.0, 1)
    streak = _max_focus_streak_min(states)
    minutes = _minutes_by_state(states)

    rets = [e["return_sec"] for e in ev["off_page"] if e["return_sec"] is not None]
    mean_return = round(float(np.mean(rets)), 1) if rets else None

    return {
        "schema": "result.v1",
        "algo_version": ALGO_VERSION,
        "param_set_version": params.get("param_set_version"),
        "confidence": confidence,
        "sfi": sfi_value,
        "components": components,
        "focused_minutes": focused_minutes,
        "max_focus_streak_min": streak,
        "mean_return_sec": mean_return,
        "minutes": minutes,
        "timeline": timeline,
        "events": ev["list"],
        "coach_text": {
            lang: {
                "student": coach_templates.student_text(
                    sfi_value, focused_minutes, streak,
                    len(ev["off_page"]), mean_return, len(ev["blank_stare"]),
                    lang=lang),
                "parent": coach_templates.parent_text(
                    sfi_value, focused_minutes, streak,
                    len(ev["off_page"]), len(ev["blank_stare"]), quality["coverage"],
                    lang=lang),
            }
            for lang in COACH_LANGS
        },
        "quality": {
            "coverage": quality["coverage"],
            "invalid_summary": quality["invalid_summary"],
            "sfi_renormalized": True,   # 7A: effort 성분 제외 재정규화 명시
            "renormalized_reason": "effort(pupil) requires NIR — measured on production device",
        },
        "rhythm_features": rhythm.session_features(df, states),
    }


def build_unscorable(df: pd.DataFrame, params: dict, quality: dict) -> dict:
    """coverage 미달 — 점수 대신 안내 (phase2 Layer 0 규칙)."""
    return {
        "schema": "result.v1",
        "algo_version": ALGO_VERSION,
        "param_set_version": params.get("param_set_version"),
        "confidence": "low",
        "sfi": None,
        "components": {},
        "focused_minutes": None,
        "max_focus_streak_min": None,
        "mean_return_sec": None,
        # 점수는 못 내도 "몇 분 사용했는지"는 사실이므로 total 만 제공
        "minutes": {"total": round(len(df) / SAMPLE_RATE_HZ / 60.0, 1),
                    "focus": None, "blank_stare": None,
                    "off_task": None, "invalid": None},
        "timeline": [],
        "events": [],
        "coach_text": {
            lang: {"student": coach_templates.unscorable_text(lang),
                   "parent": coach_templates.unscorable_text(lang)}
            for lang in COACH_LANGS
        },
        "quality": {
            "coverage": quality["coverage"],
            "invalid_summary": quality["invalid_summary"],
            "unscorable_reason": quality["reason"],
        },
        "rhythm_features": None,
    }


def _minutes_by_state(states: np.ndarray) -> dict:
    """사용 시간 분해(분) — "총 몇 분 중 집중/멍때림/이탈/측정불가 몇 분"(2026-07-07).

    total 은 세션에서 **실제로 기록된** 시간이다 — 20분 도전을 7분에 마치면 7.0.
    앱은 이 값을 파생 계산 없이 그대로 표시한다(서버 정본 원칙).
    """
    def m(name: str) -> float:
        return round(int(np.sum(states == name)) / SAMPLE_RATE_HZ / 60.0, 1)

    return {"total": round(len(states) / SAMPLE_RATE_HZ / 60.0, 1),
            "focus": m("focus"), "blank_stare": m("blank_stare"),
            "off_task": m("off_task"), "invalid": m("invalid")}


def _max_focus_streak_min(states: np.ndarray) -> float:
    """가장 길게 이어진 집중 구간(분) — 앱 캘린더의 날짜별 숫자와 동일 정의."""
    best = cur = 0
    for s in states:
        if s == "focus":
            cur += 1
            best = max(best, cur)
        elif s == "invalid":
            continue  # 짧은 측정 공백은 streak 을 끊지 않는다 (관대한 정의)
        else:
            cur = 0
    return round(best / SAMPLE_RATE_HZ / 60.0, 1)
