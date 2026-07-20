"""결제 웹훅 스텁 — /v1/billing/webhook (2026-07-07 결정: "결제 연동 준비까지").

PG(결제사)가 미정이므로 **일반형 이벤트 계약**으로 자리를 만든다. 실 PG 확정 시
이 모듈의 ① 서명 검증(X-Billing-Secret → PG 서명 방식)과 ② 이벤트 매핑만 교체하면
되고, 구독 상태 모델(app/db/models.py Subscription)과 ops 콘솔은 그대로 쓴다.

인증: X-Billing-Secret 헤더 = 설정 billing_webhook_secret (상수 시간 비교).
ops 쿠키/기기 JWT/evolution 토큰과 **완전 분리**된 네 번째 자격증명 축.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.ops import SUB_STATES
from app.config import get_settings
from app.db.models import AuditLog, Subscription, User

router = APIRouter(prefix="/v1/billing", tags=["billing"])

# PG 이벤트 → 구독 상태 매핑 (일반형 — PG 확정 시 실제 이벤트명으로 교체)
EVENT_STATE = {
    "subscription_activated": "active",
    "subscription_renewed": "active",
    "subscription_trial_started": "trial",
    "subscription_expired": "expired",
    "subscription_canceled": "canceled",
}


def require_billing_secret(x_billing_secret: str = Header(default="")) -> None:
    expected = get_settings().billing_webhook_secret
    if not secrets.compare_digest(x_billing_secret, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid billing secret")


class WebhookIn(BaseModel):
    provider: str = Field(default="stub", max_length=32)
    event_type: str
    user_id: int
    product: str = Field(default="focus_report", max_length=64)
    months: int = Field(default=12, ge=1, le=24)   # activated/renewed 시 연장 개월
    # 구매 국가 — Google Play 검증(purchases.subscriptionsv2)의 regionCode 를
    # 그대로 넘긴다(ISO 3166-1 alpha-2). 대시보드 국가별 집계의 원천.
    country: str | None = Field(default=None, min_length=2, max_length=2)


@router.post("/webhook", dependencies=[Depends(require_billing_secret)])
def billing_webhook(body: WebhookIn, db: Session = Depends(get_db)) -> dict:
    state = EVENT_STATE.get(body.event_type)
    if state is None:
        raise HTTPException(422, f"unknown event_type (expected one of {list(EVENT_STATE)})")
    assert state in SUB_STATES
    if db.get(User, body.user_id) is None:
        raise HTTPException(404, "unknown user")

    now = datetime.now(timezone.utc)
    # (user, product) 최신 구독 upsert — 없으면 생성, 있으면 상태 갱신
    sub = db.execute(
        select(Subscription)
        .where(Subscription.user_id == body.user_id,
               Subscription.product == body.product)
        .order_by(Subscription.id.desc())).scalars().first()
    if sub is None:
        sub = Subscription(user_id=body.user_id, product=body.product,
                           source=body.provider, start_at=now)
        db.add(sub)
    sub.state = state
    sub.source = body.provider
    sub.last_verified_at = now
    if body.country:
        sub.country = body.country.upper()
    if state in ("active", "trial"):
        sub.months = body.months
    if state in ("active", "trial"):
        prev = sub.expiry_at
        if prev is not None and prev.tzinfo is None:  # sqlite 는 naive 로 돌려준다
            prev = prev.replace(tzinfo=timezone.utc)
        base_dt = prev if (prev and prev > now) else now
        sub.expiry_at = base_dt + timedelta(days=30 * body.months)
    db.flush()
    db.add(AuditLog(actor=f"billing:{body.provider}",
                    action=f"webhook_{body.event_type}",
                    target=f"user:{body.user_id}",
                    detail_json={"product": body.product, "state": state,
                                 "sub_id": sub.id}))
    db.commit()
    return {"ok": True, "subscription_id": sub.id, "state": sub.state,
            "expiry_at": str(sub.expiry_at) if sub.expiry_at else None}
