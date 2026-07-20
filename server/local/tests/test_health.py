"""S0 DoD: /health 4항목 + ok 판정 논리 (S0 구현계획 §1.7).

DB/redis 컨테이너 없이도 돌아야 하므로 연결 체크는 monkeypatch 로 대체하고,
storage 체크만 실제 파일시스템(tmp_path)으로 검증한다.
"""
from fastapi.testclient import TestClient

from app.api import health as health_module
from app.main import app

client = TestClient(app)


def _patch_checks(monkeypatch, db: bool, redis_ok: bool, storage: bool) -> None:
    monkeypatch.setattr(health_module, "check_db", lambda url: db)
    monkeypatch.setattr(health_module, "check_redis", lambda url: redis_ok)
    monkeypatch.setattr(health_module, "check_storage", lambda root: storage)


def test_health_all_green(monkeypatch):
    _patch_checks(monkeypatch, db=True, redis_ok=True, storage=True)
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"ok", "db", "redis", "storage", "version"}
    assert body["ok"] is True
    assert body["db"] is True and body["redis"] is True and body["storage"] is True
    assert isinstance(body["version"], str) and body["version"]


def test_health_not_ok_when_db_down(monkeypatch):
    _patch_checks(monkeypatch, db=False, redis_ok=True, storage=True)
    body = client.get("/health").json()
    assert body["ok"] is False
    assert body["db"] is False


def test_check_storage_writes_and_cleans_probe(tmp_path):
    root = tmp_path / "storage"
    assert health_module.check_storage(str(root)) is True
    assert root.exists()
    assert list(root.iterdir()) == []  # probe 파일은 지워져야 한다


def test_check_storage_false_on_unwritable_path():
    # Windows 에서 파일명으로 쓸 수 없는 문자를 포함한 경로 → False (예외가 새면 안 됨)
    assert health_module.check_storage("Z:\\__no_such_drive__\\storage") is False
