"""트리거 3종 (SPEC-02 §2) — 수동 / 라벨 누적 알림 / 자동 제안(opt-in).

어느 트리거든 ADOPTED 로 가는 문은 [채택] 버튼 하나뿐이다 — 자동 제안 모드도
PASSED/REJECTED 에서 반드시 정지한다 (machine.py 가 보장, E6 테스트로 증명).
"""
from __future__ import annotations

import datetime as dt
import threading

from . import db
from .config import get_config
from .loop.machine import get_loop
from .server_client import OpsError, get_ops

CHECK_INTERVAL_SEC = 300  # 주기 폴링 5분 (E6 §1.1)


def labels_at_last_generation() -> int:
    return int(db.get_setting("labels_at_last_gen", 0))


def record_generation_baseline(labeled_now: int) -> None:
    db.set_setting("labels_at_last_gen", int(labeled_now))


def decide_trigger(labeled_now: int, labels_at_last_gen: int, n: int) -> bool:
    """순수 판단 함수 (E6 pytest 대상): 마지막 세대 이후 새 라벨 세션 ≥N ?"""
    return (labeled_now - labels_at_last_gen) >= n


class TriggerService:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.notice: dict | None = None   # {"kind": "proposal_ready", ...}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def check_once(self) -> dict | None:
        """1회 검사 — 대시보드 새로고침에서도 재사용."""
        try:
            ov = get_ops().overview()
        except OpsError:
            return self.notice
        labeled = int(ov.get("labeled_realtime", 0))
        n = int(db.get_setting("trigger_n", get_config().trigger_n))
        if decide_trigger(labeled, labels_at_last_generation(), n):
            self.notice = {
                "kind": "proposal_ready",
                "msg": f"새 라벨 세션 {labeled - labels_at_last_generation()}개 누적 — 제안 준비됨",
                "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            loop = get_loop()
            if db.get_setting("auto_propose", False) and loop.state == "IDLE":
                ok, _ = loop.can_run()
                if ok:
                    record_generation_baseline(labeled)
                    loop.run(auto=True)   # PASSED 에서 정지 — 채택은 언제나 수동
                    self.notice = {"kind": "auto_started",
                                   "msg": "자동 제안 모드 — 세대 실행을 시작했습니다 (채택은 수동)",
                                   "at": self.notice["at"]}
        else:
            self.notice = None
        return self.notice

    def clear(self):
        self.notice = None

    def _run(self):
        while not self._stop.wait(CHECK_INTERVAL_SEC):
            try:
                self.check_once()
            except Exception:
                pass  # 백그라운드 폴링은 앱을 죽이지 않는다


_service: TriggerService | None = None


def get_triggers() -> TriggerService:
    global _service
    if _service is None:
        _service = TriggerService()
    return _service
