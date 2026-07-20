"""mock 운영 서버 실행 — 실서버(다른 팀, Docker) S5 완성 전까지의 로컬 대역.

기본 포트 8100 (실서버와 동일 — 실서버가 8100 을 차지하면 이 mock 은 끄거나
--port 8101 로 옮기고 .env SERVER_URL 을 맞춘다).

세션 소스 (여러 개 허용, 기본):
  1) tests/mocks/demo_sessions/   ← tools/seed_demo_data.py 가 만드는 합성 세션
  2) <WEBCAM_ROOT>/data/sessions/ ← 실제 웹캠 프로토 세션 (콘솔 리허설용)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import get_config  # noqa: E402
from tests.mocks.ops_server import create_app  # noqa: E402


def main():
    cfg = get_config()
    ap = argparse.ArgumentParser(description="mock 운영 서버 (/v1/evolution/*)")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--sessions-dir", action="append", default=[],
                    help="세션 폴더 (반복 지정 가능)")
    ap.add_argument("--state", default=str(ROOT / "tests" / "mocks" / "mock_state.json"))
    ap.add_argument("--token", default=os.environ.get("EVOLUTION_TOKEN", cfg.evolution_token))
    args = ap.parse_args()

    dirs = [Path(d) for d in args.sessions_dir]
    if not dirs:
        # 아직 없는 폴더도 포함 — SessionStore 가 매 스캔마다 존재 여부를 확인하므로
        # 나중에 시드/세션이 생기면 재시작 없이 바로 보인다.
        dirs = [ROOT / "tests" / "mocks" / "demo_sessions",
                cfg.webcam_root / "data" / "sessions"]

    # 실서버(다른 팀 Docker)가 이미 8100 을 쓰는 경우가 흔하다 — 친절히 안내
    import socket
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"[!] 포트 {args.port} 이 이미 사용 중입니다 (운영 서버 Docker 일 가능성).")
            print(f"    mock 은 다른 포트로 여세요:  python run_mock_ops.py --port 8101")
            print(f"    그리고 .env 의 SERVER_URL 을 http://127.0.0.1:8101 로 맞추세요.")
            sys.exit(1)

    print(f"mock 운영 서버 → http://127.0.0.1:{args.port}")
    print(f"  세션 소스: {[str(d) for d in dirs]}")
    print(f"  상태 파일: {args.state}")
    print("  ※ 실서버(S5) 가동 후에는 이 mock 을 끄고 .env SERVER_URL 만 바꾸면 됩니다.")
    app = create_app(dirs, Path(args.state), token=args.token,
                     webcam_root=cfg.webcam_root)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
