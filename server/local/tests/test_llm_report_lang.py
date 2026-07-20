"""_slice_llm_report — LLM 코칭 리포트의 언어 슬라이스/하위호환 (2026-07-16).

순수 함수라 DB 없이 검증한다. 계약:
- 신규 스키마({ko,en,zh}) → 요청 언어 슬라이스(+model/generated_at 메타 보존).
- 구버전 평면(한국어 전용) → ko 요청에만 반환, en/zh 는 None(템플릿 폴백 유도).
"""
from app.api.ops import _slice_llm_report

NEW_SCHEMA = {
    "ko": {"student": "ko-s", "parent": "ko-p"},
    "en": {"student": "en-s", "parent": "en-p"},
    "zh": {"student": "zh-s", "parent": "zh-p"},
    "model": "qwen-plus",
    "generated_at": "2026-07-16T00:00:00+00:00",
}

LEGACY_FLAT = {
    "student": "한국어 학생 문구", "parent": "한국어 보호자 문구",
    "model": "qwen-plus",
    "generated_at": "2026-07-10T00:00:00+00:00",
}


def test_new_schema_slices_each_lang():
    for lang in ("ko", "en", "zh"):
        out = _slice_llm_report(NEW_SCHEMA, lang)
        assert out == {
            "student": f"{lang}-s", "parent": f"{lang}-p",
            "model": "qwen-plus",
            "generated_at": "2026-07-16T00:00:00+00:00",
        }


def test_legacy_flat_serves_korean_only():
    out = _slice_llm_report(LEGACY_FLAT, "ko")
    assert out is not None and out["student"] == "한국어 학생 문구"
    assert out["model"] == "qwen-plus"
    # en/zh 요청엔 한국어를 내보내지 않는다 — 호출부가 템플릿 coach_text 로 폴백
    assert _slice_llm_report(LEGACY_FLAT, "en") is None
    assert _slice_llm_report(LEGACY_FLAT, "zh") is None


def test_none_and_malformed():
    assert _slice_llm_report(None, "ko") is None
    assert _slice_llm_report({}, "ko") is None
    assert _slice_llm_report({"ko": "문자열이면 무시"}, "ko") is None


# ── _legacy_coach_localized — 다국어 이전 result 의 리포트 시점 재합성 ──
from app.api.ops import _legacy_coach_localized  # noqa: E402

LEGACY_RESULT = {
    "sfi": 72, "focused_minutes": 12.0, "max_focus_streak_min": 8.0,
    "mean_return_sec": 9.0,
    "events": [{"type": "off_page"}, {"type": "blank_stare"}],
    "quality": {"coverage": 0.9},
    "coach_text": {"student": "한국어", "parent": "한국어"},
}


def test_legacy_result_resynthesized_in_request_lang():
    ko = _legacy_coach_localized(LEGACY_RESULT, "ko")
    assert ko is None  # 원문이 이미 한국어 — 그대로 둔다
    en = _legacy_coach_localized(LEGACY_RESULT, "en")
    assert en and "minutes" in en["student"] and "focus" in en["parent"].lower()
    zh = _legacy_coach_localized(LEGACY_RESULT, "zh")
    assert zh and "专注" in zh["student"]


def test_legacy_unscorable_resynthesized():
    out = _legacy_coach_localized({"sfi": None}, "en")
    assert out and "measurement" in out["student"]
