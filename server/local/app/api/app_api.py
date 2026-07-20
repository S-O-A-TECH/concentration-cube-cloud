"""앱 조회 API — /v1/app/* (pull 전용, 2026-07-07 결정).

원칙(사용자 확정):
① 앱이 열릴 때 서버에서 가져온다(pull) — 서버는 푸시하지 않는다.
② 채점은 서버가 하고 앱은 결과 리포트만 읽는다 — 리포트 JSON 정본은
   ops.build_session_report **한 함수**를 콘솔과 공유한다(단일 정본).
③ Live Evolution 채택으로 promoted_results 가 교체되면 과거 리포트도
   다음 pull 에서 자동으로 새 채점을 받는다(소급 재채점 반영).

인증: Authorization: Bearer <app_token> — ops 콘솔에서 계정 생성/재발급 시
받은 계정 단위 JWT(role=app). S-A(OAuth) 도입 시 이 검증부만 교체한다.
"""
from __future__ import annotations

import uuid
from datetime import date

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func, select
from sqlalchemy.orm import Session

from app.api.deps import JWT_ALGO, get_db
from app.api.ops import MAX_PROFILES_PER_USER, _qc_flags, build_session_report
from app.config import get_settings
from app.db.models import (AuditLog, Device, ParamSet, Profile, PromotedResult,
                           ScoringRun, StudySession, Subscription, User)

router = APIRouter(prefix="/v1/app", tags=["app"])


def require_app_user(authorization: str = Header(default="")) -> int:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = jwt.decode(authorization.removeprefix("Bearer "),
                            get_settings().jwt_secret, algorithms=[JWT_ALGO])
        if claims.get("role") != "app":
            raise ValueError("not an app token")
        return int(str(claims["sub"]).removeprefix("user:"))
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")


def _own_profile(db: Session, user_id: int, profile_id: int) -> Profile:
    profile = db.get(Profile, profile_id)
    if profile is None or profile.user_id != user_id:
        # 존재 여부를 숨기지 않는다(404) — 단 타 계정 프로필도 동일 404 (열거 방지)
        raise HTTPException(404, "unknown profile")
    return profile


@router.get("/me")
def me(user_id: int = Depends(require_app_user),
       db: Session = Depends(get_db)) -> dict:
    """앱 초기화 — 내 계정·프로필·기기·구독 상태."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "unknown user")
    profiles = db.execute(select(Profile).where(Profile.user_id == user_id)
                          .order_by(Profile.id)).scalars().all()
    devices = db.execute(select(Device).where(Device.owner_user_id == user_id)
                         .order_by(Device.id)).scalars().all()
    sub = db.execute(select(Subscription).where(Subscription.user_id == user_id)
                     .order_by(Subscription.id.desc())).scalars().first()
    return {"user_id": user.id, "email": user.email,
            "profiles": [{"id": p.id, "nickname": p.nickname,
                          "birth_date": str(p.birth_date) if p.birth_date else None}
                         for p in profiles],
            "devices": [{"id": d.id, "serial": d.serial,
                         "default_profile_id": d.default_profile_id}
                        for d in devices],
            "subscription": None if sub is None else
            {"product": sub.product, "state": sub.state,
             "expiry_at": str(sub.expiry_at) if sub.expiry_at else None}}


class AppProfileIn(BaseModel):
    nickname: str = Field(min_length=1, max_length=64)
    birth_date: date | None = None


@router.post("/profiles")
def create_profile(body: AppProfileIn,
                   user_id: int = Depends(require_app_user),
                   db: Session = Depends(get_db)) -> dict:
    """앱 온보딩에서 학생 프로필 생성 — 프로필의 정본 입력 경로(2026-07-07 결정:
    "프로필은 사용자가 앱에서 입력하고 서버는 연동만 한다").

    멱등: 같은 닉네임이 이미 있으면 그 프로필을 그대로 돌려준다 — 앱이 온보딩을
    다시 돌아도(재설치·상태 초기화) 중복 프로필이 쌓이지 않는다.
    """
    existing = db.execute(
        select(Profile).where(Profile.user_id == user_id,
                              Profile.nickname == body.nickname)).scalars().first()
    if existing is not None:
        if body.birth_date is not None and existing.birth_date != body.birth_date:
            existing.birth_date = body.birth_date  # 생년월일 정정은 허용
            db.commit()
        return {"id": existing.id, "nickname": existing.nickname,
                "birth_date": str(existing.birth_date) if existing.birth_date else None,
                "created": False}
    count = db.scalar(select(sa_func.count()).select_from(Profile)
                      .where(Profile.user_id == user_id))
    if count >= MAX_PROFILES_PER_USER:
        raise HTTPException(409, f"profile limit ({MAX_PROFILES_PER_USER}) reached")
    profile = Profile(user_id=user_id, nickname=body.nickname,
                      birth_date=body.birth_date)
    db.add(profile)
    db.flush()
    db.add(AuditLog(actor=f"app:user:{user_id}", action="profile_created",
                    target=f"profile:{profile.id}",
                    detail_json={"nickname": body.nickname}))
    db.commit()
    return {"id": profile.id, "nickname": profile.nickname,
            "birth_date": str(profile.birth_date) if profile.birth_date else None,
            "created": True}


@router.get("/profiles/{profile_id}/reports")
def list_reports(profile_id: int, offset: int = 0, limit: int = 20,
                 user_id: int = Depends(require_app_user),
                 db: Session = Depends(get_db)) -> dict:
    """프로필의 리포트 목록(시간순) — 채점 완료(promoted)된 세션만."""
    _own_profile(db, user_id, profile_id)
    base_where = (StudySession.profile_id == profile_id,)
    total = db.scalar(
        select(sa_func.count()).select_from(StudySession)
        .join(PromotedResult, PromotedResult.session_id == StudySession.id)
        .where(*base_where))
    rows = db.execute(
        select(StudySession, ScoringRun.sfi, ScoringRun.confidence, ParamSet.version)
        .join(PromotedResult, PromotedResult.session_id == StudySession.id)
        .join(ScoringRun, ScoringRun.id == PromotedResult.scoring_run_id)
        .outerjoin(ParamSet, ParamSet.id == ScoringRun.param_set_id)
        .where(*base_where)
        .order_by(StudySession.started_at.desc())
        .offset(max(offset, 0)).limit(min(limit, 100))).all()
    return {"total": total, "offset": offset,
            "reports": [{
                "sid": str(s.id),
                "display": s.started_at.strftime("%Y-%m-%d %H:%M") if s.started_at else None,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "mode": s.mode, "sfi": sfi, "confidence": confidence,
                "engine": engine, "qc_flags": _qc_flags(s, confidence),
            } for s, sfi, confidence, engine in rows]}


@router.get("/reports/{sid}")
def report_detail(sid: uuid.UUID, lang: str = Query("ko", pattern="^(ko|en|zh)$"),
                  user_id: int = Depends(require_app_user),
                  db: Session = Depends(get_db)) -> dict:
    """리포트 상세 — ops 콘솔과 동일한 정본 JSON.

    [lang] (2026-07-16): 앱 현재 언어(ko/en/zh) — 코칭 문구(coach_text)만 이 언어로
    내려간다. 점수·타임라인 등 정본 채점 값은 언어와 무관하게 항상 동일."""
    sess = db.get(StudySession, sid)
    if sess is None or sess.profile_id is None:
        raise HTTPException(404, "unknown session")
    _own_profile(db, user_id, sess.profile_id)
    return build_session_report(db, sid, lang=lang)
