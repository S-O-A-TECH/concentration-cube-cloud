"""재채점 잡 — evaluate 성적표 산출 / promote·rollback 전체 재채점 (S5 §1.6~1.8, SPEC-03 §4).

원칙:
- scoring_runs 는 INSERT only. 같은 (session, param_set) 조합의 run 이 이미 있으면 재사용(멱등).
- evaluate 의 run 은 성적 계산용 — promoted_results 는 건드리지 않는다.
- promoted_results 는 promote/rollback 경로에서만 대상 param_set 의 run 으로 갱신.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (AuditLog, EvolutionJob, Label, ParamSet,
                           PromotedResult, ScoringRun, StudySession)
from app.db.session import make_session_factory
from app.jobs.score_session import _score_into_run
from app.services.evaluation import merge_counts, metrics_from_counts, session_counts
from app.services.gate import evaluate_gate
from app.services.storage import SessionStorage

MIN_LABELED_SESSIONS = 10   # 게이트 안정성 하한 (S5 리스크 표)


def _get_or_create_run(db: Session, sess: StudySession, ps: ParamSet,
                       storage: SessionStorage) -> ScoringRun:
    run = db.execute(
        select(ScoringRun)
        .where(ScoringRun.session_id == sess.id, ScoringRun.param_set_id == ps.id)
        .order_by(ScoringRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if run is not None:
        return run
    return _score_into_run(db, sess, ps.id, ps.json_params, storage)


def _labeled_sessions(db: Session, include_retrospective: bool) -> list[tuple[StudySession, Label]]:
    methods = ["realtime_instructed"] + (["retrospective"] if include_retrospective else [])
    rows = db.execute(
        select(StudySession, Label)
        .join(Label, Label.session_id == StudySession.id)
        .where(StudySession.upload_state == "complete",
               StudySession.excluded.is_(False),
               Label.method.in_(methods))
    ).all()
    return [(s, l) for s, l in rows]


def _job(db: Session, job_id: int) -> EvolutionJob:
    job = db.get(EvolutionJob, job_id)
    if job is None:
        raise RuntimeError(f"unknown evolution job {job_id}")
    return job


def _flat(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _infer_targets(baseline_params: dict, cand_params: dict) -> list[str]:
    """변경된 파라미터 키 → 표적 상태 (live-evolution 계약과 동일 매핑)."""
    base, cand = _flat(baseline_params), _flat(cand_params)
    changed = sorted(k for k in set(base) | set(cand) if base.get(k) != cand.get(k))
    targets: list[str] = []
    for k in changed:
        if k.startswith("blank_stare.") and "blank_stare" not in targets:
            targets.append("blank_stare")
        if k.startswith("gaze.") and "off_task" not in targets:
            targets.append("off_task")
    return targets


def evaluate_paramset(param_set_id: int, job_id: int,
                      include_retrospective: bool = False,
                      _factory=None, _storage=None) -> dict:
    """후보 param_set 성적표: train/holdout sens·spec (baseline 대비) + 게이트 판정."""
    factory = _factory or make_session_factory()
    storage = _storage or SessionStorage(get_settings().storage_root)
    with factory() as db:
        job = _job(db, job_id)
        job.state = "running"
        db.commit()
        try:
            cand = db.get(ParamSet, param_set_id)
            if cand is None:
                raise RuntimeError(f"unknown param_set {param_set_id}")
            baseline = db.execute(
                select(ParamSet).where(ParamSet.status == "adopted")
                .order_by(ParamSet.adopted_at.desc()).limit(1)
            ).scalar_one_or_none()
            if baseline is None:
                raise RuntimeError("no adopted baseline")

            pairs = _labeled_sessions(db, include_retrospective)
            if len(pairs) < MIN_LABELED_SESSIONS:
                raise RuntimeError(
                    f"labeled sessions {len(pairs)} < {MIN_LABELED_SESSIONS} — "
                    f"라벨 세션이 더 필요합니다")

            job.progress_total = len(pairs) * 2
            db.commit()

            counts: dict = {"train": {"before": [], "after": []},
                            "holdout": {"before": [], "after": []}}
            for sess, label in pairs:
                split = sess.split if sess.split in ("train", "holdout") else "train"
                for tag, ps in (("before", baseline), ("after", cand)):
                    run = _get_or_create_run(db, sess, ps, storage)
                    counts[split][tag].append(
                        session_counts(label.segments_json, run.timeline_json or []))
                    job.progress_done += 1
                db.commit()

            metrics = {split: {tag: metrics_from_counts(merge_counts(cs))
                               for tag, cs in tags.items()}
                       for split, tags in counts.items()}
            targets = _infer_targets(baseline.json_params, cand.json_params)
            gate = evaluate_gate(metrics["holdout"]["before"], metrics["holdout"]["after"],
                                 targets=targets or None)

            def _before_after(split: str) -> dict:
                return {"per_state": {
                    st: {m: {"before": metrics[split]["before"][st][m],
                             "after": metrics[split]["after"][st][m]}
                         for m in ("sens", "spec")}
                    for st in metrics[split]["after"]}}

            report = {
                "param_set_id": cand.id,
                "param_set_version": cand.version,
                "baseline_id": baseline.id,
                "baseline_version": baseline.version,
                "n_sessions": len(pairs),
                "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "train": _before_after("train"),      # live-evolution 계약: 양쪽 다 before/after
                "holdout": _before_after("holdout"),
                "gate": gate,
            }
            cand.report_json = report
            cand.status = "passed" if gate["passed"] else "rejected"
            db.add(AuditLog(actor="evolution-job",
                            action=("param_set_passed" if gate["passed"]
                                    else "param_set_rejected"),
                            target=cand.version, detail_json={"gate": gate}))
            job.state = "done"
            job.detail_json = {"gate_passed": gate["passed"]}
            db.commit()
            return report
        except Exception as e:
            db.rollback()
            job = _job(db, job_id)
            job.state = "failed"
            job.detail_json = {"error": f"{type(e).__name__}: {e}"[:500]}
            cand = db.get(ParamSet, param_set_id)
            if cand is not None and cand.status == "evaluating":
                cand.status = "candidate"      # 재시도 가능하게 복귀
            db.commit()
            raise


def rescore_all(param_set_id: int, job_id: int, _factory=None, _storage=None) -> dict:
    """promote/rollback 후 전체 세션 재채점 + promoted_results 갱신 (멱등)."""
    factory = _factory or make_session_factory()
    storage = _storage or SessionStorage(get_settings().storage_root)
    with factory() as db:
        job = _job(db, job_id)
        job.state = "running"
        db.commit()
        try:
            ps = db.get(ParamSet, param_set_id)
            if ps is None or ps.status != "adopted":
                raise RuntimeError(f"param_set {param_set_id} is not adopted")
            sessions = db.execute(
                select(StudySession).where(StudySession.upload_state == "complete")
            ).scalars().all()
            job.progress_total = len(sessions)
            db.commit()

            updated = 0
            for sess in sessions:
                try:
                    run = _get_or_create_run(db, sess, ps, storage)
                except FileNotFoundError:
                    job.progress_done += 1
                    continue                    # parquet 소실 세션은 건너뜀 (로그성)
                promoted = db.get(PromotedResult, sess.id)
                if promoted is None:
                    db.add(PromotedResult(session_id=sess.id, scoring_run_id=run.id))
                else:
                    promoted.scoring_run_id = run.id
                coverage = (run.quality_json or {}).get("coverage")
                if coverage is not None:
                    sess.coverage = coverage
                if sess.scoring_state != "done":
                    sess.scoring_state = "done"
                updated += 1
                job.progress_done += 1
                if updated % 20 == 0:
                    db.commit()
            job.state = "done"
            job.detail_json = {"rescored": updated}
            db.commit()
            return {"rescored": updated}
        except Exception as e:
            db.rollback()
            job = _job(db, job_id)
            job.state = "failed"
            job.detail_json = {"error": f"{type(e).__name__}: {e}"[:500]}
            db.commit()
            raise
