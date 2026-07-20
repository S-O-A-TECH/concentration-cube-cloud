"""E0 DoD: 로그인 성공/실패/잠금 + 페이지·API 인증 가드."""
from fastapi.testclient import TestClient


def _client(env):
    from app.main import app
    return TestClient(app)


def test_login_success_and_logout(env):
    with _client(env) as c:
        r = c.post("/api/login", json={"id": "admin", "pw": "1234"})
        assert r.status_code == 200
        assert c.cookies.get("lev_session")
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/settings").status_code == 200
        r = c.post("/api/logout")
        assert r.status_code == 200


def test_login_failure_message(env):
    with _client(env) as c:
        r = c.post("/api/login", json={"id": "admin", "pw": "wrong"})
        assert r.status_code == 401
        assert "실패 1/5" in r.json()["detail"]


def test_lockout_after_5_failures(env):
    with _client(env) as c:
        for _ in range(4):
            assert c.post("/api/login", json={"id": "admin", "pw": "x"}).status_code == 401
        r = c.post("/api/login", json={"id": "admin", "pw": "x"})
        assert "잠급니다" in r.json()["detail"]
        # 잠금 중에는 올바른 비밀번호도 거부
        r = c.post("/api/login", json={"id": "admin", "pw": "1234"})
        assert r.status_code == 401
        assert "잠겨" in r.json()["detail"]


def test_api_requires_auth_and_pages_redirect(env):
    with _client(env) as c:
        assert c.get("/api/dashboard").status_code == 401
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"
        assert c.get("/login").status_code == 200
