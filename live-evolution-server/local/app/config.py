"""정적 설정 — .env 로딩 (SPEC-00 §3).

.env 는 서버 주소·토큰·계정 등 재시작이 필요한 값만 담는다.
런타임에 바뀌는 값(기본 에이전트, 타임아웃, 프리셋 등)은 db.settings 가 우선한다.
환경변수가 .env 파일보다 우선한다 (테스트 격리용 — LEV_STATE_DIR 등).
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from . import ROOT_DIR

_DEFAULTS = {
    "SERVER_URL": "http://127.0.0.1:8100",
    "EVOLUTION_TOKEN": "dev-evolution-token",
    "ADMIN_ID": "admin",
    "ADMIN_PW": "1234",
    "DEVICE_URL": "http://127.0.0.1:8123",
    "AGENT_DEFAULT": "claude",
    "AGENT_TIMEOUT_SEC": "600",
    "DAILY_RUN_LIMIT": "20",
    "TRIGGER_N": "5",
    "WEBCAM_ROOT": "../../web_cam_version_prototype",
    "SECRET_KEY": "",
}


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


class Config:
    def __init__(self, root: Path | None = None):
        self.root = root or ROOT_DIR
        file_vals = _load_env_file(self.root / ".env")

        def get(key: str) -> str:
            return os.environ.get(f"LEV_{key}", os.environ.get(key, file_vals.get(key, _DEFAULTS[key])))

        self.server_url = get("SERVER_URL").rstrip("/")
        self.evolution_token = get("EVOLUTION_TOKEN")
        self.admin_id = get("ADMIN_ID")
        self.admin_pw = get("ADMIN_PW")
        self.device_url = get("DEVICE_URL").rstrip("/")
        self.agent_default = get("AGENT_DEFAULT")
        self.agent_timeout_sec = int(get("AGENT_TIMEOUT_SEC"))
        self.daily_run_limit = int(get("DAILY_RUN_LIMIT"))
        self.trigger_n = int(get("TRIGGER_N"))

        webcam = Path(get("WEBCAM_ROOT"))
        if not webcam.is_absolute():
            webcam = (self.root / webcam).resolve()
        self.webcam_root = webcam

        # 런타임 데이터 (git 제외): sqlite·작업장·백업. 테스트는 LEV_STATE_DIR 로 격리.
        self.state_dir = Path(os.environ.get("LEV_STATE_DIR", self.root / "state"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "state.sqlite"
        self.workspaces_dir = self.state_dir / "workspaces"
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

        self.secret_key = get("SECRET_KEY") or self._persistent_secret()
        # 에이전트 작업장의 simulate.py 를 실행할 인터프리터 = 이 서버의 venv (SPEC-06 §3
        # "표준 venv 재사용") — MISSION.md 에 절대경로로 렌더된다.
        self.python_exe = sys.executable

    def _persistent_secret(self) -> str:
        p = self.state_dir / "secret.key"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        key = secrets.token_hex(32)
        p.write_text(key, encoding="utf-8")
        return key


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """테스트 전용 — 환경변수 변경 후 재로딩."""
    global _config
    _config = None
