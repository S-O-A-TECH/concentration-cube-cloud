"""live-evolution 서버 실행 — http://127.0.0.1:8200 (E0 §1.6).

이미 실행 중이면 안내만 하고 종료한다. 브라우저 자동 오픈.
"""
from __future__ import annotations

import argparse
import threading
import time
import webbrowser

import httpx
import uvicorn

HOST = "127.0.0.1"
PORT = 8200


def already_running() -> bool:
    try:
        r = httpx.get(f"http://{HOST}:{PORT}/api/health", timeout=1.5)
        return r.status_code == 200 and r.json().get("app") == "live-evolution"
    except httpx.HTTPError:
        return False


def open_browser_later(url: str, delay: float = 1.2):
    def _open():
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="live-evolution 서버 (127.0.0.1 전용)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    if args.port == PORT and already_running():
        print(f"이미 실행 중입니다 → http://{HOST}:{PORT}")
        if not args.no_browser:
            webbrowser.open(f"http://{HOST}:{PORT}")
        return

    url = f"http://{HOST}:{args.port}"
    print(f"live-evolution 서버 시작 → {url}  (종료: Ctrl+C)")
    if not args.no_browser:
        open_browser_later(url)
    # 바인딩은 127.0.0.1 전용 — LAN 노출 금지 (SPEC-04 §2)
    uvicorn.run("app.main:app", host=HOST, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
