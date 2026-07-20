"""세션 수집 API — start / calibration / chunk / finish / status / result (SPEC-02 §1.1).

원칙: 무결성 실패는 오류가 아니라 데이터 — finish 는 200 + count_match:false 로 기록한다.
채점 큐잉은 S3 에서 finish 뒤에 붙는다 (여기서는 자리만).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import check_nonce, get_db, get_storage, require_device
from app.db.models import Device, PromotedResult, ScoringRun, StudySession
from app.services import queue as queue_service
from app.focus_scoring.records import COLUMN_NAMES, SCHEMA_VERSION, read_parquet
from app.services.integrity import df_to_canonical_records, missing_ranges, records_crc32
from app.services.split import split_for
from app.services.storage import SessionStorage

router = APIRouter(prefix="/v1/sessions", tags=["sessions"],
                   dependencies=[Depends(check_nonce)])

_COLUMN_SET = set(COLUMN_NAMES)


def _get_own_session(sid: uuid.UUID, device: Device, db: Session) -> StudySession:
    sess = db.get(StudySession, sid)
    if sess is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    if sess.device_id != device.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "session belongs to another device")
    return sess


# ---------- start ----------

class StartIn(BaseModel):
    firmware_version: str | None = None
    schema_version: str
    session_mode: str = Field(pattern=r"^[A-Z]+-\d+$")   # SFI-20, DEV-2 ...
    duration_sec: int = Field(gt=0, le=4 * 3600)
    sample_rate_hz: int = 10
    upload_policy: str = "AFTER_SESSION_ONLY"
    raw_video_uploaded: bool = False
    research_mode: bool = False        # 참고값 — 실제로는 devices.research_flag 로 강제
    device_id: str | None = None
    # 안경 착용 여부(2026-07-08) — 기기가 캘리브레이션에서 자동 감지. None=미상
    glasses: bool | None = None


@router.post("/start")
def start_session(body: StartIn,
                  device: Device = Depends(require_device),
                  db: Session = Depends(get_db),
                  storage: SessionStorage = Depends(get_storage)) -> dict:
    if body.schema_version != SCHEMA_VERSION:
        raise HTTPException(422, f"schema_version must be {SCHEMA_VERSION}")
    if body.sample_rate_hz != 10:
        raise HTTPException(422, "sample_rate_hz must be 10")
    if body.raw_video_uploaded:
        # raw video-free 원칙 — 영상을 올렸다는 주장 자체를 거부
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            "raw video is never accepted")

    sid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    sess = StudySession(
        id=sid,
        device_id=device.id,
        profile_id=device.default_profile_id,
        mode=body.session_mode,
        schema_version=body.schema_version,
        started_at=now,
        upload_state="open",
        expected_samples=body.duration_sec * 10,
        uploaded_samples=0,
        research_mode=device.research_flag,     # 기기 플래그로 강제 (S2 계획 §1.2)
        split=split_for(sid),
        glasses=body.glasses,
    )
    db.add(sess)
    db.commit()
    storage.update_meta(str(sid), {
        "sid": str(sid),
        "device_serial": device.serial,
        "mode": body.session_mode,
        "schema_version": body.schema_version,
        "firmware_version": body.firmware_version,
        "started_at": now.isoformat(timespec="seconds"),
        "duration_sec": body.duration_sec,
        "research_mode": device.research_flag,
        "glasses": body.glasses,
    })
    return {"session_id": str(sid), "expected_samples": sess.expected_samples}


# ---------- calibration ----------

class CalibrationIn(BaseModel):
    points: int = Field(ge=1, le=16)
    max_residual: float | None = None
    residuals: list[float] | None = None
    extra: dict | None = None


@router.post("/{sid}/calibration")
def report_calibration(sid: uuid.UUID, body: CalibrationIn,
                       device: Device = Depends(require_device),
                       db: Session = Depends(get_db),
                       storage: SessionStorage = Depends(get_storage)) -> dict:
    _get_own_session(sid, device, db)
    storage.update_meta(str(sid), {"calibration": body.model_dump(exclude_none=True)})
    return {"ok": True}


# ---------- chunk ----------

class ChunkIn(BaseModel):
    chunk_index: int = Field(ge=0)
    first_sample_index: int = Field(ge=1)
    crc32: int = Field(ge=0, le=0xFFFFFFFF)
    records: list[dict] = Field(min_length=1, max_length=1000)


@router.post("/{sid}/chunk")
def upload_chunk(sid: uuid.UUID, body: ChunkIn,
                 device: Device = Depends(require_device),
                 db: Session = Depends(get_db),
                 storage: SessionStorage = Depends(get_storage)) -> dict:
    sess = _get_own_session(sid, device, db)
    if sess.upload_state != "open":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"session is {sess.upload_state}, not open")

    # 컬럼 계약 검증 — focus_scoring COLUMNS 가 정본 (SPEC-02 §2.3)
    for i, r in enumerate(body.records):
        if set(r.keys()) != _COLUMN_SET:
            missing = _COLUMN_SET - set(r.keys())
            extra = set(r.keys()) - _COLUMN_SET
            raise HTTPException(422,
                                f"record[{i}] schema mismatch "
                                f"(missing={sorted(missing)}, extra={sorted(extra)})")
    # sample_index 연속성
    for i, r in enumerate(body.records):
        if r["sample_index"] != body.first_sample_index + i:
            raise HTTPException(422, f"record[{i}] sample_index not contiguous")
    # CRC 대조
    if records_crc32(body.records) != body.crc32:
        raise HTTPException(400, "chunk crc32 mismatch")

    manifest = storage.read_manifest(str(sid))
    known = manifest.get(str(body.chunk_index))
    if known is not None:
        if known["crc32"] == body.crc32:
            # 동일 chunk 재전송 — 멱등 무시
            return {"ok": True, "duplicate": True,
                    "uploaded_samples": sess.uploaded_samples}
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "chunk_index already uploaded with different crc32")

    manifest = storage.write_chunk(str(sid), body.chunk_index, body.records,
                                   body.crc32, body.first_sample_index)
    sess.uploaded_samples = sum(m["count"] for m in manifest.values())
    db.commit()
    return {"ok": True, "duplicate": False, "received": len(body.records),
            "uploaded_samples": sess.uploaded_samples}


# ---------- finish ----------

class FinishIn(BaseModel):
    expected_samples: int = Field(gt=0)
    session_crc: int = Field(ge=0, le=0xFFFFFFFF)


def _enqueue_scoring_or_mark_failed(db: Session, sess: StudySession) -> None:
    """상태 커밋 후 큐잉 — 실패해도 stuck 되지 않게 failed 로 기록 (finish 재시도가 재큐잉)."""
    try:
        queue_service.enqueue_score_session(str(sess.id))
        if sess.scoring_state != "queued":
            sess.scoring_state = "queued"
            db.commit()
    except Exception as e:
        sess.scoring_state = "failed"
        sess.scoring_error = f"enqueue: {type(e).__name__}: {e}"[:500]
        db.commit()


@router.post("/{sid}/finish")
def finish_session(sid: uuid.UUID, body: FinishIn,
                   device: Device = Depends(require_device),
                   db: Session = Depends(get_db),
                   storage: SessionStorage = Depends(get_storage)) -> dict:
    # 동시 finish 직렬화 (row lock — 기기 재시도·--sync 경합 대비).
    # sqlite(단위테스트)는 FOR UPDATE 를 무시하지만 단일 스레드라 안전.
    sess = db.execute(
        select(StudySession).where(StudySession.id == sid).with_for_update()
    ).scalar_one_or_none()
    if sess is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
    if sess.device_id != device.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "session belongs to another device")

    if sess.upload_state == "complete":
        # finish 재전송 멱등 — 기록된 판정 재반환.
        # 채점이 큐잉 유실로 멈춰 있으면 여기서 재큐잉 (자동 복구 경로).
        meta = storage.read_meta(str(sid)).get("finish", {})
        if (sess.scoring_state in ("none", "failed", "queued")
                and db.get(PromotedResult, sid) is None):
            _enqueue_scoring_or_mark_failed(db, sess)
        storage.cleanup_chunks(str(sid))    # 커밋-정리 사이 크래시 잔존물 청소 (멱등, R3)
        return {"ok": True, "already_complete": True,
                "count_match": sess.count_match, **meta}
    if sess.upload_state != "open":
        raise HTTPException(status.HTTP_409_CONFLICT, f"session is {sess.upload_state}")

    df = storage.merge_chunks(str(sid))
    if len(df) == 0 and storage.has_record(str(sid)):
        # 크래시 후 재시도: chunks 는 정리됐지만 확정본은 존재 — 확정본 기준 재판정
        df = read_parquet(storage.record_path(str(sid)))

    expected = sess.expected_samples or body.expected_samples
    present = df["sample_index"].tolist() if len(df) else []
    missing = missing_ranges(present, expected)
    server_crc = records_crc32(df_to_canonical_records(df)) if len(df) else 0
    crc_match = server_crc == body.session_crc
    count_match = (len(df) == expected and not missing and crc_match
                   and body.expected_samples == expected)

    now = datetime.now(timezone.utc)
    verdict = {
        "uploaded_samples": len(df),
        "expected_samples": expected,
        "missing_ranges": missing,
        "session_crc_match": crc_match,
        "count_match": count_match,
    }
    path = storage.finalize(str(sid), df, {"finish": verdict,
                                           "ended_at": now.isoformat(timespec="seconds")})
    sess.uploaded_samples = len(df)
    sess.count_match = count_match
    sess.upload_state = "complete"
    sess.ended_at = now
    sess.raw_uri = str(path)
    sess.scoring_state = "queued"
    db.commit()
    storage.cleanup_chunks(str(sid))                 # 반드시 커밋 이후 (크래시 복구 재료 보존)
    _enqueue_scoring_or_mark_failed(db, sess)        # 자동 채점 (S3)
    return {"ok": True, "already_complete": False, **verdict}


# ---------- status / result ----------

@router.get("/{sid}/status")
def session_status(sid: uuid.UUID,
                   device: Device = Depends(require_device),
                   db: Session = Depends(get_db)) -> dict:
    sess = _get_own_session(sid, device, db)
    return {"upload_state": sess.upload_state,
            "uploaded_samples": sess.uploaded_samples,
            "expected_samples": sess.expected_samples,
            "count_match": sess.count_match,
            "scoring": sess.scoring_state,
            "scoring_error": sess.scoring_error}


@router.get("/{sid}/result")
def session_result(sid: uuid.UUID,
                   device: Device = Depends(require_device),
                   db: Session = Depends(get_db)):
    """promoted_results 기준 result 반환 — 웹캠 SPEC §4.4 계약 그대로 (SPEC-02 §2.4)."""
    from fastapi.responses import JSONResponse

    sess = _get_own_session(sid, device, db)
    promoted = db.get(PromotedResult, sid)
    if promoted is None:
        if sess.scoring_state in ("queued", "running"):
            return JSONResponse(status_code=202,
                                content={"scoring": sess.scoring_state})
        if sess.scoring_state == "failed":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"scoring failed: {sess.scoring_error}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no promoted result")
    run = db.get(ScoringRun, promoted.scoring_run_id)
    result = dict(run.result_json)
    if run.llm_report_json:                          # LLM 강화 리포트 (있을 때만)
        result["llm_report"] = run.llm_report_json
    return result
