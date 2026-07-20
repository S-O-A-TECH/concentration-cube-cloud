"""운영 관리자 API — /v1/ops/* (SPEC-02 §1.2, S6).

인증: 서명된 세션 쿠키(24h). live-evolution 과 완전 분리 —
이 네임스페이스에는 Evolution 기능(라벨·param_set 승격)이 한 조각도 없다.
"""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func, select
from sqlalchemy.orm import Session

from app.api.deps import JWT_ALGO, get_db
from app.config import get_settings
from app.focus_scoring import coach_templates as fs_coach_templates
from app.db.models import (AuditLog, Device, ParamSet, Profile, PromotedResult,
                           ScoringRun, StudySession, Subscription, User)
from app.services import queue as queue_service

OPS_COOKIE = "ops_session"
OPS_TTL_SEC = 24 * 3600


def make_ops_cookie() -> str:
    now = int(time.time())
    return jwt.encode({"sub": "ops", "role": "ops", "iat": now,
                       "exp": now + OPS_TTL_SEC},
                      get_settings().jwt_secret, algorithm=JWT_ALGO)


def require_ops(ops_session: str = Cookie(default="")) -> None:
    try:
        claims = jwt.decode(ops_session, get_settings().jwt_secret,
                            algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ops login required")
    if claims.get("role") != "ops":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ops role required")


router = APIRouter(prefix="/v1/ops", tags=["ops"])
protected = APIRouter(prefix="/v1/ops", tags=["ops"],
                      dependencies=[Depends(require_ops)])


def _audit(db: Session, action: str, target: str, detail: dict | None = None) -> None:
    db.add(AuditLog(actor="ops", action=action, target=target, detail_json=detail))


# ------------------------------------------------------------------ 인증

class LoginIn(BaseModel):
    id: str
    pw: str


# 로그인 잠금 — 5회 연속 실패 시 10분 (S7 §1.1). 단일 api 프로세스 전제(v0).
# 키에 클라이언트 IP 포함 — 타인이 admin id 오입력으로 실제 관리자를 잠그는 self-DoS 방지.
LOCKOUT_MAX_FAILS = 5
LOCKOUT_SEC = 600
_login_fails: dict[str, list[float]] = {}


def _locked(key: str) -> bool:
    fails = [t for t in _login_fails.get(key, []) if time.time() - t < LOCKOUT_SEC]
    _login_fails[key] = fails
    return len(fails) >= LOCKOUT_MAX_FAILS


@router.post("/login")
def login(body: LoginIn, response: Response, request: Request) -> dict:
    s = get_settings()
    client_host = request.client.host if request.client else "?"
    key = f"{body.id}|{client_host}"
    if _locked(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"too many failures — {LOCKOUT_SEC // 60}분 후 재시도")
    if not (secrets.compare_digest(body.id, s.ops_admin_id)
            and secrets.compare_digest(body.pw, s.ops_admin_pw)):
        _login_fails.setdefault(key, []).append(time.time())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    _login_fails.pop(key, None)
    response.set_cookie(OPS_COOKIE, make_ops_cookie(), max_age=OPS_TTL_SEC,
                        httponly=True, samesite="lax")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(OPS_COOKIE)
    return {"ok": True}


# ------------------------------------------------------------------ 대시보드
#
# 2026-07-07 개편: 구독이 첫 화면 — 국가별 활성 구독 카드(국가 미상=테스트 버킷)
# → 국가 클릭 → 기간(2/4/6/12개월)별 인원 → 계정 목록 → 계정 상세(기기·세션).
# 국가 원천: Google Play RTDN 검증의 regionCode (수동 부여 시 직접 입력).

SUB_MONTH_BUCKETS = (2, 4, 6, 12)
COUNTRY_NAMES = {"KR": "한국", "US": "미국", "CN": "중국", "JP": "일본",
                 "TW": "대만", "VN": "베트남", "SG": "싱가포르", "IN": "인도"}


def _months_bucket(sub: Subscription) -> int | None:
    """구독 기간 버킷 — months 컬럼 우선, 없으면(과거 데이터) 날짜로 근사."""
    if sub.months in SUB_MONTH_BUCKETS:
        return sub.months
    if sub.start_at and sub.expiry_at:
        days = (sub.expiry_at - sub.start_at).days
        return min(SUB_MONTH_BUCKETS, key=lambda m: abs(m * 30 - days))
    return None


@protected.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict:
    subs = db.execute(select(Subscription)
                      .where(Subscription.state.in_(("active", "trial")))
                      ).scalars().all()
    by_country: dict[str, dict] = {}
    for s in subs:
        cc = s.country or "test"
        b = by_country.setdefault(cc, {
            "country": cc,
            "name": "테스트" if cc == "test" else COUNTRY_NAMES.get(cc, cc),
            "total": 0,
            "by_months": {"2": 0, "4": 0, "6": 0, "12": 0, "etc": 0},
        })
        b["total"] += 1
        m = _months_bucket(s)
        b["by_months"][str(m) if m else "etc"] += 1
    # 정렬: 실국가(구독 많은 순) 먼저, 테스트 카드는 항상 맨 뒤
    countries = sorted(by_country.values(),
                       key=lambda x: (x["country"] == "test", -x["total"]))

    # 보조 지표(하단 한 줄) — 운영 건강 상태
    total_sessions = db.scalar(select(sa_func.count()).select_from(StudySession))
    failed = db.scalar(select(sa_func.count()).select_from(StudySession)
                       .where(StudySession.scoring_state == "failed"))
    accounts = db.scalar(select(sa_func.count()).select_from(User))

    # 동공 누락 알람 건수 — 정식 기기(테스트 hw_rev 아님/기기 없음)인데 동공(NIR)
    # 없이 재정규화된 승격 세션. 세션 수가 적은 로컬 환경이라 파이썬 루프로 충분하다.
    alarm_rows = db.execute(
        select(Device.hw_rev, ScoringRun.result_json)
        .select_from(PromotedResult)
        .join(ScoringRun, ScoringRun.id == PromotedResult.scoring_run_id)
        .join(StudySession, StudySession.id == PromotedResult.session_id)
        .outerjoin(Device, Device.id == StudySession.device_id)).all()
    pupil_alarms = sum(1 for hw_rev, result in alarm_rows
                       if pupil_alarm(hw_rev, result) is not None)
    return {
        "subscriptions": {
            "active_total": len(subs),
            "countries": countries,
        },
        "ops": {"accounts": accounts, "sessions": total_sessions,
                "scoring_failed": failed, "pupil_alarms": pupil_alarms},
    }


# ------------------------------------------------------------------ 버전 이력
#
# 2026-07-07 결정: Live Evolution 으로 서버의 내부 원칙(채점 param_set)이 채택될
# 때마다 서비스 버전이 0.0.0.1 씩 증가한다. 원천은 param_sets 채택 이력 —
# rationale(AI 의 오답노트 요약/수정 근거)이 곧 "그때 바뀐 내부 원칙"이다.

@protected.get("/versions")
def list_versions(db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(ParamSet).where(ParamSet.adopted_at.isnot(None))
                      .order_by(ParamSet.adopted_at.asc(), ParamSet.id.asc())
                      ).scalars().all()
    current = None
    items = []
    for i, p in enumerate(rows, start=1):
        ver = f"0.0.0.{i}"
        if p.status == "adopted":
            current = ver  # 롤백되면 직전 adopted 가 현재로 남는다
        items.append({
            "version": ver,
            "updated_at": p.adopted_at.strftime("%Y-%m-%d-%H"),
            "param_set_version": p.version,
            "engine_version": p.engine_version,
            "origin": p.origin,
            "agent": p.agent_name,
            "decided_by": p.decided_by,
            "principle": p.rationale or "(기록된 근거 없음)",
            "status": p.status,
        })
    items.reverse()  # 최신이 위
    return {"current": current, "count": len(items), "versions": items}


# ------------------------------------------------------------------ 계정/소비자
#
# 가족 계정 구조(2026-07-07 결정): 보호자 계정(User) 1 : 학생 프로필(Profile) ≤3 :
# 기기(Device) 무제한 바인딩. 리포트는 학생 프로필 단위로 시간순 정리한다.

MAX_PROFILES_PER_USER = 3

# 앱 pull 토큰 — S-A(OAuth) 전 임시 자격증명. 폐기는 재발급(구 토큰은 만료까지 유효)
# 또는 jwt_secret 교체로만 가능하다는 한계를 알고 쓰는 v0 스텁.
APP_TOKEN_TTL_SEC = 400 * 24 * 3600


def make_app_token(user_id: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": f"user:{user_id}", "role": "app", "iat": now,
                       "exp": now + APP_TOKEN_TTL_SEC},
                      get_settings().jwt_secret, algorithm=JWT_ALGO)


def _qc_flags(sess: StudySession, confidence: str | None) -> list[str]:
    """세션 목록용 QC 뱃지 — '측정 실패 ≠ 이탈' 원칙을 운영 화면에서도 드러낸다."""
    flags = []
    if sess.count_match is False:
        flags.append("count_mismatch")
    if sess.coverage is not None and sess.coverage < 0.7:
        flags.append("low_coverage")
    if confidence == "low":
        flags.append("low_confidence")
    if sess.scoring_state == "failed":
        flags.append("scoring_failed")
    if sess.excluded:
        flags.append("excluded")
    return flags


@protected.get("/users")
def list_users(q: str | None = None, offset: int = 0, limit: int = 50,
               db: Session = Depends(get_db)) -> dict:
    count_q = select(sa_func.count()).select_from(User)
    rows_q = (select(User, sa_func.count(Profile.id))
              .outerjoin(Profile, Profile.user_id == User.id)
              .group_by(User.id).order_by(User.id)
              .offset(max(offset, 0)).limit(min(limit, 200)))
    if q:
        count_q = count_q.where(User.email.ilike(f"%{q}%"))
        rows_q = rows_q.where(User.email.ilike(f"%{q}%"))
    total = db.scalar(count_q)
    rows = db.execute(rows_q).all()
    # 구독 요약(활성 여부) — 목록에서 바로 보이게
    sub_states = dict(db.execute(
        select(Subscription.user_id, sa_func.max(Subscription.state))
        .group_by(Subscription.user_id)).all())
    return {"total": total, "offset": offset,
            "users": [{"id": u.id, "email": u.email,
                       "auth_provider": u.auth_provider,
                       "created_at": str(u.created_at), "profiles": n,
                       "subscription": sub_states.get(u.id)}
                      for u, n in rows]}


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    auth_provider: str = "manual"


@protected.post("/users")
def create_user(body: UserIn, db: Session = Depends(get_db)) -> dict:
    """보호자 계정 수동 생성 — 앱 pull 토큰은 이 응답에서 1회만 노출."""
    if db.execute(select(User).where(User.email == body.email)).scalar_one_or_none():
        raise HTTPException(409, "email already registered")
    user = User(email=body.email, auth_provider=body.auth_provider)
    db.add(user)
    db.flush()
    token = make_app_token(user.id)
    _audit(db, "user_created", body.email, None)
    db.commit()
    return {"id": user.id, "email": user.email, "app_token": token,
            "note": "app_token 은 이 응답에서 1회만 표시됩니다 — 앱 설정에 보관하세요"}


@protected.post("/users/{user_id}/app_token")
def reissue_app_token(user_id: int, db: Session = Depends(get_db)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "unknown user")
    _audit(db, "app_token_reissued", f"user:{user_id}", None)
    db.commit()
    return {"user_id": user_id, "app_token": make_app_token(user_id),
            "note": "구 토큰은 만료 전까지 유효합니다 (v0 한계 — S-A 에서 세션 관리로 대체)"}


class ProfileIn(BaseModel):
    nickname: str = Field(min_length=1, max_length=64)
    birth_date: date | None = None


@protected.post("/users/{user_id}/profiles")
def create_profile(user_id: int, body: ProfileIn,
                   db: Session = Depends(get_db)) -> dict:
    """학생 프로필 생성 — 계정당 최대 3명(2026-07-07 결정)."""
    if db.get(User, user_id) is None:
        raise HTTPException(404, "unknown user")
    count = db.scalar(select(sa_func.count()).select_from(Profile)
                      .where(Profile.user_id == user_id))
    if count >= MAX_PROFILES_PER_USER:
        raise HTTPException(409, f"profile limit ({MAX_PROFILES_PER_USER}) reached")
    profile = Profile(user_id=user_id, nickname=body.nickname,
                      birth_date=body.birth_date)
    db.add(profile)
    db.flush()
    _audit(db, "profile_created", f"user:{user_id}", {"nickname": body.nickname})
    db.commit()
    return {"id": profile.id, "user_id": user_id, "nickname": profile.nickname}


@protected.get("/users/{user_id}")
def user_detail(user_id: int, limit: int = 50,
                db: Session = Depends(get_db)) -> dict:
    """계정 상세 — 프로필(≤3)·기기·구독 + 프로필별 최근 데이터 시간순.

    각 세션에 채점 엔진 버전(param_set_version)과 QC 플래그를 함께 실어,
    소급 재채점(Live Evolution 채택) 시 어떤 엔진으로 채점된 결과인지 즉시 보인다.
    열람은 감사로그에 남는다(전체 열람+감사 — 2026-07-07 결정).
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "unknown user")
    profiles = db.execute(select(Profile).where(Profile.user_id == user_id)
                          .order_by(Profile.id)).scalars().all()
    devices = db.execute(select(Device).where(Device.owner_user_id == user_id)
                         .order_by(Device.id)).scalars().all()
    subs = db.execute(select(Subscription).where(Subscription.user_id == user_id)
                      .order_by(Subscription.id)).scalars().all()

    pids = [p.id for p in profiles]
    sessions_by_profile: dict[int, list[dict]] = {pid: [] for pid in pids}
    if pids:
        rows = db.execute(
            select(StudySession, Device.serial, ScoringRun.sfi,
                   ScoringRun.confidence, ParamSet.version)
            .join(Device, Device.id == StudySession.device_id)
            .outerjoin(PromotedResult, PromotedResult.session_id == StudySession.id)
            .outerjoin(ScoringRun, ScoringRun.id == PromotedResult.scoring_run_id)
            .outerjoin(ParamSet, ParamSet.id == ScoringRun.param_set_id)
            .where(StudySession.profile_id.in_(pids))
            .order_by(StudySession.started_at.desc())
            .limit(min(limit, 500))).all()
        for s, serial, sfi, confidence, engine in rows:
            sessions_by_profile[s.profile_id].append({
                "sid": str(s.id),
                "display": s.started_at.strftime("%Y-%m-%d %H:%M") if s.started_at else None,
                "mode": s.mode, "device_serial": serial,
                "sfi": sfi, "engine": engine,
                "coverage": s.coverage, "scoring_state": s.scoring_state,
                "qc_flags": _qc_flags(s, confidence),
            })

    _audit(db, "account_viewed", f"user:{user_id}", None)
    db.commit()
    return {"id": user.id, "email": user.email, "auth_provider": user.auth_provider,
            "created_at": str(user.created_at),
            "profiles": [{"id": p.id, "nickname": p.nickname,
                          "birth_date": str(p.birth_date) if p.birth_date else None,
                          "sessions": sessions_by_profile.get(p.id, [])}
                         for p in profiles],
            "devices": [{"id": d.id, "serial": d.serial,
                         "default_profile_id": d.default_profile_id,
                         "fw_ver": d.fw_ver} for d in devices],
            "subscriptions": [{"id": s.id, "product": s.product, "state": s.state,
                               "source": s.source,
                               "start_at": str(s.start_at) if s.start_at else None,
                               "expiry_at": str(s.expiry_at) if s.expiry_at else None}
                              for s in subs],
            "profile_limit": MAX_PROFILES_PER_USER}


@protected.get("/profiles")
def list_profiles(user_id: int | None = None, db: Session = Depends(get_db)) -> dict:
    q = select(Profile).order_by(Profile.id)
    if user_id is not None:
        q = q.where(Profile.user_id == user_id)
    rows = db.execute(q).scalars().all()
    return {"profiles": [{"id": p.id, "user_id": p.user_id, "nickname": p.nickname,
                          "birth_date": str(p.birth_date) if p.birth_date else None}
                         for p in rows]}


# ------------------------------------------------------------------ 기기

@protected.get("/devices")
def list_devices(db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Device).order_by(Device.id)).scalars().all()
    counts = dict(db.execute(
        select(StudySession.device_id, sa_func.count())
        .group_by(StudySession.device_id)).all())
    return {"devices": [{"id": d.id, "serial": d.serial, "hw_rev": d.hw_rev,
                         "fw_ver": d.fw_ver, "research_flag": d.research_flag,
                         "sessions": counts.get(d.id, 0),
                         "created_at": str(d.created_at)} for d in rows]}


class DeviceIn(BaseModel):
    serial: str = Field(min_length=3, max_length=64)
    hw_rev: str | None = None
    research_flag: bool = False


@protected.post("/devices")
def register_device(body: DeviceIn, db: Session = Depends(get_db)) -> dict:
    """기기 등록 — factory_token 평문은 이 응답에서 1회만 노출."""
    if db.execute(select(Device).where(Device.serial == body.serial)).scalar_one_or_none():
        raise HTTPException(409, "serial already registered")
    token = secrets.token_hex(16)
    device = Device(serial=body.serial, hw_rev=body.hw_rev,
                    research_flag=body.research_flag,
                    factory_token_hash=hashlib.sha256(token.encode()).hexdigest())
    db.add(device)
    _audit(db, "device_registered", body.serial, {"research_flag": body.research_flag})
    db.commit()
    return {"id": device.id, "serial": device.serial, "factory_token": token,
            "note": "factory_token 은 이 응답에서 1회만 표시됩니다"}


@protected.post("/devices/{device_id}/reissue_token")
def reissue_token(device_id: int, db: Session = Depends(get_db)) -> dict:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "unknown device")
    token = secrets.token_hex(16)
    device.factory_token_hash = hashlib.sha256(token.encode()).hexdigest()
    _audit(db, "device_token_reissued", device.serial, None)
    db.commit()
    return {"serial": device.serial, "factory_token": token,
            "note": "이전 토큰은 즉시 무효화되었습니다"}


class BindIn(BaseModel):
    """기기 → 계정/프로필 바인딩. null 로 해제. 기기 수 제한 없음(2026-07-07 결정)."""
    user_id: int | None = None
    default_profile_id: int | None = None


@protected.post("/devices/{device_id}/bind")
def bind_device(device_id: int, body: BindIn,
                db: Session = Depends(get_db)) -> dict:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "unknown device")
    if body.user_id is not None and db.get(User, body.user_id) is None:
        raise HTTPException(404, "unknown user")
    if body.default_profile_id is not None:
        profile = db.get(Profile, body.default_profile_id)
        if profile is None:
            raise HTTPException(404, "unknown profile")
        effective_user = body.user_id if body.user_id is not None else device.owner_user_id
        if profile.user_id != effective_user:
            raise HTTPException(422, "profile does not belong to the bound user")
    device.owner_user_id = body.user_id
    device.default_profile_id = body.default_profile_id
    _audit(db, "device_bound", device.serial,
           {"user_id": body.user_id, "default_profile_id": body.default_profile_id})
    db.commit()
    return {"ok": True, "serial": device.serial,
            "user_id": device.owner_user_id,
            "default_profile_id": device.default_profile_id}


class ResearchFlagIn(BaseModel):
    research_flag: bool


@protected.post("/devices/{device_id}/research_flag")
def set_research_flag(device_id: int, body: ResearchFlagIn,
                      db: Session = Depends(get_db)) -> dict:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "unknown device")
    device.research_flag = body.research_flag
    _audit(db, "device_research_flag", device.serial, {"value": body.research_flag})
    db.commit()
    return {"ok": True, "research_flag": device.research_flag}


# ------------------------------------------------------------------ 세션 운영 뷰

@protected.get("/sessions")
def list_sessions(state: str | None = None, count_match: bool | None = None,
                  device: str | None = None, limit: int = 100,
                  db: Session = Depends(get_db)) -> dict:
    q = (select(StudySession, Device.serial, ScoringRun.sfi)
         .join(Device, Device.id == StudySession.device_id)
         .outerjoin(PromotedResult, PromotedResult.session_id == StudySession.id)
         .outerjoin(ScoringRun, ScoringRun.id == PromotedResult.scoring_run_id)
         .order_by(StudySession.started_at.desc()).limit(min(limit, 500)))
    if state:
        q = q.where(StudySession.upload_state == state)
    if count_match is not None:
        q = q.where(StudySession.count_match.is_(count_match))
    if device:
        q = q.where(Device.serial == device)
    rows = db.execute(q).all()
    return {"sessions": [{
        "sid": str(s.id),
        "display": s.started_at.strftime("%Y-%m-%d %H:%M") if s.started_at else None,
        "device_serial": serial, "mode": s.mode,
        "upload_state": s.upload_state, "count_match": s.count_match,
        "scoring_state": s.scoring_state, "coverage": s.coverage,
        "research_mode": s.research_mode, "sfi": sfi,
    } for s, serial, sfi in rows]}


# 측정 소스 분류(2026-07-07 결정) — 테스트 기기(웹캠/가상)는 시선 추적만으로 채점
# (동공(NIR) 성분은 데이터에 없어 채점 엔진이 자동으로 제외·재정규화한다.
#  quality.sfi_renormalized 가 그 사실의 계약 필드 — 스위치가 아니라 데이터 기반).
# load-test: 2026-07-05 부하 리허설(LOAD_DEV_*) 잔재 세션 — 동공 누락 알람 대상 아님.
TEST_HW_REVS = ("webcam-proto", "virtual", "load-test")


def measurement_source(hw_rev: str | None) -> dict:
    is_test = hw_rev in TEST_HW_REVS
    return {"hw_rev": hw_rev,
            "kind": "test" if is_test else "production",
            "label": ("시선 추적만 — PC 테스트 기기(동공/NIR 제외 재정규화)"
                      if is_test else "정식 기기(NIR — 시선+동공)")}


def pupil_alarm(hw_rev: str | None, result: dict | None) -> dict | None:
    """정식 기기 세션인데 동공(NIR) 없이 채점됨 — 관리자 알람(2026-07-07 결정).
    테스트 기기(웹캠/가상)는 동공 부재가 전제라 알람 대상이 아니다."""
    if hw_rev in TEST_HW_REVS or not result:
        return None
    if (result.get("quality") or {}).get("sfi_renormalized"):
        return {"code": "pupil_missing_on_production",
                "message": "정식 기기 세션인데 동공(NIR) 신호가 없습니다 — 기기 점검 필요. 이 점수는 시선 추적만으로 재정규화된 참고값입니다."}
    return None


COACH_LANGS = ("ko", "en", "zh")


def _legacy_coach_localized(result: dict, lang: str) -> dict | None:
    """다국어 이전에 채점된 result(평면 한국어 coach_text)용 — 리포트 시점에 같은
    순수 템플릿(focus_scoring.coach_templates)으로 요청 언어 문구를 재합성한다.

    숫자는 result 에 저장된 사실값을 그대로 쓰므로 재채점이 필요 없고, 같은 입력이면
    항상 같은 출력이다(순수함수 원칙 유지). ko 요청은 원문이 이미 한국어라 None."""
    if lang == "ko":
        return None
    if result.get("sfi") is None:
        text = fs_coach_templates.unscorable_text(lang)
        return {"student": text, "parent": text}
    events = result.get("events") or []
    n_off = sum(1 for e in events if e.get("type") == "off_page")
    n_blank = sum(1 for e in events if e.get("type") == "blank_stare")
    focused = float(result.get("focused_minutes") or 0.0)
    streak = float(result.get("max_focus_streak_min") or 0.0)
    coverage = float((result.get("quality") or {}).get("coverage") or 0.0)
    return {
        "student": fs_coach_templates.student_text(
            result["sfi"], focused, streak, n_off,
            result.get("mean_return_sec"), n_blank, lang=lang),
        "parent": fs_coach_templates.parent_text(
            result["sfi"], focused, streak, n_off, n_blank, coverage, lang=lang),
    }


def _slice_llm_report(llm: dict | None, lang: str) -> dict | None:
    """llm_report_json 에서 요청 언어의 {student, parent}(+메타)만 뽑는다.

    - 신규 스키마 {ko:{student,parent}, en:{...}, zh:{...}, model, generated_at}:
      요청 언어 슬라이스 반환.
    - 구버전 평면 {student, parent, model, generated_at}(한국어 전용, 다국어 이전 생성):
      ko 요청에만 반환 — en/zh 요청에 한국어를 내보내면 언어 통일이 깨지므로 None
      (호출부는 None 이면 이미 로컬라이즈된 템플릿 coach_text 를 그대로 쓴다).
    """
    if not isinstance(llm, dict):
        return None
    meta = {k: llm[k] for k in ("model", "generated_at") if k in llm}
    per_lang = llm.get(lang)
    if isinstance(per_lang, dict) and "student" in per_lang and "parent" in per_lang:
        return {"student": str(per_lang["student"]),
                "parent": str(per_lang["parent"]), **meta}
    if lang == "ko" and "student" in llm and "parent" in llm:
        return {"student": str(llm["student"]), "parent": str(llm["parent"]), **meta}
    return None


def build_session_report(db: Session, sid: uuid.UUID, lang: str = "ko") -> dict:
    """리포트 JSON 정본 — ops 콘솔과 앱(/v1/app/*)이 **같은 함수**를 쓴다(단일 정본,
    2026-07-07 결정). promoted_results 가 승격 시 교체되므로 소급 재채점은 자동 반영.

    [lang] (2026-07-16): result.coach_text 는 focus_scoring 이 ko/en/zh 를 모두
    담아 두므로, 여기서 요청 언어 하나만 골라 기존 {student, parent} 평면 shape 으로
    되돌려준다 — 클라이언트 파싱은 그대로. 구버전(다국어 이전) 캐시 result_json 은
    coach_text 가 이미 평면 shape(한국어 고정)이라 그대로 둔다(하위호환)."""
    sess = db.get(StudySession, sid)
    if sess is None:
        raise HTTPException(404, "unknown session")
    promoted = db.get(PromotedResult, sid)
    if promoted is None:
        raise HTTPException(404, f"no promoted result (scoring: {sess.scoring_state})")
    run = db.get(ScoringRun, promoted.scoring_run_id)
    ps = db.get(ParamSet, run.param_set_id)
    device = db.get(Device, sess.device_id)
    result = dict(run.result_json)
    coach = result.get("coach_text")
    if lang not in COACH_LANGS:
        lang = "ko"
    if isinstance(coach, dict) and isinstance(coach.get(lang), dict):
        result["coach_text"] = coach[lang]  # 신규 스키마: {ko:{...}, en:{...}, zh:{...}}
    else:
        # 구버전 평면 {student, parent}(한국어 전용) 캐시 — en/zh 요청이면 같은
        # 순수 템플릿으로 요청 언어 문구를 재합성한다(ko 는 원문 그대로).
        localized = _legacy_coach_localized(result, lang)
        if localized:
            result["coach_text"] = localized
    llm = _slice_llm_report(run.llm_report_json, lang)
    if llm:
        result["llm_report"] = llm
        # LLM 코칭이 요청 언어로 존재하면 템플릿 문구 대신 노출한다(2026-07-16).
        # 원래 S3 의 의도("숫자 SFI → 사람이 읽는 코칭 문장")가 이제야 앱에 닿는다 —
        # 이전에는 result["llm_report"] 키로만 실려 앱(coach_text 만 읽음)에 미전달.
        # 응답에서만 덮고 result_json(순수 채점 산출물)은 건드리지 않는다.
        result["coach_text"] = {"student": llm["student"], "parent": llm["parent"]}
    return {"sid": str(sid), "profile_id": sess.profile_id,
            "display": sess.started_at.strftime("%Y-%m-%d %H:%M") if sess.started_at else None,
            "mode": sess.mode, "param_set_version": ps.version if ps else None,
            "glasses": sess.glasses,   # 안경 착용(기기 감지, null=미상 — 2026-07-08)
            "device": {"serial": device.serial if device else None,
                       **measurement_source(device.hw_rev if device else None)},
            "alarm": pupil_alarm(device.hw_rev if device else None, result),
            "result": result}


@protected.get("/sessions/{sid}/report")
def session_report(sid: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """★ 리포트 뷰 데이터 — 사람이 가장 먼저 결과를 확인하는 곳 (SPEC-03 §5).
    운영자 열람은 감사로그에 남는다(전체 열람+감사 — 2026-07-07 결정)."""
    data = build_session_report(db, sid)
    _audit(db, "report_viewed", str(sid), None)
    db.commit()
    return data


@protected.post("/sessions/{sid}/rescore")
def rescore_session(sid: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    sess = db.get(StudySession, sid)
    if sess is None:
        raise HTTPException(404, "unknown session")
    if sess.upload_state != "complete":
        raise HTTPException(409, "session upload not complete")
    sess.scoring_state = "queued"
    _audit(db, "session_rescore_requested", str(sid), None)
    db.commit()
    try:
        queue_service.enqueue_score_session(str(sid))
    except Exception as e:
        sess.scoring_state = "failed"
        sess.scoring_error = f"enqueue: {e}"[:500]
        db.commit()
        raise HTTPException(503, "재채점 큐잉 실패 — 잠시 후 재시도")
    return {"ok": True, "scoring": "queued"}


# ------------------------------------------------------------------ 구독·개인정보·감사
#
# 구독은 v0 수동 관리 + 결제 웹훅 스텁(app/api/billing.py)까지 — 2026-07-07 결정.
# PG 확정 시 웹훅 모듈의 서명 검증/이벤트 매핑만 교체하면 된다.

SUB_STATES = ("trial", "active", "expired", "canceled")


@protected.get("/subscriptions")
def list_subscriptions(country: str | None = None, months: int | None = None,
                       state: str | None = None,
                       db: Session = Depends(get_db)) -> dict:
    """구독 목록 — 대시보드 드릴다운 필터(country: 'test'=국가 미상, months: 2/4/6/12)."""
    q = (select(Subscription, User.email)
         .outerjoin(User, User.id == Subscription.user_id)
         .order_by(Subscription.id.desc()))
    if country == "test":
        q = q.where(Subscription.country.is_(None))
    elif country:
        q = q.where(Subscription.country == country.upper())
    if state:
        q = q.where(Subscription.state == state)
    rows = db.execute(q).all()
    out = []
    for s, email in rows:
        m = _months_bucket(s)
        if months is not None and m != months:
            continue
        out.append({
            "id": s.id, "user_id": s.user_id, "email": email,
            "source": s.source, "product": s.product, "state": s.state,
            "country": s.country, "months": m,
            "start_at": str(s.start_at) if s.start_at else None,
            "expiry_at": str(s.expiry_at) if s.expiry_at else None,
        })
    return {"subscriptions": out, "states": list(SUB_STATES),
            "filter": {"country": country, "months": months, "state": state}}


class SubscriptionIn(BaseModel):
    user_id: int
    product: str = Field(default="focus_report", max_length=64)
    state: str = "active"
    months: int = Field(default=12, ge=1, le=24)   # start_at 부터 개월 수
    country: str | None = Field(default=None, min_length=2, max_length=2)


@protected.post("/subscriptions")
def grant_subscription(body: SubscriptionIn, db: Session = Depends(get_db)) -> dict:
    """수동 구독 부여 — 파일럿/검증 연구 운영용. country 없으면 '테스트' 버킷."""
    if body.state not in SUB_STATES:
        raise HTTPException(422, f"state must be one of {SUB_STATES}")
    if db.get(User, body.user_id) is None:
        raise HTTPException(404, "unknown user")
    now = datetime.now(timezone.utc)
    sub = Subscription(user_id=body.user_id, source="manual", product=body.product,
                       state=body.state, start_at=now,
                       months=body.months,
                       country=body.country.upper() if body.country else None,
                       expiry_at=now + timedelta(days=30 * body.months),
                       last_verified_at=now)
    db.add(sub)
    db.flush()
    _audit(db, "subscription_granted", f"user:{body.user_id}",
           {"product": body.product, "state": body.state, "months": body.months})
    db.commit()
    return {"id": sub.id, "user_id": sub.user_id, "state": sub.state,
            "expiry_at": str(sub.expiry_at)}


class SubscriptionUpdateIn(BaseModel):
    state: str | None = None
    extend_months: int | None = Field(default=None, ge=1, le=24)


@protected.post("/subscriptions/{sub_id}")
def update_subscription(sub_id: int, body: SubscriptionUpdateIn,
                        db: Session = Depends(get_db)) -> dict:
    sub = db.get(Subscription, sub_id)
    if sub is None:
        raise HTTPException(404, "unknown subscription")
    if body.state is not None:
        if body.state not in SUB_STATES:
            raise HTTPException(422, f"state must be one of {SUB_STATES}")
        sub.state = body.state
    if body.extend_months:
        base_dt = sub.expiry_at or datetime.now(timezone.utc)
        sub.expiry_at = base_dt + timedelta(days=30 * body.extend_months)
    sub.last_verified_at = datetime.now(timezone.utc)
    _audit(db, "subscription_updated", f"sub:{sub_id}",
           {"state": body.state, "extend_months": body.extend_months})
    db.commit()
    return {"id": sub.id, "state": sub.state,
            "expiry_at": str(sub.expiry_at) if sub.expiry_at else None}


class DeleteProfileIn(BaseModel):
    profile_id: int
    confirm: bool = False


@protected.post("/privacy/delete_profile")
def delete_profile(body: DeleteProfileIn, db: Session = Depends(get_db)) -> dict:
    """삭제권 집행 트리거 — 실제 삭제 잡은 S7 (2단 확인)."""
    if not body.confirm:
        raise HTTPException(422, "confirm:true required (2단 확인)")
    profile = db.get(Profile, body.profile_id)
    if profile is None:
        raise HTTPException(404, "unknown profile")
    _audit(db, "privacy_delete_requested", f"profile:{body.profile_id}",
           {"nickname": profile.nickname})
    db.commit()
    queue_service.enqueue_delete_profile(body.profile_id)
    return {"ok": True, "note": "삭제 잡이 큐잉되었습니다 (S7)"}


@protected.get("/audit")
def audit_log(limit: int = 100, db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.desc())
                      .limit(min(limit, 500))).scalars().all()
    return {"audit": [{"id": a.id, "actor": a.actor, "action": a.action,
                       "target": a.target, "detail": a.detail_json, "at": str(a.at)}
                      for a in rows]}
