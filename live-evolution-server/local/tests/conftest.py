"""pytest 공용 fixture.

- mock_server: uvicorn 스레드 1개(세션 전역) + 내부 ASGI 앱 교체(reset)로 테스트 격리
- demo_data: 합성 LAB-5 세션 배치 (세션 전역 1회 생성 — 결정적)
- env: 테스트별 격리 환경 (LEV_STATE_DIR 임시화 + 싱글턴 리셋)
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from tests.mocks.ops_server import MockState, create_app
from tests.mocks.synth_sessions import generate_batch

TEST_TOKEN = "test-token"


class _SwapApp:
    """uvicorn 은 계속 돌리고 내부 앱만 바꿔 낀다 — 테스트당 fresh mock."""

    def __init__(self):
        self.inner = None

    async def __call__(self, scope, receive, send):
        if self.inner is None:
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send({"type": "http.response.body", "body": b"no inner app"})
            return
        await self.inner(scope, receive, send)


class MockServer:
    def __init__(self, swap: _SwapApp, port: int, demo_dir: Path, batch: list[dict]):
        self.swap = swap
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.demo_dir = demo_dir
        self.batch = batch
        self._n = 0

    def reset(self, tmp: Path, labeled_count: int | None = None) -> Path:
        """fresh 상태로 mock 재구성. labeled_count=None → 전부 라벨."""
        self._n += 1
        state_path = tmp / f"mock_state_{self._n}.json"
        seed_params = json.loads(
            (Path(__file__).parent / "mocks" / "params_v1.json").read_text(encoding="utf-8"))
        st = MockState(state_path, seed_params)
        items = self.batch if labeled_count is None else self.batch[:labeled_count]
        for b in items:
            st.d["labels"][b["sid"]] = {
                "labeler": "admin", "method": "realtime_instructed",
                "protocol": b["protocol"], "segments": b["segments"],
                "saved_at": "2026-07-01T10:00:00+09:00",
            }
        st.save()
        self.swap.inner = create_app([self.demo_dir], state_path, token=TEST_TOKEN,
                                     webcam_root=None)  # 테스트에서 SFI 채점은 불필요 (속도)
        return state_path


@pytest.fixture(scope="session")
def demo_data(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo_sessions")
    batch = generate_batch(d, n_train_min=10, n_holdout_min=3)
    return d, batch


@pytest.fixture(scope="session")
def mock_server(demo_data):
    demo_dir, batch = demo_data
    swap = _SwapApp()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(swap, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    t0 = time.monotonic()
    while not server.started:
        if time.monotonic() - t0 > 10:
            raise RuntimeError("mock uvicorn 기동 실패")
        time.sleep(0.05)
    yield MockServer(swap, port, demo_dir, batch)
    server.should_exit = True


@pytest.fixture
def env(mock_server, tmp_path, monkeypatch):
    """테스트별 격리: 상태 디렉토리·싱글턴·mock 상태 전부 리셋."""
    from app import auth, config, db
    from app import console as console_mod
    from app import server_client
    from app.loop import machine

    mock_server.reset(tmp_path)
    monkeypatch.setenv("LEV_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LEV_SERVER_URL", mock_server.base_url)
    monkeypatch.setenv("LEV_EVOLUTION_TOKEN", TEST_TOKEN)
    db.close()
    config.reset_config()
    server_client.reset_ops()
    machine.reset_loop()
    console_mod.reset_console()
    auth.reset_lock()
    from app import main as main_mod
    main_mod._ops_status_cache.invalidate()
    main_mod._device_status_cache.invalidate()
    main_mod._agent_cache.clear()
    yield mock_server
    db.close()
    config.reset_config()
    server_client.reset_ops()
    machine.reset_loop()
    console_mod.reset_console()


@pytest.fixture
def client(env):
    """인증 쿠키가 셋된 FastAPI TestClient."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/login", json={"id": "admin", "pw": "1234"})
        assert r.status_code == 200
        yield c
