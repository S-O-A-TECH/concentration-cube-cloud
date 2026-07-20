"""기기 제어 추상화 — 단일 경유 (SPEC-05 §2).

v0 구현 = 웹캠 프로토 로컬 API (http://127.0.0.1:8123) 직결.
향후 큐브/클라우드 시대에는 운영 서버 명령 채널(/v1/evolution/devices/*) 구현으로
교체된다 — 원칙 불변: "콘솔의 [시작]이 기기를 시작시킨다. 전송로만 바뀐다."

시간 동기화 (SPEC-05 §3 — 이 설계의 심장):
  기기 상태(t_sec)를 1Hz 로 수신하고, 라벨 클릭 시각은
  「최근 수신 t_sec + 수신 후 경과(단조시계)」로 보간한다. PC 벽시계는 쓰지 않는다.
  웹캠 WS 가 1Hz 로 밀어주는 payload 와 GET status 응답이 동일하므로 v0 전송로는
  0.5s 폴링을 쓴다 (인터페이스 뒤에 숨김 — WS 구현으로 교체 가능).
"""
from __future__ import annotations

import threading
import time

import httpx

from .config import get_config

SYNC_STALE_SEC = 3.0  # 이보다 오래된 t_sec 로 보간하면 '동기화 경고' (E3 리스크)


class DeviceError(Exception):
    def __init__(self, user_msg: str, status: int | None = None, detail: str = ""):
        super().__init__(user_msg)
        self.user_msg = user_msg
        self.status = status
        self.detail = detail


class DeviceClient:
    """웹캠 프로토 REST 래퍼."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or get_config().device_url).rstrip("/")

    def _req(self, method: str, path: str, json_body: dict | None = None,
             params: dict | None = None, timeout: float = 5.0):
        try:
            r = httpx.request(method, f"{self.base_url}{path}", json=json_body,
                              params=params, timeout=timeout)
        except httpx.HTTPError as e:
            raise DeviceError(f"Cannot connect to the device (webcam proto, {self.base_url}). "
                              "Check that the webcam proto (run.py) is running.", detail=str(e))
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise DeviceError(str(detail), status=r.status_code, detail=str(detail))
        return r.json()

    def health(self) -> dict:
        return self._req("GET", "/api/health", timeout=3.0)

    def start(self, mode: str = "LAB-5", skip_calibration: bool = False) -> dict:
        return self._req("POST", "/api/session/start",
                         {"mode": mode, "skip_calibration": skip_calibration,
                          "debug_preview": False})

    def calibrate(self, sid: str, command: str) -> dict:
        return self._req("POST", f"/api/session/{sid}/calibrate", {"command": command})

    def stop(self, sid: str, abort: bool = False) -> dict:
        return self._req("POST", f"/api/session/{sid}/stop", {}, params={"abort": abort})

    def status(self, sid: str) -> dict:
        return self._req("GET", f"/api/session/{sid}/status")


class SessionClock:
    """수신 t_sec + 단조시계 보간 (순수 로직 — 단위테스트 대상, E3 §1.3)."""

    def __init__(self):
        self._t_sec: float | None = None
        self._mono_at: float | None = None
        self._recording = False

    def feed(self, t_sec: float, recording: bool, mono_now: float | None = None):
        self._t_sec = float(t_sec)
        self._mono_at = time.monotonic() if mono_now is None else mono_now
        self._recording = recording

    def now_t(self, mono_now: float | None = None) -> tuple[float | None, bool]:
        """→ (보간된 세션 시각, 동기화 경고 여부). 수신 이력 없으면 (None, True)."""
        if self._t_sec is None:
            return None, True
        now = time.monotonic() if mono_now is None else mono_now
        age = now - self._mono_at
        t = self._t_sec + (age if self._recording else 0.0)
        return round(t, 1), age > SYNC_STALE_SEC


class DeviceMonitor:
    """세션 상태 0.5s 폴링 스레드 — 콘솔의 시계·상태 공급원."""

    POLL_SEC = 0.5

    def __init__(self, client: DeviceClient, sid: str):
        self.client = client
        self.sid = sid
        self.clock = SessionClock()
        self.last_status: dict = {}
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        terminal = ("done", "failed", "error", "aborted")
        while not self._stop.is_set():
            try:
                st = self.client.status(self.sid)
                self.last_status = st
                self.error = None
                self.clock.feed(float(st.get("t_sec") or 0.0),
                                st.get("state") == "recording")
                if st.get("state") in terminal:
                    break
            except DeviceError as e:
                self.error = e.user_msg
            self._stop.wait(self.POLL_SEC)
