"""LLM 코칭 리포트 잡 — Qwen/Alibaba Cloud Model Studio (worker 전용, S3 계획 §1.4).

원칙 (SPEC-00 §2, phase2 확정):
- LLM 에는 **수치 요약 JSON 만** 전송 — 원시 record·개인 식별 정보 미전송.
- 채점(result_json)은 순수·결정적으로 유지 — LLM 리포트는 별도 컬럼(llm_report_json),
  실패해도 채점 결과에는 영향 없음.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.db.models import ScoringRun
from app.db.session import make_session_factory

# 앱이 선택 가능한 언어(2026-07-16) — 세 언어를 한 번의 호출로 전부 생성해 두고,
# 실제 노출 언어 선택은 build_session_report(ops.py)가 한다.
COACH_LANGS = ("ko", "en", "zh")

SYSTEM_PROMPT = (
    "당신은 학습 집중 코치입니다. 아래 수치 요약(0-100 SFI 집중 점수, 상태별 시간, 이벤트 수)만 보고 "
    "학생용/보호자용 코칭 문구를 작성하세요. 규칙: ① 진단·질환(ADHD 등) 표현 금지 — 교육용 '집중 유지 점수'로만 서술 "
    "② 따뜻하고 구체적으로, 각 3~4문장 ③ 측정 품질이 낮으면(coverage<0.7) 점수 평가 대신 환경 개선을 안내 "
    "④ 같은 내용을 한국어(ko)·영어(en)·중국어 간체(zh) 세 언어로 모두 작성 — 번역투가 아니라 각 언어에서 자연스러운 문장으로 "
    "⑤ 반드시 JSON 으로만 응답: "
    "{\"ko\": {\"student\": \"...\", \"parent\": \"...\"}, "
    "\"en\": {\"student\": \"...\", \"parent\": \"...\"}, "
    "\"zh\": {\"student\": \"...\", \"parent\": \"...\"}}"
)


def build_numeric_summary(result: dict) -> dict:
    """result 전문에서 LLM 에 보낼 수치 요약만 추출 (개인정보·원시 데이터 없음)."""
    events = result.get("events") or []
    event_counts: dict[str, int] = {}
    for e in events:
        event_counts[e.get("type", "?")] = event_counts.get(e.get("type", "?"), 0) + 1
    timeline = result.get("timeline") or []
    state_secs: dict[str, float] = {}
    for seg in timeline:
        dur = float(seg.get("t1", 0)) - float(seg.get("t0", 0))
        state_secs[seg.get("state", "?")] = state_secs.get(seg.get("state", "?"), 0.0) + dur
    return {
        "sfi": result.get("sfi"),
        "confidence": result.get("confidence"),
        "components": result.get("components"),
        "focused_minutes": result.get("focused_minutes"),
        "max_focus_streak_min": result.get("max_focus_streak_min"),
        "mean_return_sec": result.get("mean_return_sec"),
        "coverage": (result.get("quality") or {}).get("coverage"),
        "unscorable_reason": (result.get("quality") or {}).get("unscorable_reason"),
        "event_counts": event_counts,
        "state_seconds": {k: round(v, 1) for k, v in state_secs.items()},
    }


def _call_qwen(summary: dict) -> dict:
    s = get_settings()
    r = httpx.post(
        f"{s.qwen_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {s.qwen_api_key}"},
        json={
            "model": s.qwen_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": json.dumps(summary, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "max_tokens": 2400,   # 3개 언어 × (학생+보호자) — 한국어 단일이던 800 에서 증액
        },
        timeout=90.0,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or any(
        not isinstance(parsed.get(lang), dict)
        or "student" not in parsed[lang] or "parent" not in parsed[lang]
        for lang in COACH_LANGS
    ):
        raise ValueError(f"unexpected LLM response shape: {content[:200]}")
    return {
        lang: {"student": str(parsed[lang]["student"]),
               "parent": str(parsed[lang]["parent"])}
        for lang in COACH_LANGS
    }


def generate_llm_report(run_id: int, _factory=None) -> dict:
    """scoring_run 의 result 요약 → Qwen → llm_report_json 저장."""
    settings = get_settings()
    if not (settings.llm_report_enabled and settings.qwen_api_key):
        return {"skipped": "llm report disabled or no api key"}

    factory = _factory or make_session_factory()
    with factory() as db:
        run = db.get(ScoringRun, run_id)
        if run is None:
            raise RuntimeError(f"unknown scoring_run {run_id}")
        if run.llm_report_json:                      # 멱등 — 이미 생성됨
            return {"skipped": "already generated"}
        report = _call_qwen(build_numeric_summary(run.result_json))
        run.llm_report_json = {
            **report,
            "model": settings.qwen_model,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        db.commit()
        return {"run_id": run_id, "ok": True}
