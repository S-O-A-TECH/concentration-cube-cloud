"""수면세션 음성 배포 API — /v1/ops/relax_voice(운영자), /v1/app/enhance/*(앱).

운영자가 콘솔에서 단계별 대사 mp3 를 업로드하고 배포일을 정해 배포하면, 앱이 published +
배포일 도래한 최신 릴리즈를 pull 해 폰 TTS 대신 재생한다(앱 업데이트 없이 대사 교체).

수면 세션 2종:
- short(기존): 슬롯 4개 [close_eyes, pmr, breathing, closing].
- long(긴 버전 5분, 2026-07-07): 슬롯 7개 + 서버가 정한 음악 시퀀스(운영콘솔에서 편집).

원칙
- 슬롯 계약은 protocol 별로 고정 — 앱이 이 키로 재생 단계를 분기한다.
- published 릴리즈는 불변 — 업로드/재배포 불가, 교체는 새 draft 로만.
- 앱은 published + release_at ≤ now(UTC) 인 릴리즈만, protocol 별 독립으로 본다(최신이 active).
- 영상 무수신 원칙: audio/* + mp3 시그니처만 통과(미들웨어에서 video/* 는 경로 불문 거부).
- 긴 버전 시퀀스(단계 길이·음악)는 enhance_protocol_configs 가 서버 정본, 앱이 pull.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.app_api import require_app_user
from app.api.deps import get_db, get_storage
from app.api.ops import _audit, require_ops
from app.db.models import EnhanceProtocolConfig, RelaxVoiceRelease
from app.services.relax_voice import (DEFAULT_RELAX_LONG_PROTOCOL, KST,
                                      MAX_VOICE_BYTES, RELAX_LONG_PROTOCOL_KEY,
                                      RELAX_MUSIC_OPTIONS, RELAX_VOICE_PROTOCOLS,
                                      RelaxVoiceStorage, apply_long_protocol_edits,
                                      as_utc, kst_date_to_release_at, labels_for,
                                      long_protocol_rows, looks_like_mp3, slots_for)
from app.services.storage import SessionStorage

ops_router = APIRouter(prefix="/v1/ops/relax_voice", tags=["ops-relax-voice"],
                       dependencies=[Depends(require_ops)])
app_router = APIRouter(prefix="/v1/app/enhance", tags=["app-enhance"],
                       dependencies=[Depends(require_app_user)])

PROTOCOL_LABELS = {"short": "짧은 버전", "long": "긴 버전"}


def _voice_storage(storage: SessionStorage = Depends(get_storage)) -> RelaxVoiceStorage:
    # SessionStorage 와 같은 STORAGE_ROOT 를 공유 — 테스트의 get_storage 오버라이드가 흘러든다.
    return RelaxVoiceStorage(storage.root)


def _iso_utc(dt: datetime | None) -> str | None:
    dt = as_utc(dt)
    return dt.isoformat() if dt else None


def _iso_kst(dt: datetime | None) -> str | None:
    """운영자·앱이 보는 시각은 배포일 기준(KST) — DB 는 UTC 로 저장하되 표시는 KST."""
    dt = as_utc(dt)
    return dt.astimezone(KST).isoformat() if dt else None


def _is_released(r: RelaxVoiceRelease, now: datetime | None = None) -> bool:
    if r.status != "published":
        return False
    ra = as_utc(r.release_at)
    return ra is not None and ra <= (now or datetime.now(timezone.utc))


def active_release(db: Session, protocol: str = "short") -> RelaxVoiceRelease | None:
    """published & release_at ≤ now(UTC) 중 release_at 최신 (protocol 별 독립)."""
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(RelaxVoiceRelease)
        .where(RelaxVoiceRelease.status == "published",
               RelaxVoiceRelease.protocol == protocol)).scalars().all()
    released = [r for r in rows if _is_released(r, now)]
    if not released:
        return None
    return max(released, key=lambda r: as_utc(r.release_at))


def _release_view(r: RelaxVoiceRelease, active_id: int | None) -> dict:
    files = r.files or {}
    return {
        "id": r.id,
        "protocol": r.protocol,
        "status": r.status,
        "note": r.note,
        "release_at": _iso_kst(r.release_at),
        "created_at": _iso_utc(r.created_at),
        "published_at": _iso_utc(r.published_at),
        "is_active": r.id == active_id,
        "slots": {slot: (slot in files) for slot in slots_for(r.protocol)},
    }


def _get_long_config(db: Session) -> dict:
    row = db.get(EnhanceProtocolConfig, RELAX_LONG_PROTOCOL_KEY)
    return row.config_json if row else DEFAULT_RELAX_LONG_PROTOCOL


# ============================================================ 운영자 API — 음성 배포

@ops_router.get("")
@ops_router.get("/")
def list_releases(db: Session = Depends(get_db)) -> dict:
    """protocol(short/long)별 섹션 — 슬롯 목록 + 릴리즈(최신순) + active 표시."""
    rows = db.execute(select(RelaxVoiceRelease)
                      .order_by(RelaxVoiceRelease.id.desc())).scalars().all()
    protocols = []
    for proto in RELAX_VOICE_PROTOCOLS:
        active = active_release(db, proto)
        active_id = active.id if active else None
        proto_rows = [r for r in rows if r.protocol == proto]
        labels = labels_for(proto)
        protocols.append({
            "key": proto,
            "label": PROTOCOL_LABELS.get(proto, proto),
            "slots": [{"key": s, "label": labels[s]} for s in slots_for(proto)],
            "active_id": active_id,
            "releases": [_release_view(r, active_id) for r in proto_rows],
        })
    return {"protocols": protocols}


class ReleaseCreateIn(BaseModel):
    note: str = Field(default="", max_length=255)
    protocol: str = Field(default="short")


@ops_router.post("/releases")
def create_release(body: ReleaseCreateIn, db: Session = Depends(get_db)) -> dict:
    """새 draft 릴리즈 생성 — protocol(short/long) 선택. 여기서만 업로드가 가능하다."""
    if body.protocol not in RELAX_VOICE_PROTOCOLS:
        raise HTTPException(422, f"protocol must be one of {list(RELAX_VOICE_PROTOCOLS)}")
    r = RelaxVoiceRelease(status="draft", protocol=body.protocol,
                          note=body.note or None, files={})
    db.add(r)
    db.flush()
    _audit(db, "relax_voice_release_created", f"relax_voice:{r.id}",
           {"note": body.note, "protocol": body.protocol})
    db.commit()
    return _release_view(r, None)


@ops_router.post("/releases/{release_id}/upload/{slot}")
async def upload_slot(release_id: int, slot: str,
                      file: UploadFile = File(...),
                      db: Session = Depends(get_db),
                      voice: RelaxVoiceStorage = Depends(_voice_storage)) -> dict:
    """draft 릴리즈의 한 슬롯에 mp3 업로드 (draft 에만, 슬롯은 protocol 계약 내에서만).

    검증: content-type audio/* + mp3 시그니처(ID3/0xFFEx) + 크기 ≤ 15MB.
    video/* 는 미들웨어에서 경로 불문 이미 415 로 거부된다(여기 도달 불가).
    """
    r = db.get(RelaxVoiceRelease, release_id)
    if r is None:
        raise HTTPException(404, "unknown release")
    if r.status != "draft":
        raise HTTPException(409, "published 릴리즈는 수정할 수 없습니다 — 새 draft 를 만드세요")
    if slot not in slots_for(r.protocol):
        raise HTTPException(422, f"unknown slot for protocol '{r.protocol}' "
                                 f"(expected one of {list(slots_for(r.protocol))})")

    ctype = (file.content_type or "").lower()
    if not ctype.startswith("audio/"):
        raise HTTPException(400, f"audio/* 만 허용됩니다 (받은 값: {file.content_type or '없음'})")
    data = await file.read()
    if not data:
        raise HTTPException(400, "빈 파일입니다")
    if len(data) > MAX_VOICE_BYTES:
        raise HTTPException(413, f"파일이 {MAX_VOICE_BYTES} bytes(15MB) 를 초과합니다")
    if not looks_like_mp3(data[:16]):
        raise HTTPException(400, "mp3 파일이 아닙니다 (ID3/프레임 시그니처 없음)")

    rel = voice.save(release_id, slot, data)
    files = dict(r.files or {})
    files[slot] = rel
    r.files = files
    _audit(db, "relax_voice_uploaded", f"relax_voice:{release_id}",
           {"slot": slot, "protocol": r.protocol, "bytes": len(data)})
    db.commit()
    return _release_view(r, None)


class PublishIn(BaseModel):
    release_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@ops_router.post("/releases/{release_id}/publish")
def publish_release(release_id: int, body: PublishIn,
                    db: Session = Depends(get_db)) -> dict:
    """draft → published. 배포일(KST 0시)을 UTC 로 변환해 release_at 저장.

    published 릴리즈는 이후 불변(재배포/업로드 불가). 최소 1개 슬롯이 있어야 배포 가능.
    """
    r = db.get(RelaxVoiceRelease, release_id)
    if r is None:
        raise HTTPException(404, "unknown release")
    if r.status != "draft":
        raise HTTPException(409, "이미 배포된 릴리즈입니다 — 새 draft 를 만드세요")
    if not (r.files or {}):
        raise HTTPException(422, "업로드된 음성이 없습니다 — 최소 1개 슬롯을 업로드하세요")
    try:
        release_at = kst_date_to_release_at(body.release_date)
    except (ValueError, TypeError):
        raise HTTPException(422, "release_date 형식 오류 (YYYY-MM-DD)")

    r.status = "published"
    r.release_at = release_at
    r.published_at = datetime.now(timezone.utc)
    _audit(db, "relax_voice_published", f"relax_voice:{release_id}",
           {"protocol": r.protocol, "release_date": body.release_date,
            "release_at_utc": release_at.isoformat(),
            "slots": list((r.files or {}).keys())})
    db.commit()
    active = active_release(db, r.protocol)
    return _release_view(r, active.id if active else None)


@ops_router.get("/{release_id}/{slot}.mp3")
def ops_preview_file(release_id: int, slot: str,
                     db: Session = Depends(get_db),
                     voice: RelaxVoiceStorage = Depends(_voice_storage)):
    """운영자 미리듣기 — draft/published 불문 전부 허용."""
    r = db.get(RelaxVoiceRelease, release_id)
    if r is None:
        raise HTTPException(404, "unknown release")
    if slot not in slots_for(r.protocol):
        raise HTTPException(404, "unknown slot")
    rel = (r.files or {}).get(slot)
    if not rel:
        raise HTTPException(404, "not uploaded")
    path = voice.abs_path(rel)
    if not path.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(path, media_type="audio/mpeg")


# ============================================================ 운영자 API — 긴 버전 시퀀스

@ops_router.get("/long_protocol")
def ops_long_protocol(db: Session = Depends(get_db)) -> dict:
    """긴 버전 시퀀스 편집기 데이터 — 단계별 행(길이·음악) + 음악 선택지 + 원본 config."""
    config = _get_long_config(db)
    return {"key": RELAX_LONG_PROTOCOL_KEY,
            "rows": long_protocol_rows(config),
            "music_options": RELAX_MUSIC_OPTIONS,
            "config": config}


class LongStepEdit(BaseModel):
    seconds: int = Field(ge=0, le=3600)
    music: str


class LongProtocolEditIn(BaseModel):
    steps: list[LongStepEdit]


@ops_router.post("/long_protocol")
def ops_save_long_protocol(body: LongProtocolEditIn,
                           db: Session = Depends(get_db)) -> dict:
    """긴 버전 시퀀스 저장 — 단계 길이·음악만 반영(종류/순서/텍스트 보존), 즉시 적용 + 감사."""
    config = _get_long_config(db)
    try:
        new_config = apply_long_protocol_edits(
            config, [{"seconds": s.seconds, "music": s.music} for s in body.steps])
    except ValueError as e:
        raise HTTPException(422, str(e))
    row = db.get(EnhanceProtocolConfig, RELAX_LONG_PROTOCOL_KEY)
    if row is None:
        db.add(EnhanceProtocolConfig(key=RELAX_LONG_PROTOCOL_KEY, config_json=new_config))
    else:
        row.config_json = new_config
    _audit(db, "relax_long_protocol_edited",
           f"enhance_protocol:{RELAX_LONG_PROTOCOL_KEY}",
           {"steps": [{"seconds": s.seconds, "music": s.music} for s in body.steps]})
    db.commit()
    return {"key": RELAX_LONG_PROTOCOL_KEY,
            "rows": long_protocol_rows(new_config),
            "music_options": RELAX_MUSIC_OPTIONS,
            "config": new_config}


# ============================================================ 앱 API (pull)

@app_router.get("/relax_voice")
def app_active(protocol: str = "short", db: Session = Depends(get_db)) -> dict:
    """현재 배포 중인 음성 세트(protocol 기본 short — 기존 앱 하위호환).

    active 는 published + 배포일 도래한 것 중 최신. slots 는 실제 업로드된 슬롯만.
    """
    if protocol not in RELAX_VOICE_PROTOCOLS:
        protocol = "short"        # 미지 값은 short 로 (구버전 앱 안전)
    r = active_release(db, protocol)
    if r is None:
        return {"active": None}
    files = r.files or {}
    slots = {slot: f"/v1/app/enhance/relax_voice/{r.id}/{slot}.mp3"
             for slot in slots_for(r.protocol) if slot in files}
    return {"active": {"release_id": r.id,
                       "release_at": _iso_kst(r.release_at),
                       "slots": slots}}


@app_router.get("/relax_voice/{release_id}/{slot}.mp3")
def app_file(release_id: int, slot: str,
             db: Session = Depends(get_db),
             voice: RelaxVoiceStorage = Depends(_voice_storage)):
    """음성 파일 — published + 배포일 도래한 릴리즈의 업로드된 슬롯만 200, 그 외 404."""
    r = db.get(RelaxVoiceRelease, release_id)
    if r is None or not _is_released(r):
        raise HTTPException(404, "not found")
    if slot not in slots_for(r.protocol):
        raise HTTPException(404, "not found")
    rel = (r.files or {}).get(slot)
    if not rel:
        raise HTTPException(404, "not found")
    path = voice.abs_path(rel)
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="audio/mpeg")


@app_router.get("/relax_long_protocol")
def app_long_protocol(db: Session = Depends(get_db)) -> dict:
    """긴 버전(5분) 프로토콜 전문 — 앱이 EnhanceProtocol 로 파싱(서버가 정본)."""
    return _get_long_config(db)
