"""param_set 조회 — 활성 세트 규칙 (SPEC-01 §2).

활성 = status 'adopted' 중 adopted_at 최신 1개. 새 세션 채점은 항상 활성 세트 사용.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ParamSet


def get_active_param_set(db: Session) -> ParamSet | None:
    return db.execute(
        select(ParamSet)
        .where(ParamSet.status == "adopted")
        .order_by(ParamSet.adopted_at.desc())
        .limit(1)
    ).scalar_one_or_none()
