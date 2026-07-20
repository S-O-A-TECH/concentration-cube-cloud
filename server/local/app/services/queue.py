"""RQ 큐 — 채점·재채점 잡 큐잉 (S3 계획 §1.2, 큐 이름 scoring).

worker: compose 의 `rq worker scoring` 컨테이너가 소비한다.
테스트에서는 이 모듈의 enqueue_* 를 monkeypatch 해 동기 실행으로 대체한다.
"""
from __future__ import annotations

import redis as redis_lib
from rq import Queue, Retry

from app.config import get_settings

QUEUE_NAME = "scoring"
RETRY = Retry(max=3, interval=[10, 30, 60])   # 지수 백오프 (S3 계획 §1.5)


def get_queue() -> Queue:
    conn = redis_lib.Redis.from_url(get_settings().redis_url)
    return Queue(QUEUE_NAME, connection=conn)


def enqueue_score_session(sid: str) -> None:
    get_queue().enqueue("app.jobs.score_session.score_session", sid,
                        retry=RETRY, job_timeout=600)


def enqueue_llm_report(run_id: int) -> None:
    get_queue().enqueue("app.jobs.llm_report.generate_llm_report", run_id,
                        retry=Retry(max=2, interval=[30, 120]), job_timeout=300)


def enqueue_evaluate(param_set_id: int, job_id: int, include_retrospective: bool) -> None:
    get_queue().enqueue("app.jobs.rescore_paramset.evaluate_paramset",
                        param_set_id, job_id, include_retrospective,
                        retry=RETRY, job_timeout=3600)


def enqueue_rescore_all(param_set_id: int, job_id: int) -> None:
    # 실패 시 재시도 — 잡이 멱등(_get_or_create_run)이라 이어서 재개된다 (세대 혼재 방지)
    get_queue().enqueue("app.jobs.rescore_paramset.rescore_all",
                        param_set_id, job_id, retry=RETRY, job_timeout=7200)


def enqueue_delete_profile(profile_id: int) -> None:
    # 동의 철회 집행 — 조용한 유실 방지를 위해 재시도 필수 (완료 audit 이 진실원본)
    get_queue().enqueue("app.jobs.privacy.delete_profile_data", profile_id,
                        retry=RETRY, job_timeout=600)
