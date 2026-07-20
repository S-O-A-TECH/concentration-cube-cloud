"""live-evolution 서버 전용 API — /v1/evolution/* (SPEC-02 §1.3, S5).

인증: X-Evolution-Token 서비스 토큰 단일. 운영 쿠키와 교차 사용 불가.
소비자는 live-evolution 서버 하나 (+웹캠 프로토의 라벨 push).
"""
from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone

import pydantic
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_storage
from app.config import get_settings
from app.db.models import (AuditLog, Device, EvolutionJob, Label, ParamSet,
                           PromotedResult, ScoringRun, StudySession)
from app.focus_scoring import records as rec
from app.services import queue as queue_service
from app.services.evaluation import (STATES, merge_counts, metrics_from_counts,
                                     session_counts, session_mistakes)
from app.services.gate import GATE_RULE
from app.services.param_schema import validate_params
from app.services.param_sets import get_active_param_set
from app.services.storage import SessionStorage

MISTAKES_CAP = 300

# live-evolution Evidence Pack 이 소비하는 수치 컬럼 (그쪽 classify_core.CLASSIFIER_COLUMNS 와 동일)
CLASSIFIER_COLUMNS = ("t_ms", "face_valid", "gaze_valid", "gaze_on_page_prob",
                      "gaze_dispersion_1s", "saccade_count_1s")


def require_evolution_token(x_evolution_token: str = Header(default="")) -> None:
    expected = get_settings().evolution_token
    if not x_evolution_token or not hmac.compare_digest(x_evolution_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid evolution token")


router = APIRouter(prefix="/v1/evolution", tags=["evolution"],
                   dependencies=[Depends(require_evolution_token)])


def _audit(db: Session, action: str, target: str, detail: dict | None = None) -> None:
    db.add(AuditLog(actor="evolution", action=action, target=target,
                    detail_json=detail))


def _display(dt_val) -> str | None:
    """사람용 세션 표기 표준 — YYYY-MM-DD HH:MM (SPEC-01 §3.1)."""
    return dt_val.strftime("%Y-%m-%d %H:%M") if dt_val else None


def _get_session(db: Session, sid: uuid.UUID) -> StudySession:
    sess = db.get(StudySession, sid)
    if sess is None:
        raise HTTPException(404, "unknown session")
    return sess


# ------------------------------------------------------------------ overview

@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict:
    """live-evolution 이 소비하는 필드(mock 계약)를 1급으로, 운영 상세는 부가로."""
    total = db.scalar(select(sa_func.count()).select_from(StudySession))
    complete = db.scalar(select(sa_func.count()).select_from(StudySession)
                         .where(StudySession.upload_state == "complete"))
    labeled = db.scalar(select(sa_func.count()).select_from(Label))
    excluded = db.scalar(select(sa_func.count()).select_from(StudySession)
                         .where(StudySession.excluded.is_(True)))
    # 연구 정본 = realtime 라벨 & 비제외 (live-evolution SPEC-05 §4)
    realtime_rows = db.execute(
        select(StudySession.split)
        .join(Label, Label.session_id == StudySession.id)
        .where(Label.method == "realtime_instructed",
               StudySession.excluded.is_(False))).scalars().all()
    retro = db.scalar(select(sa_func.count()).select_from(Label)
                      .where(Label.method == "retrospective"))
    split_counts = dict(db.execute(
        select(StudySession.split, sa_func.count())
        .where(StudySession.upload_state == "complete")
        .group_by(StudySession.split)).all())
    ps_counts = dict(db.execute(
        select(ParamSet.status, sa_func.count()).group_by(ParamSet.status)).all())
    active = get_active_param_set(db)
    jobs_running = db.scalar(select(sa_func.count()).select_from(EvolutionJob)
                             .where(EvolutionJob.state.in_(["queued", "running"])))
    last_session_at = db.scalar(select(sa_func.max(StudySession.started_at)))
    last_label_at = db.scalar(select(sa_func.max(Label.created_at)))
    return {
        # ---- live-evolution 소비 필드 (mock ops_server 와 동일 이름) ----
        "sessions_total": total,
        "labeled_realtime": len(realtime_rows),
        "labeled_retrospective": retro,
        "train_labeled": sum(1 for s in realtime_rows if s == "train"),
        "holdout_labeled": sum(1 for s in realtime_rows if s == "holdout"),
        "active_param_set": ({"id": active.id, "version": active.version,
                              "engine_version": active.engine_version}
                             if active else None),
        "gate_rule": GATE_RULE,
        "last_session_at": str(last_session_at) if last_session_at else None,
        "last_label_at": str(last_label_at) if last_label_at else None,
        # ---- 운영 상세 (서버 확장) ----
        "sessions": {"total": total, "complete": complete, "labeled": labeled,
                     "excluded": excluded,
                     "split": {"train": split_counts.get("train", 0),
                               "holdout": split_counts.get("holdout", 0)}},
        "param_sets": ps_counts,
        "jobs_running": jobs_running,
    }


# ------------------------------------------------------------------ sessions

def _label_summary(label: Label | None) -> dict | None:
    if label is None:
        return None
    segs = label.segments_json or []
    return {"method": label.method, "protocol": label.protocol,
            "labeler": label.labeler,
            "label_sec": round(sum(s["t1"] - s["t0"] for s in segs), 1)}


def _session_item(sess: StudySession, serial: str, label: Label | None,
                  sfi, runs_count: int) -> dict:
    """세션 목록/상세(view) 공용 항목 — live-evolution UI 소비 필드 포함."""
    return {
        "sid": str(sess.id),
        "started_at": (sess.started_at.isoformat(timespec="seconds")
                       if sess.started_at else None),
        "display": _display(sess.started_at),
        "mode": sess.mode,
        "device_id": serial,                    # mock 계약 이름
        "device_serial": serial,
        "duration_sec": (sess.expected_samples or 0) / 10,
        "split": sess.split,
        "labeled": label is not None,
        "label": _label_summary(label),
        "excluded": sess.excluded,
        "excluded_reason": sess.excluded_reason,
        "count_match": sess.count_match,
        "coverage": sess.coverage,
        "research_mode": sess.research_mode,
        "scoring_state": sess.scoring_state,
        "sfi": sfi,
        "runs_count": runs_count,
    }


@router.get("/sessions")
def list_sessions(labeled: bool | None = None, split: str | None = None,
                  mode: str | None = None, include_excluded: bool = False,
                  db: Session = Depends(get_db)) -> dict:
    """labeled=true 는 연구 정본(realtime 라벨 & 비제외)만 — mock 계약과 동일 시맨틱."""
    q = (select(StudySession, Device.serial, Label, ScoringRun.sfi)
         .join(Device, Device.id == StudySession.device_id)
         .outerjoin(Label, Label.session_id == StudySession.id)
         .outerjoin(PromotedResult, PromotedResult.session_id == StudySession.id)
         .outerjoin(ScoringRun, ScoringRun.id == PromotedResult.scoring_run_id)
         .where(StudySession.upload_state == "complete")
         .order_by(StudySession.started_at.desc()))
    if split in ("train", "holdout", "na"):
        q = q.where(StudySession.split == split)
    if mode:
        q = q.where(StudySession.mode == mode)
    rows = db.execute(q).all()
    runs_counts = dict(db.execute(
        select(ScoringRun.session_id, sa_func.count())
        .group_by(ScoringRun.session_id)).all())
    out = []
    for sess, serial, label, sfi in rows:
        if labeled is True and (label is None
                                or label.method != "realtime_instructed"
                                or sess.excluded):
            continue
        if labeled is False and label is not None:
            continue
        if not include_excluded and sess.excluded:
            continue
        out.append(_session_item(sess, serial, label, sfi,
                                 runs_counts.get(sess.id, 0)))
    return {"sessions": out}


def _label_raw(label: Label | None) -> dict | None:
    if label is None:
        return None
    return {"labeler": label.labeler, "method": label.method,
            "protocol": label.protocol, "segments": label.segments_json,
            "created_at": str(label.created_at)}


@router.get("/sessions/{sid}/detail")
def session_detail(sid: uuid.UUID, db: Session = Depends(get_db),
                   storage: SessionStorage = Depends(get_storage)) -> dict:
    sess = _get_session(db, sid)
    serial = db.get(Device, sess.device_id).serial
    promoted = db.get(PromotedResult, sid)
    result = None
    if promoted:
        run = db.get(ScoringRun, promoted.scoring_run_id)
        result = run.result_json if run else None
    label = db.execute(select(Label).where(Label.session_id == sid)).scalar_one_or_none()
    runs_count = db.scalar(select(sa_func.count()).select_from(ScoringRun)
                           .where(ScoringRun.session_id == sid))
    promoted_sfi = (result or {}).get("sfi")
    meta = storage.read_meta(str(sid))
    meta.setdefault("duration_sec", (sess.expected_samples or 0) / 10)
    # Evidence Pack 용 수치 배열 (개인정보 0 — SPEC-04 §3)
    classifier_inputs = None
    if storage.has_record(str(sid)):
        df = rec.read_parquet(storage.record_path(str(sid)))
        classifier_inputs = {c: df[c].tolist() for c in CLASSIFIER_COLUMNS}
    return {
        "sid": str(sid),
        "display": _display(sess.started_at),
        "view": _session_item(sess, serial, label, promoted_sfi, runs_count),
        "session": {"mode": sess.mode, "split": sess.split,
                    "excluded": sess.excluded, "excluded_reason": sess.excluded_reason,
                    "count_match": sess.count_match, "coverage": sess.coverage,
                    "research_mode": sess.research_mode,
                    "scoring_state": sess.scoring_state},
        "meta": meta,
        "result": result,          # 승격 result 전문 — param_set_version·timeline 포함
        "label": _label_raw(label),
        "labels": _label_raw(label),   # mock 계약 이름 (archive 화면 소비)
        "classifier_inputs": classifier_inputs,
        "runs_count": runs_count,
    }


# ------------------------------------------------------------------ labels

class LabelsIn(BaseModel):
    labeler: str = "admin"
    method: str = Field(default="realtime_instructed",
                        pattern="^(realtime_instructed|retrospective)$")
    protocol: str | None = None
    segments: list[dict] = Field(min_length=1)


@router.get("/sessions/{sid}/labels")
def get_labels(sid: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """라벨 원형 반환 — 없으면 {"segments": []} (mock 계약과 동일)."""
    _get_session(db, sid)
    label = db.execute(select(Label).where(Label.session_id == sid)).scalar_one_or_none()
    return _label_raw(label) or {"segments": []}


@router.post("/sessions/{sid}/labels")
def post_labels(sid: uuid.UUID, body: LabelsIn, db: Session = Depends(get_db)) -> dict:
    """★불변 — 세션당 1회, 재-POST 409 (사후 수정 금지: live-evolution SPEC-05)."""
    sess = _get_session(db, sid)
    if sess.upload_state != "complete":
        raise HTTPException(409, "session upload not complete")
    if db.execute(select(Label).where(Label.session_id == sid)).scalar_one_or_none():
        raise HTTPException(409, "label already exists (immutable — no post-hoc edits)")
    allowed = set(STATES)
    for seg in body.segments:
        if seg.get("label") not in allowed:
            raise HTTPException(422, f"label must be one of {sorted(allowed)}")
        if not (isinstance(seg.get("t0"), (int, float))
                and isinstance(seg.get("t1"), (int, float)) and seg["t0"] < seg["t1"]):
            raise HTTPException(422, "each segment needs numeric t0 < t1")
    label = Label(session_id=sid, labeler=body.labeler, method=body.method,
                  protocol=body.protocol, segments_json=body.segments)
    db.add(label)
    _audit(db, "label_created", str(sid),
           {"method": body.method, "n_segments": len(body.segments)})
    try:
        db.commit()
    except IntegrityError:
        # 동시 POST 경합 — DB UNIQUE(session_id) 가 불변성을 지켰으니 409 로 번역
        db.rollback()
        raise HTTPException(409, "label already exists (immutable — no post-hoc edits)")
    return {"ok": True, "label_id": label.id}


class ReasonIn(BaseModel):
    reason: str = Field(min_length=2)


@router.post("/sessions/{sid}/exclude")
def exclude_session(sid: uuid.UUID, body: ReasonIn, db: Session = Depends(get_db)) -> dict:
    """연구 제외 — 삭제 아님, 아카이브 보존 (SPEC-01 §3.1). evaluate·mistakes 에서 배제."""
    sess = _get_session(db, sid)
    sess.excluded = True
    sess.excluded_reason = body.reason
    _audit(db, "session_excluded", str(sid), {"reason": body.reason})
    db.commit()
    return {"ok": True, "excluded": True}


@router.post("/sessions/{sid}/restore")
def restore_session(sid: uuid.UUID, body: ReasonIn, db: Session = Depends(get_db)) -> dict:
    sess = _get_session(db, sid)
    sess.excluded = False
    sess.excluded_reason = None
    _audit(db, "session_restored", str(sid), {"reason": body.reason})
    db.commit()
    return {"ok": True, "excluded": False}


# ------------------------------------------------------------------ runs (아카이브 재활용)

@router.get("/sessions/{sid}/runs")
def session_runs(sid: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """세대별 재채점 이력 — 라벨이 있으면 세대별 sens/spec(per_state)도 함께 (아카이브 화면 소비)."""
    _get_session(db, sid)
    promoted = db.get(PromotedResult, sid)
    label = db.execute(select(Label).where(Label.session_id == sid)).scalar_one_or_none()
    rows = db.execute(
        select(ScoringRun, ParamSet.version, ParamSet.engine_version)
        .join(ParamSet, ParamSet.id == ScoringRun.param_set_id)
        .where(ScoringRun.session_id == sid)
        .order_by(ScoringRun.run_at)
    ).all()
    out = []
    for run, version, engine_version in rows:
        per_state = None
        if label is not None and run.timeline_json:
            m = metrics_from_counts(session_counts(label.segments_json,
                                                   run.timeline_json))
            per_state = {st: {"sens": v["sens"], "spec": v["spec"]}
                         for st, v in m.items()}
        out.append({
            "run_id": run.id,
            "param_set_id": run.param_set_id,
            "param_set_version": version,
            "engine_version": engine_version,
            "sfi": run.sfi,
            "confidence": run.confidence,
            "per_state": per_state,
            "run_at": str(run.run_at),
            "promoted": bool(promoted and promoted.scoring_run_id == run.id),
        })
    return {"runs": out}


# ------------------------------------------------------------------ mistakes (오답노트)

@router.get("/mistakes")
def mistakes(param_set_id: int | None = None, include_retrospective: bool = False,
             db: Session = Depends(get_db),
             storage: SessionStorage = Depends(get_storage)) -> dict:
    """라벨 vs 판정 불일치 — train 만 (holdout 누출 방지, SPEC-02 §1.3)."""
    ps = (db.get(ParamSet, param_set_id) if param_set_id
          else get_active_param_set(db))
    if ps is None:
        raise HTTPException(404, "param_set not found")
    methods = ["realtime_instructed"] + (["retrospective"] if include_retrospective else [])
    pairs = db.execute(
        select(StudySession, Label)
        .join(Label, Label.session_id == StudySession.id)
        .where(StudySession.upload_state == "complete",
               StudySession.excluded.is_(False),
               StudySession.split == "train",
               Label.method.in_(methods))
    ).all()

    all_mistakes: list[dict] = []
    confusion: dict = {}
    counts_list: list[dict] = []
    bins_total = 0
    for sess, label in pairs:
        run = db.execute(
            select(ScoringRun)
            .where(ScoringRun.session_id == sess.id,
                   ScoringRun.param_set_id == ps.id)
            .order_by(ScoringRun.id.desc()).limit(1)
        ).scalar_one_or_none()
        if run is None:
            continue                      # 이 param_set 으로 채점된 적 없음 — evaluate 후 재조회
        df = None
        if storage.has_record(str(sess.id)):
            df = rec.read_parquet(storage.record_path(str(sess.id)))
        m, c, n = session_mistakes(str(sess.id), label.segments_json,
                                   run.timeline_json or [], df)
        counts_list.append(session_counts(label.segments_json,
                                          run.timeline_json or []))
        bins_total += n
        for truth, preds in c.items():
            confusion.setdefault(truth, {})
            for pred, cnt in preds.items():
                confusion[truth][pred] = confusion[truth].get(pred, 0) + cnt
        all_mistakes.extend(m)
        if len(all_mistakes) >= MISTAKES_CAP:
            all_mistakes = all_mistakes[:MISTAKES_CAP]
            break
    per_state = ({st: {"sens": v["sens"], "spec": v["spec"], **{k: v[k] for k in ("tp", "fn", "fp", "tn")}}
                  for st, v in metrics_from_counts(merge_counts(counts_list)).items()}
                 if counts_list else {})
    return {"param_set_id": ps.id, "param_set_version": ps.version,
            "bins_total": bins_total, "mistakes": all_mistakes,
            "confusion": confusion, "per_state": per_state}


# ------------------------------------------------------------------ param_sets

@router.get("/param_sets")
def list_param_sets(db: Session = Depends(get_db)) -> dict:
    active = get_active_param_set(db)
    rows = db.execute(select(ParamSet).order_by(ParamSet.id)).scalars().all()
    return {"param_sets": [{
        "id": p.id, "version": p.version, "engine_version": p.engine_version,
        "status": p.status, "origin": p.origin, "agent_name": p.agent_name,
        "rationale": p.rationale, "parent_id": p.parent_id,
        "json_params": p.json_params,       # 에이전트 프롬프트 조립이 소비 (Evidence Pack)
        "created_at": str(p.created_at),
        "adopted_at": str(p.adopted_at) if p.adopted_at else None,
        "decided_by": p.decided_by,
        "active": bool(active and active.id == p.id),
        "has_report": p.report_json is not None,
        "gate_passed": (p.report_json or {}).get("gate", {}).get("passed"),
    } for p in rows]}


class ParamSetIn(BaseModel):
    json_params: dict
    origin: str = Field(default="agent", pattern="^(seed|agent|manual)$")
    agent_name: str | None = None
    rationale: str = Field(min_length=2)
    parent_id: int | None = None


@router.post("/param_sets")
def create_param_set(body: ParamSetIn, db: Session = Depends(get_db)) -> dict:
    """후보 등록 — 스키마 검증 통과분만 (AI 는 도구 실행자: SPEC-03 §2)."""
    try:
        validated = validate_params(body.json_params)
    except pydantic.ValidationError as e:
        raise HTTPException(422, f"json_params schema violation: {e.errors()[:3]}")
    if body.parent_id is not None and db.get(ParamSet, body.parent_id) is None:
        raise HTTPException(404, "parent param_set not found")
    ps = ParamSet(version=f"pending-{uuid.uuid4().hex[:8]}", json_params=validated,
                  origin=body.origin, agent_name=body.agent_name,
                  rationale=body.rationale, parent_id=body.parent_id,
                  status="candidate")
    db.add(ps)
    db.flush()
    ps.version = f"v1.{ps.id}"
    ps.json_params = {**validated, "param_set_version": ps.version}
    _audit(db, "param_set_created", ps.version,
           {"origin": body.origin, "agent": body.agent_name, "parent": body.parent_id})
    db.commit()
    return {"id": ps.id, "version": ps.version, "status": ps.status}


class EvaluateIn(BaseModel):
    include_retrospective: bool = False


@router.post("/param_sets/{param_set_id}/evaluate")
def evaluate_param_set(param_set_id: int, body: EvaluateIn = EvaluateIn(),
                       db: Session = Depends(get_db)) -> dict:
    ps = db.get(ParamSet, param_set_id)
    if ps is None:
        raise HTTPException(404, "param_set not found")
    if ps.status != "candidate":
        raise HTTPException(409, f"evaluate requires status=candidate (now {ps.status})")
    job = EvolutionJob(kind="evaluate", param_set_id=ps.id)
    ps.status = "evaluating"
    db.add(job)
    _audit(db, "evaluate_started", ps.version, None)
    db.commit()
    try:
        queue_service.enqueue_evaluate(ps.id, job.id, body.include_retrospective)
    except Exception as e:
        # 큐잉 실패 — 상태를 되돌려 stuck 방지 (재-evaluate 가능)
        ps.status = "candidate"
        job.state = "failed"
        job.detail_json = {"error": f"enqueue: {e}"[:300]}
        db.commit()
        raise HTTPException(503, "job enqueue failed — 잠시 후 evaluate 재시도")
    return {"ok": True, "job_id": job.id, "status": "queued"}


@router.get("/param_sets/{param_set_id}/report")
def param_set_report(param_set_id: int, db: Session = Depends(get_db)) -> dict:
    ps = db.get(ParamSet, param_set_id)
    if ps is None:
        raise HTTPException(404, "param_set not found")
    if ps.report_json is None:
        raise HTTPException(404, "no report yet — run evaluate first")
    return ps.report_json


class PromoteIn(BaseModel):
    confirm: bool = False
    decided_by: str = "evolution-console"


def _reject_if_rescore_busy(db: Session) -> None:
    """promote/rollback 동시 실행 방지 — 진행 중 재채점 잡이 있으면 409 (경합 시 세대 혼재 방지)."""
    busy = db.scalar(select(sa_func.count()).select_from(EvolutionJob).where(
        EvolutionJob.kind.in_(["promote_rescore", "rollback_rescore"]),
        EvolutionJob.state.in_(["queued", "running"])))
    if busy:
        raise HTTPException(409, "이전 승격/롤백 재채점이 진행 중 — 완료 후 재시도")


@router.post("/param_sets/{param_set_id}/promote")
def promote_param_set(param_set_id: int, body: PromoteIn,
                      db: Session = Depends(get_db)) -> dict:
    """★ 채택 — 게이트 통과(passed) + 사람의 confirm 만 (SPEC-03 §2)."""
    ps = db.execute(select(ParamSet).where(ParamSet.id == param_set_id)
                    .with_for_update()).scalar_one_or_none()
    if ps is None:
        raise HTTPException(404, "param_set not found")
    if ps.status != "passed":
        raise HTTPException(409, f"promote requires status=passed (now {ps.status}) — "
                                 f"a human click cannot override a failed gate")
    if not body.confirm:
        raise HTTPException(422, "confirm:true required — adoption is an explicit human decision")
    _reject_if_rescore_busy(db)
    ps.status = "adopted"
    ps.adopted_at = datetime.now(timezone.utc)
    ps.decided_by = body.decided_by
    job = EvolutionJob(kind="promote_rescore", param_set_id=ps.id)
    db.add(job)
    _audit(db, "param_set_promoted", ps.version,
           {"decided_by": body.decided_by, "report": ps.report_json})
    db.commit()                                   # 커밋 순간부터 신규 세션은 새 기준
    try:
        queue_service.enqueue_rescore_all(ps.id, job.id)
    except Exception as e:
        # 큐잉 실패 — 채택을 되돌려 반쪽 상태(신규만 새 기준, 과거 미재채점) 방지
        ps.status = "passed"
        ps.adopted_at = None
        ps.decided_by = None
        job.state = "failed"
        job.detail_json = {"error": f"enqueue: {e}"[:300]}
        _audit(db, "param_set_promote_reverted", ps.version,
               {"reason": "rescore enqueue failed"})
        db.commit()
        raise HTTPException(503, "재채점 큐잉 실패 — 채택이 되돌려졌습니다. 재시도하세요")
    return {"ok": True, "id": ps.id, "version": ps.version,
            "active_version": ps.version,
            "rescore_job": job.id, "rescore_job_id": job.id}


class RejectIn(BaseModel):
    reason: str | None = None


@router.post("/param_sets/{param_set_id}/reject")
def reject_param_set(param_set_id: int, body: RejectIn = RejectIn(),
                     db: Session = Depends(get_db)) -> dict:
    ps = db.get(ParamSet, param_set_id)
    if ps is None:
        raise HTTPException(404, "param_set not found")
    if ps.status not in ("candidate", "evaluating", "passed"):
        raise HTTPException(409, f"cannot reject from status {ps.status}")
    ps.status = "rejected"
    _audit(db, "param_set_rejected", ps.version, {"reason": body.reason})
    db.commit()
    return {"ok": True, "status": "rejected"}


class RollbackIn(BaseModel):
    reason: str | None = None


@router.post("/param_sets/{param_set_id}/rollback")
def rollback_param_set(param_set_id: int, body: RollbackIn = RollbackIn(),
                       db: Session = Depends(get_db)) -> dict:
    """활성 세대 롤백 — 직전 adopted 가 다시 활성 + 전체 재채점 (SPEC-01 §2)."""
    ps = db.execute(select(ParamSet).where(ParamSet.id == param_set_id)
                    .with_for_update()).scalar_one_or_none()
    active = get_active_param_set(db)
    if ps is None:
        raise HTTPException(404, "param_set not found")
    if active is None or active.id != ps.id:
        raise HTTPException(409, "rollback target must be the active adopted param_set")
    _reject_if_rescore_busy(db)
    prior_adopted_at = ps.adopted_at
    ps.status = "rolled_back"
    db.flush()
    new_active = get_active_param_set(db)
    if new_active is None:
        db.rollback()
        raise HTTPException(409, "no previous adopted generation to fall back to")
    job = EvolutionJob(kind="rollback_rescore", param_set_id=new_active.id)
    db.add(job)
    _audit(db, "param_set_rolled_back", ps.version,
           {"new_active": new_active.version, "reason": body.reason})
    db.commit()
    try:
        queue_service.enqueue_rescore_all(new_active.id, job.id)
    except Exception as e:
        # 큐잉 실패 — 롤백을 되돌려 반쪽 상태 방지
        ps.status = "adopted"
        ps.adopted_at = prior_adopted_at
        job.state = "failed"
        job.detail_json = {"error": f"enqueue: {e}"[:300]}
        _audit(db, "param_set_rollback_reverted", ps.version,
               {"reason": "rescore enqueue failed"})
        db.commit()
        raise HTTPException(503, "재채점 큐잉 실패 — 롤백이 되돌려졌습니다. 재시도하세요")
    return {"ok": True, "id": new_active.id, "version": new_active.version,
            "active_version": new_active.version,
            "rescore_job": job.id, "rescore_job_id": job.id}


# ------------------------------------------------------------------ jobs

@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(EvolutionJob)
                      .order_by(EvolutionJob.id.desc()).limit(50)).scalars().all()
    return {"jobs": [{
        # live-evolution 폴링 소비 필드 (mock 계약 이름)
        "job_id": j.id,
        "status": j.state,
        "progress": (round(j.progress_done / j.progress_total * 100)
                     if j.progress_total else (100 if j.state == "done" else 0)),
        "error": (j.detail_json or {}).get("error"),
        # 서버 상세
        "id": j.id, "kind": j.kind, "param_set_id": j.param_set_id,
        "state": j.state, "progress_done": j.progress_done,
        "progress_total": j.progress_total, "detail": j.detail_json,
        "created_at": str(j.created_at), "updated_at": str(j.updated_at),
    } for j in rows]}
