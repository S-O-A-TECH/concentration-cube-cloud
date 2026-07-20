"""실시간 검증 세션 콘솔 (SPEC-05) — [시작]이 기기를 시작시키고, 클릭이 정답지를 만든다.

- 버튼 4종: focus / blank_stare / off_task / pause(중단·기타 — 정답지 공백)
- 클릭 시각 = 기기 세션 시계 (DeviceMonitor 의 t_sec 보간 — 벽시계 금지)
- 저장은 세션당 1회·불변 (재-POST 는 운영 서버가 409) — 실수는 [연구 제외] 후 재시도
- 동시 콘솔 세션 1개 (웹캠 프로토도 동시 세션 1개 — 409)
"""
from __future__ import annotations

import datetime as dt
import json
import threading

from . import db
from .config import get_config
from .device_client import DeviceClient, DeviceError, DeviceMonitor
from .server_client import OpsError, get_ops

BUTTONS = ("focus", "blank_stare", "off_task", "pause")
LABEL_METHOD = "realtime_instructed"

DEFAULT_PRESETS = [
    {"name": "기본5분", "steps": [
        {"label": "focus", "sec": 120, "instruction": "지금부터 집중해서 책 읽어!"},
        {"label": "blank_stare", "sec": 60, "instruction": "이제부터 멍때려~"},
        {"label": "focus", "sec": 60, "instruction": "다시 집중해서 읽어!"},
        {"label": "off_task", "sec": 60, "instruction": "이제 딴 데 보면서 딴짓해!"},
    ]},
]


def clicks_to_segments(clicks: list[dict], end_t: float) -> list[dict]:
    """클릭 이력 → 정답지 구간 배열 (순수 함수 — E3 §1.9 단위테스트 대상).

    클릭 = 새 상태 시작, 다음 클릭이 이전 상태를 닫는다. pause 는 구간을 닫기만
    한다(라벨 없는 공백 = 평가 제외). 길이 0 구간은 버린다.
    """
    segs: list[dict] = []
    open_label: str | None = None
    open_t = 0.0
    for c in sorted(clicks, key=lambda c: c["t"]):
        t = round(float(c["t"]), 1)
        if t > end_t:
            break
        if open_label is not None and t > open_t:
            segs.append({"t0": open_t, "t1": t, "label": open_label})
        open_label = None if c["button"] == "pause" else c["button"]
        open_t = t
    if open_label is not None and end_t > open_t:
        segs.append({"t0": open_t, "t1": round(float(end_t), 1), "label": open_label})
    return segs


def get_presets() -> list[dict]:
    return db.get_setting("protocol_presets", DEFAULT_PRESETS)


class ConsoleManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.device = DeviceClient()
        self.monitor: DeviceMonitor | None = None
        self.sid: str | None = None
        self.clicks: list[dict] = []
        self.preset: dict | None = None
        self.labeler: str = "admin"
        self.phase: str = "idle"          # idle|running|finished|saved|discarded
        self.sync_warning = False
        self.save_error: str | None = None

    # ------------------------------------------------------------- 제어

    def start(self, preset_name: str | None, labeler: str,
              skip_calibration: bool = False) -> dict:
        # 기기 HTTP(≤5s) 는 락 밖에서 — state() 폴링이 굳지 않게 (리뷰 MINOR 2).
        # phase 선점("starting")이 이중 시작을 막는다.
        with self._lock:
            if self.phase in ("running", "starting"):
                raise RuntimeError("이미 진행 중인 검증 세션이 있습니다.")
            self.phase = "starting"
        try:
            res = self.device.start(mode="LAB-5", skip_calibration=skip_calibration)
        except BaseException:
            with self._lock:
                self.phase = "idle"
            raise
        with self._lock:
            self.sid = res["sid"]
            self.clicks = []
            self.labeler = labeler or "admin"
            self.preset = next((p for p in get_presets() if p["name"] == preset_name), None)
            self.phase = "running"
            self.sync_warning = False
            self.save_error = None
            if self.monitor:
                self.monitor.stop()
            self.monitor = DeviceMonitor(self.device, self.sid)
            self.monitor.start()
            db.save_console_session(
                self.sid, started_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                mode=res.get("mode", "LAB-5"), labeler=self.labeler,
                protocol=(self.preset or {}).get("name", "자유 진행"),
                clicks_json="[]", status="running", sync_warning=0)
            return {"sid": self.sid, "duration_sec": res.get("duration_sec")}

    def calibrate(self, command: str) -> dict:
        with self._lock:
            self._require_active()
            sid = self.sid
        return self.device.calibrate(sid, command)   # 네트워크는 락 밖

    def mark(self, button: str) -> dict:
        """지시 순간의 마킹 — 진실은 언제나 이 클릭이다 (자동 마킹 없음)."""
        with self._lock:
            self._require_active()
            if button not in BUTTONS:
                raise RuntimeError(f"버튼은 {BUTTONS} 중 하나여야 합니다")
            st = (self.monitor.last_status or {}) if self.monitor else {}
            if st.get("state") != "recording":
                raise RuntimeError("기기가 측정 중이 아닙니다 (보정을 먼저 완료해 주세요)")
            t, warn = self.monitor.clock.now_t()
            if t is None:
                raise RuntimeError("기기 시계 동기화 전입니다 — 잠시 후 다시 시도해 주세요")
            if warn:
                self.sync_warning = True   # WS/폴링 공백 중 클릭 (E3 리스크 — 저장 전 확인)
            click = {"t": round(min(t, float(st.get("duration_sec") or t)), 1),
                     "button": button, "warn": warn}
            self.clicks.append(click)
            db.save_console_session(self.sid, clicks_json=json.dumps(self.clicks),
                                    sync_warning=1 if self.sync_warning else 0)
            return click

    def finish(self) -> dict:
        """[세션 종료] — 정상 조기 종료 포함. 여기까지 데이터로 기기가 채점·저장한다."""
        with self._lock:
            self._require_active()
            sid = self.sid
            self.phase = "finishing"
        try:
            self.device.stop(sid, abort=False)   # 네트워크는 락 밖
        except BaseException:
            with self._lock:
                self.phase = "running"
            raise
        with self._lock:
            self.phase = "finished"
            db.save_console_session(sid, status="finished")
            return self.summary()

    def discard(self, reason: str = "프로토콜 실패") -> dict:
        """[폐기하고 다시] / [연구 제외] — 실패 세션은 통째로 버리고 새로 한다 (SPEC-05 §1)."""
        with self._lock:
            if not self.sid:
                raise RuntimeError("진행 중인 세션이 없습니다")
            sid = self.sid
            prev_phase = self.phase
            self.phase = "discarding"
        excluded = False
        try:
            if prev_phase == "running":
                try:
                    self.device.stop(sid, abort=True)     # 저장 없이 폐기
                except DeviceError:
                    pass
            if prev_phase == "finished":
                # 기기에는 이미 저장됨 — 운영 서버(아카이브)에서 연구 제외 마킹 (삭제 아님)
                try:
                    get_ops().exclude(sid, reason=f"콘솔 폐기: {reason}")
                    excluded = True
                except OpsError:
                    pass  # 운영 서버 미연결이어도 콘솔은 초기화 (아카이브에서 나중에 제외 가능)
        finally:
            with self._lock:
                db.save_console_session(sid, status="discarded")
                self._reset_runtime()
        return {"sid": sid, "excluded": excluded}

    def save(self) -> dict:
        """정답지 저장 — 운영 서버 POST, 1회뿐 (불변). 성공 후 콘솔 초기화."""
        with self._lock:
            if self.phase != "finished" or not self.sid:
                raise RuntimeError("종료된 세션이 없습니다 — [세션 종료] 후 저장할 수 있어요")
            st = (self.monitor.last_status or {}) if self.monitor else {}
            if st.get("state") not in ("done", None):
                # 기기가 record/result 저장을 마쳐야 운영 서버가 세션을 안다 (S4 업로드 경로)
                raise RuntimeError(f"기기 저장이 아직 끝나지 않았습니다 (상태: {st.get('state')}) "
                                   "— 잠시 후 다시 눌러 주세요")
            end_t = float(st.get("t_sec") or st.get("duration_sec") or 300.0)
            segments = clicks_to_segments(self.clicks, end_t)
            if not segments:
                raise RuntimeError("정답지 구간이 없습니다 — 버튼 클릭 이력이 비어 있어요")
            row = db.get_console_session(self.sid) or {}
            if row.get("status") == "saved":
                raise RuntimeError("이미 저장된 세션입니다 (정답지는 세션당 1회·불변)")
            sid = self.sid
            labeler = self.labeler
            protocol = (self.preset or {}).get("name", "자유 진행")
            self.phase = "saving"      # 이중 저장 방지 — 운영 서버 POST 는 락 밖
        try:
            res = get_ops().labels_post(sid, labeler=labeler, method=LABEL_METHOD,
                                        protocol=protocol, segments=segments)
        except OpsError as e:
            with self._lock:
                self.phase = "finished"   # 재시도 가능하게 되돌림
                self.save_error = e.user_msg
            raise RuntimeError(e.user_msg)
        with self._lock:
            db.save_console_session(sid, status="saved",
                                    saved_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
            self.phase = "saved"
            self._reset_runtime()
        return {"sid": sid, "n_segments": len(segments), "ops": res}

    # ------------------------------------------------------------- 조회

    def summary(self) -> dict:
        st = (self.monitor.last_status or {}) if self.monitor else {}
        end_t = float(st.get("t_sec") or st.get("duration_sec") or 0.0)
        return {"sid": self.sid, "segments": clicks_to_segments(self.clicks, end_t),
                "clicks": self.clicks, "sync_warning": self.sync_warning}

    def state(self) -> dict:
        with self._lock:
            st = dict(self.monitor.last_status) if self.monitor else {}
            t, warn = (self.monitor.clock.now_t() if self.monitor else (None, True))
            # 기기 판정(live_state)은 기본 숨김 (SPEC-05 §5 — 피검자 노출·지시 타이밍
            # 끌림 방지). 디버그 토글이 켜진 경우에만 내려보낸다.
            if not db.get_setting("debug_show_live_state", False):
                st.pop("live_state", None)
            return {
                "phase": self.phase, "sid": self.sid, "device": st,
                "t_interp": t, "stale": warn,
                "clicks": self.clicks, "sync_warning": self.sync_warning,
                "labeler": self.labeler,
                "preset": self.preset,
                "monitor_error": self.monitor.error if self.monitor else None,
                "save_error": self.save_error,
            }

    # ------------------------------------------------------------- 내부

    def _require_active(self):
        if self.phase != "running" or not self.sid:
            raise RuntimeError("진행 중인 검증 세션이 없습니다 — [시작]을 먼저 눌러 주세요")

    def _reset_runtime(self):
        if self.monitor:
            self.monitor.stop()
        self.monitor = None
        self.sid = None
        self.clicks = []
        self.preset = None
        self.phase = "idle"
        self.sync_warning = False


_console: ConsoleManager | None = None


def get_console() -> ConsoleManager:
    global _console
    if _console is None:
        _console = ConsoleManager()
    return _console


def reset_console() -> None:
    """테스트 전용."""
    global _console
    if _console and _console.monitor:
        _console.monitor.stop()
    _console = None
