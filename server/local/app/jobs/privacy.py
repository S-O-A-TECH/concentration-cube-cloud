"""삭제권 집행 잡 — 프로필 단위 개인정보 완전 삭제 (S7 §1.2, SPEC-04).

동의 철회 = 이 경로. 프로필에 귀속된 세션의 parquet·scoring_runs·promoted_results·
labels 까지 일괄 삭제한다 (연구 아카이브 보존 예외는 동의 범위 내에서만 —
SPEC-01 §3.1: 철회 시에는 삭제 대상). 모든 집행은 audit 에 남는다.
"""
from __future__ import annotations

import shutil

from sqlalchemy import delete, select, update

from app.config import get_settings
from app.db.models import (AuditLog, Device, Label, Profile, PromotedResult,
                           ScoringRun, StudySession)
from app.db.session import make_session_factory
from app.services.storage import SessionStorage


def delete_profile_data(profile_id: int, _factory=None, _storage=None) -> dict:
    factory = _factory or make_session_factory()
    storage = _storage or SessionStorage(get_settings().storage_root)
    with factory() as db:
        profile = db.get(Profile, profile_id)
        if profile is None:
            return {"skipped": "profile not found (이미 삭제됨)"}

        sids = db.execute(
            select(StudySession.id).where(StudySession.profile_id == profile_id)
        ).scalars().all()

        for sid in sids:
            db.execute(delete(PromotedResult).where(PromotedResult.session_id == sid))
            db.execute(delete(ScoringRun).where(ScoringRun.session_id == sid))
            db.execute(delete(Label).where(Label.session_id == sid))
            db.execute(delete(StudySession).where(StudySession.id == sid))
            shutil.rmtree(storage.session_dir(str(sid)), ignore_errors=True)

        db.execute(update(Device)
                   .where(Device.default_profile_id == profile_id)
                   .values(default_profile_id=None))
        nickname = profile.nickname
        db.delete(profile)
        db.add(AuditLog(actor="privacy-job", action="profile_deleted",
                        target=f"profile:{profile_id}",
                        detail_json={"nickname": nickname,
                                     "sessions_deleted": len(sids)}))
        db.commit()
        return {"deleted": profile_id, "sessions_deleted": len(sids)}
