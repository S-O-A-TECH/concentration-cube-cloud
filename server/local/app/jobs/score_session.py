"""채점 잡 — parquet 로드 → 활성 param_set 으로 score() → scoring_runs + promoted_results.

원칙 (SPEC-03 §3.1):
- 활성 param_set 은 **매 실행마다 DB 조회** (캐싱 금지) — promote 즉시 새 기준 반영.
- 실패 시 builtin 폴백 금지 — 해당 잡만 failed 로 남긴다 (조용한 잘못된 채점 방지).
- scoring_runs 는 INSERT only — 재채점은 언제나 새 row.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app import focus_scoring
from app.config import get_settings
from app.db.models import PromotedResult, ScoringRun, StudySession
from app.db.session import make_session_factory
from app.focus_scoring import records as rec
from app.services.param_sets import get_active_param_set
from app.services.storage import SessionStorage


def _score_into_run(db: Session, sess: StudySession, param_set_id: int,
                    json_params: dict, storage: SessionStorage) -> ScoringRun:
    """한 세션을 주어진 param_set 으로 채점해 scoring_runs 에 INSERT (공용 — evaluate/rescore 도 사용)."""
    df = rec.read_parquet(storage.record_path(str(sess.id)))
    result = focus_scoring.score(df, json_params)
    run = ScoringRun(
        session_id=sess.id,
        param_set_id=param_set_id,
        sfi=result["sfi"],
        confidence=result["confidence"],
        components_json=result["components"],
        timeline_json=result["timeline"],
        events_json=result["events"],
        quality_json=result["quality"],
        result_json=result,
    )
    db.add(run)
    db.flush()
    return run


def _maybe_enqueue_llm(run_id: int) -> None:
    """LLM 리포트는 선택 강화 — 큐잉 실패가 채점 성공을 되돌리면 안 된다 (best-effort)."""
    settings = get_settings()
    if not (settings.llm_report_enabled and settings.qwen_api_key):
        return
    try:
        from app.services import queue as queue_service
        queue_service.enqueue_llm_report(run_id)
    except Exception as e:
        print(f"[warn] llm report enqueue failed (scoring unaffected): {e}")


def score_session(sid: str, _factory=None, _storage: SessionStorage | None = None) -> dict:
    """finish 후 자동 채점 — 활성 param_set 사용, promoted_results 갱신.

    _factory/_storage 는 테스트 주입용 — RQ 는 sid 만 넘긴다.
    """
    factory = _factory or make_session_factory()
    storage = _storage or SessionStorage(get_settings().storage_root)
    with factory() as db:
        sess = db.get(StudySession, uuid.UUID(sid))
        if sess is None:
            raise RuntimeError(f"unknown session {sid}")
        if sess.scoring_state == "done" and db.get(PromotedResult, sess.id) is not None:
            # 중복 큐잉(finish 재시도 경합 등) — 이미 채점 완료면 조기 반환 (R1)
            # S8 다중 워커 확장 시: PromotedResult upsert(ON CONFLICT) 로 강화할 것.
            return {"sid": sid, "skipped": "already scored"}
        sess.scoring_state = "running"
        db.commit()
        try:
            ps = get_active_param_set(db)
            if ps is None:
                raise RuntimeError("no adopted param_set")
            run = _score_into_run(db, sess, ps.id, ps.json_params, storage)
            promoted = db.get(PromotedResult, sess.id)
            if promoted is None:
                db.add(PromotedResult(session_id=sess.id, scoring_run_id=run.id))
            else:
                promoted.scoring_run_id = run.id
            # 세션 coverage 를 채점 결과로 채운다 (qc 계산값)
            coverage = (run.quality_json or {}).get("coverage")
            if coverage is not None:
                sess.coverage = coverage
            sess.scoring_state = "done"
            sess.scoring_error = None
            db.commit()
            _maybe_enqueue_llm(run.id)
            return {"sid": sid, "run_id": run.id, "sfi": run.sfi}
        except Exception as e:
            db.rollback()
            sess.scoring_state = "failed"
            sess.scoring_error = f"{type(e).__name__}: {e}"[:2000]
            db.commit()
            raise   # RQ Retry 가 재시도 — 최종 실패 시 failed 로 남는다
