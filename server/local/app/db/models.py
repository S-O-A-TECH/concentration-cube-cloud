"""SQLAlchemy 2.0 모델 — SPEC-01 §1 전 테이블.

설계 노트
- ENUM 은 native_enum=False (VARCHAR + CHECK) — PG 타입 관리 없이 Alembic up/down 왕복이 깨끗하고
  sqlite(단위테스트)에서도 동일하게 동작한다.
- JSON 컬럼은 PG 에서 JSONB, sqlite 에서 JSON 으로 동작하는 variant 타입.
- v0 미사용 테이블(users/profiles/subscriptions/intervention_*)도 스키마는 전부 생성 (S1 구현계획 §1.1).
"""
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# PG=JSONB / sqlite=JSON
JsonCol = JSON().with_variant(JSONB(), "postgresql")
# sqlite 자동증가 호환 BigInteger
BigIntPk = BigInteger().with_variant(Integer(), "sqlite")


def _enum(*values: str, name: str) -> Enum:
    return Enum(*values, name=name, native_enum=False, length=24)


# ---------- 계정/프로필 (v0: 스키마만, API 는 phase4 시점) ----------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    auth_provider: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    nickname: Mapped[str] = mapped_column(String(64))
    birth_date: Mapped[date | None] = mapped_column(Date)  # 나이 해석 목적 제한 (phase4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- 기기 ----------

class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    serial: Mapped[str] = mapped_column(String(64), unique=True)
    hw_rev: Mapped[str | None] = mapped_column(String(32))
    fw_ver: Mapped[str | None] = mapped_column(String(32))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # 앱 없이 올라온 세션의 프로필 귀속 (Flutter 대비 — SPEC-01)
    default_profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"))
    factory_token_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 hex
    research_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- 세션 ----------

class StudySession(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"))
    mode: Mapped[str] = mapped_column(String(16))            # SFI-20/30/40, DEV-2 ...
    schema_version: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_uri: Mapped[str | None] = mapped_column(String(512))  # parquet 경로
    coverage: Mapped[float | None] = mapped_column(Float)
    upload_state: Mapped[str] = mapped_column(
        _enum("open", "complete", "failed", name="upload_state"), default="open"
    )
    expected_samples: Mapped[int | None] = mapped_column(Integer)
    uploaded_samples: Mapped[int] = mapped_column(Integer, default=0)
    count_match: Mapped[bool | None] = mapped_column(Boolean)
    research_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    split: Mapped[str] = mapped_column(
        _enum("train", "holdout", "na", name="session_split"), default="na"
    )
    # 안경 착용 여부(2026-07-08, 기술문서 v4.1 "eyeglass flag") — 실물 기기가
    # 캘리브레이션 중 자동 감지해 보고. None=미상. 진화 분석의 그룹 축(안경/비안경
    # 정확도 분리 검증) + 유효율 저하의 원인 해석용. 채점 로직 분기에는 쓰지 않는다.
    glasses: Mapped[bool | None] = mapped_column(Boolean)
    # 연구 제외 (실패 검증 세션 — evaluate·오답노트 배제, 삭제는 하지 않음: SPEC-01 §3.1)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    excluded_reason: Mapped[str | None] = mapped_column(Text)
    # 채점 잡 상태 (S3 계획 §1.5) — done 판정의 근거는 promoted_results 존재
    scoring_state: Mapped[str] = mapped_column(
        _enum("none", "queued", "running", "done", "failed", name="scoring_state"),
        default="none",
    )
    scoring_error: Mapped[str | None] = mapped_column(Text)


# ---------- 채점 (원본 불변 — 재채점은 언제나 새 row) ----------

class ParamSet(Base):
    __tablename__ = "param_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)   # 예: v1.0, v1.1-gen3
    engine_version: Mapped[str] = mapped_column(String(32), default="builtin")  # SPEC-03 §3.1
    json_params: Mapped[dict] = mapped_column(JsonCol)
    origin: Mapped[str] = mapped_column(_enum("seed", "agent", "manual", name="param_origin"))
    agent_name: Mapped[str | None] = mapped_column(String(64))
    rationale: Mapped[str | None] = mapped_column(Text)             # AI 수정 근거 (오답노트 요약)
    status: Mapped[str] = mapped_column(
        _enum("candidate", "evaluating", "passed", "adopted", "rejected", "rolled_back",
              name="param_status"),
        default="candidate",
    )
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("param_sets.id"))  # 세대 계보
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    report_json: Mapped[dict | None] = mapped_column(JsonCol)   # evaluate 성적표 (SPEC-02 §2.6)


class EvolutionJob(Base):
    """evaluate/rescore 잡 진행률 — GET /v1/evolution/jobs 노출용 (S5)."""
    __tablename__ = "evolution_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        _enum("evaluate", "promote_rescore", "rollback_rescore", name="evolution_job_kind"))
    param_set_id: Mapped[int | None] = mapped_column(ForeignKey("param_sets.id"))
    state: Mapped[str] = mapped_column(
        _enum("queued", "running", "done", "failed", name="evolution_job_state"),
        default="queued")
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    detail_json: Mapped[dict | None] = mapped_column(JsonCol)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ScoringRun(Base):
    __tablename__ = "scoring_runs"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    param_set_id: Mapped[int] = mapped_column(ForeignKey("param_sets.id"))
    sfi: Mapped[float | None] = mapped_column(Float)        # 커버리지 미달 시 null
    confidence: Mapped[str | None] = mapped_column(String(16))   # "high"|"low" (result 계약)
    components_json: Mapped[dict | None] = mapped_column(JsonCol)
    timeline_json: Mapped[list | None] = mapped_column(JsonCol)
    events_json: Mapped[list | None] = mapped_column(JsonCol)
    quality_json: Mapped[dict | None] = mapped_column(JsonCol)
    result_json: Mapped[dict | None] = mapped_column(JsonCol)   # result 계약 전문 (§4.4) — API 는 이걸 그대로 반환
    llm_report_json: Mapped[dict | None] = mapped_column(JsonCol)  # LLM 코칭 리포트 (선택 강화 — 결정적 채점과 분리)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PromotedResult(Base):
    """앱/기기가 보는 유일한 결과 — 세션당 1 row, 승격 시 교체."""
    __tablename__ = "promoted_results"

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    scoring_run_id: Mapped[int] = mapped_column(ForeignKey("scoring_runs.id"))


# ---------- 정답지 (Evolution 의 원료) ----------

class Label(Base):
    """★불변: UPDATE 없음, 재-POST 는 409 — 실패 세션은 sessions.excluded 처리 후 재시도."""
    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), unique=True)
    labeler: Mapped[str] = mapped_column(String(64))        # v0: 'admin', 임상: 연구원 id
    method: Mapped[str] = mapped_column(
        _enum("realtime_instructed", "retrospective", name="label_method"),
        default="realtime_instructed",  # 정본 = realtime_instructed (live-evolution SPEC-05)
    )
    protocol: Mapped[str | None] = mapped_column(String(64))  # 지시 프로토콜 프리셋명
    segments_json: Mapped[list] = mapped_column(JsonCol)      # [{t0,t1,label}] — 공백 구간 = 평가 제외
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- 강화 기능(phase12) — 채점 입력 아님, 스키마만 ----------

class InterventionEvent(Base):
    __tablename__ = "intervention_events"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict | None] = mapped_column(JsonCol)
    t_offset_ms: Mapped[int | None] = mapped_column(Integer)


class InterventionEffectiveness(Base):
    __tablename__ = "intervention_effectiveness"

    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), primary_key=True)
    condition: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric_json: Mapped[dict | None] = mapped_column(JsonCol)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------- 구독 (스키마만, phase6 시점 활성화) ----------

class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str | None] = mapped_column(String(16))    # play/appstore/web
    product: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str | None] = mapped_column(String(16))
    # 구매 국가(ISO 3166-1 alpha-2) — Google Play RTDN 검증 응답의 regionCode 가
    # 원천(2026-07-07 대시보드 개편). null = 국가 미상(수동/테스트) → '테스트' 버킷.
    country: Mapped[str | None] = mapped_column(String(2))
    months: Mapped[int | None] = mapped_column(Integer)       # 2/4/6/12 구독 기간
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------- 감사 ----------

class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(128))
    detail_json: Mapped[dict | None] = mapped_column(JsonCol)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- 수면세션 음성 배포 (2026-07-07) ----------

class RelaxVoiceRelease(Base):
    """운영자가 업로드한 수면-릴랙스 세션 단계별 음성(mp3) 배포 릴리즈.

    앱은 published + release_at(KST 배포일 0시 → UTC 저장) 도래한 최신 릴리즈를
    pull 해 폰 TTS 대신 재생한다. published 릴리즈는 불변 — 대사 교체는 새 draft 로만.
    files = {slot: 스토리지 상대경로}, slot ∈ {close_eyes, pmr, breathing, closing}.
    """
    __tablename__ = "relax_voice_releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 수면 세션 버전 — short(기존 4슬롯) | long(긴 버전 5분, 7슬롯). active 는 protocol 별 독립.
    protocol: Mapped[str] = mapped_column(
        _enum("short", "long", name="relax_voice_protocol"), default="short")
    status: Mapped[str] = mapped_column(
        _enum("draft", "published", name="relax_voice_status"), default="draft")
    release_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(255))
    files: Mapped[dict] = mapped_column(JsonCol, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EnhanceProtocolConfig(Base):
    """향상 세션 프로토콜(시퀀스·음악)의 서버 정본 — 운영콘솔에서 편집, 앱이 pull.

    key='relax_long_v1' 하나로 시작(긴 버전 5분). config_json 은 앱 EnhanceProtocol 스키마
    전문이고, 앱은 이 JSON 을 그대로 파싱한다(서버가 정본, 앱 내장 에셋은 오프라인 폴백).
    """
    __tablename__ = "enhance_protocol_configs"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_json: Mapped[dict] = mapped_column(JsonCol)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
