"""정리 잡 — 방치 세션·임시 chunk 청소 (S7 §1.3).

- open 상태 24h 초과 세션 → upload_state=failed + audit (업로드 중단 방치 대응)
- failed 처리된 세션의 chunks/ 임시 파일 삭제
- (LLM payload 는 저장하지 않으므로 삭제 대상 없음 — 수치 요약만 즉시 전송)

실행: 컨테이너에서 `python -m app.jobs.maintenance --loop 3600` (compose scheduler 서비스)
      또는 1회성 `python -m app.jobs.maintenance`
"""
from __future__ import annotations

import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import AuditLog, StudySession
from app.db.session import make_session_factory
from app.services.storage import SessionStorage

STALE_OPEN_HOURS = 24


def cleanup_stale_sessions(_factory=None, _storage=None) -> dict:
    factory = _factory or make_session_factory()
    storage = _storage or SessionStorage(get_settings().storage_root)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_OPEN_HOURS)
    with factory() as db:
        stale = db.execute(
            select(StudySession).where(StudySession.upload_state == "open",
                                       StudySession.started_at < cutoff)
        ).scalars().all()
        for sess in stale:
            sess.upload_state = "failed"
            shutil.rmtree(storage.chunks_dir(str(sess.id)), ignore_errors=True)
            db.add(AuditLog(actor="maintenance", action="stale_session_failed",
                            target=str(sess.id),
                            detail_json={"open_hours": STALE_OPEN_HOURS}))
        db.commit()

        # complete 세션에 잔존한 chunks/ 청소 — finish 커밋과 정리 사이 크래시 잔존물 (R3)
        orphan_chunks = 0
        sessions_root = storage.root / "sessions"
        if sessions_root.exists():
            for cdir in sessions_root.glob("*/chunks"):
                sid = cdir.parent.name
                try:
                    sess = db.get(StudySession, uuid.UUID(sid))
                except ValueError:
                    continue
                if sess is not None and sess.upload_state == "complete":
                    shutil.rmtree(cdir, ignore_errors=True)
                    orphan_chunks += 1
        return {"stale_failed": len(stale), "orphan_chunks_cleaned": orphan_chunks}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SEC",
                    help="주기 실행 (compose scheduler 서비스용)")
    args = ap.parse_args()
    while True:
        out = cleanup_stale_sessions()
        print(f"maintenance: {out}", flush=True)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
