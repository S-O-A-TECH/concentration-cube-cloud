"""sync_focus_scoring.py — focus_scoring 정본(서버) → 웹캠 프로토 단방향 동기화 (SPEC-03 §1).

S3 부터 정본은 server/local/app/focus_scoring. 웹캠 프로토는 이 스크립트로 복사해 쓴다.
역방향(프로토→서버) 수정 금지 — 프로토 쪽이 다르면 이 스크립트가 경고한다.

실행 (server/local 에서):
  .venv\\Scripts\\python tools\\sync_focus_scoring.py            # 해시 대조만 (drift 검사)
  .venv\\Scripts\\python tools\\sync_focus_scoring.py --push     # 서버 → 프로토 복사
"""
import argparse
import hashlib
import shutil
import sys
from pathlib import Path

SERVER_PKG = Path(__file__).resolve().parents[1] / "app" / "focus_scoring"
PROTO_PKG = (Path(__file__).resolve().parents[3]
             / "web_cam_version_prototype" / "p0_webcam" / "focus_scoring")

FILES = ["__init__.py", "records.py", "qc.py", "events.py", "rhythm.py",
         "sfi.py", "report.py", "coach_templates.py", "params/default.json"]


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "(missing)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="서버 정본을 프로토로 복사")
    args = ap.parse_args()

    drift = []
    for rel in FILES:
        s, p = SERVER_PKG / rel, PROTO_PKG / rel
        hs, hp = file_hash(s), file_hash(p)
        mark = "OK " if hs == hp else "DRIFT"
        if hs != hp:
            drift.append(rel)
        print(f"[{mark}] {rel}")

    if not drift:
        print("\n동기화 상태 양호 — 서버와 프로토가 동일합니다.")
        return 0

    if args.push:
        for rel in drift:
            dst = PROTO_PKG / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SERVER_PKG / rel, dst)
            print(f"[PUSH] {rel} → 프로토")
        print(f"\n{len(drift)}개 파일 동기화 완료.")
        return 0

    print(f"\n경고: {len(drift)}개 파일 불일치 — 정본은 서버입니다. "
          f"반영하려면 --push (프로토 쪽 수정은 서버에 먼저 넣을 것).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
