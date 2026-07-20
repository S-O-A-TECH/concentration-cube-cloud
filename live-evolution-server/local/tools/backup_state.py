"""state.sqlite 주간 백업 — 단순 파일 복사 (SPEC-04 §3, E6 §1.4).

사용:  .venv\\Scripts\\python tools\\backup_state.py
백업 위치: state/backups/state_YYYYMMDD_HHMMSS.sqlite (최근 12개 유지)
작업 스케줄러 등록 예 (주 1회):
  schtasks /Create /SC WEEKLY /TN lev_backup /TR "<venv python> <이 파일 절대경로>"
"""
from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_config  # noqa: E402

KEEP = 12


def main():
    cfg = get_config()
    src = cfg.db_path
    if not src.exists():
        print("state.sqlite 가 아직 없습니다 — 서버를 한 번 실행한 뒤 백업하세요.")
        return
    dst_dir = cfg.state_dir / "backups"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / f"state_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite"
    shutil.copy2(src, dst)
    backups = sorted(dst_dir.glob("state_*.sqlite"))
    for old in backups[:-KEEP]:
        old.unlink()
    print(f"백업 완료: {dst}  (보관 {min(len(backups), KEEP)}/{KEEP})")


if __name__ == "__main__":
    main()
