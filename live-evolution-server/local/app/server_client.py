"""운영 서버 /v1/evolution/* 래퍼 — 전 호출이 이 파일 하나를 경유한다 (E1 §1.1).

계약 정본: server/docs/plan/spec/SPEC-02_API_계약.md §1.3.
운영 서버는 다른 팀이 Docker 로 병행 개발 중 (S5 전) — 그때까지는 mock
(tests/mocks/ops_server.py, run_mock_ops.py) 을 같은 계약으로 띄워 개발한다.
실서버 전환 = .env 의 SERVER_URL/EVOLUTION_TOKEN 교체뿐, 코드 변경 없음.

규율: 이 파일 밖에서 httpx 로 운영 서버를 직접 부르지 않는다 (E1 DoD — grep 검사).
"""
from __future__ import annotations

import httpx

from .config import get_config

TIMEOUT = 10.0


class OpsError(Exception):
    """사용자 문장으로 변환된 운영 서버 오류."""

    def __init__(self, user_msg: str, status: int | None = None, detail: str = ""):
        super().__init__(user_msg)
        self.user_msg = user_msg
        self.status = status
        self.detail = detail


class OpsClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        cfg = get_config()
        self.base_url = (base_url or cfg.server_url).rstrip("/")
        self.token = token if token is not None else cfg.evolution_token

    # ------------------------------------------------------------- 저수준

    def _req(self, method: str, path: str, *, json_body: dict | None = None,
             params: dict | None = None, timeout: float = TIMEOUT):
        url = f"{self.base_url}{path}"
        headers = {"X-Evolution-Token": self.token}
        try:
            r = httpx.request(method, url, json=json_body, params=params,
                              headers=headers, timeout=timeout)
        except httpx.ConnectError as e:
            raise OpsError(f"운영 서버({self.base_url})에 연결할 수 없습니다. "
                           "서버(또는 mock: python run_mock_ops.py)가 켜져 있는지 확인해 주세요.",
                           detail=str(e))
        except httpx.TimeoutException as e:
            raise OpsError("운영 서버 응답이 늦습니다 (10초 초과). 잠시 후 다시 시도해 주세요.",
                           detail=str(e))
        except httpx.HTTPError as e:
            # ReadError/RemoteProtocolError/ProxyError 등 — 전부 사용자 문장으로 (리뷰 MAJOR 2:
            # 이 계층이 새면 '미연결이어도 화면은 뜬다' 원칙이 500 으로 깨진다)
            raise OpsError(f"운영 서버 통신 오류: {type(e).__name__}. 잠시 후 다시 시도해 주세요.",
                           detail=str(e))
        if r.status_code in (401, 403):
            raise OpsError("EVOLUTION_TOKEN 이 유효하지 않습니다. "
                           ".env 의 토큰이 운영 서버와 같은 값인지 확인해 주세요.",
                           status=r.status_code, detail=r.text)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise OpsError(str(detail), status=r.status_code, detail=str(detail))
        return r.json()

    def _get(self, path: str, **params):
        return self._req("GET", path, params={k: v for k, v in params.items() if v is not None})

    def _post(self, path: str, body: dict | None = None, timeout: float = TIMEOUT):
        return self._req("POST", path, json_body=body or {}, timeout=timeout)

    # ------------------------------------------------------------- SPEC-02 §1.3

    def health(self) -> dict:
        return self._req("GET", "/health", timeout=3.0)

    def overview(self) -> dict:
        return self._get("/v1/evolution/overview")

    def sessions(self, labeled: bool | None = None, split: str | None = None,
                 mode: str | None = None, include_excluded: bool | None = None) -> list[dict]:
        return self._get("/v1/evolution/sessions", labeled=labeled, split=split,
                         mode=mode, include_excluded=include_excluded)["sessions"]

    def session_detail(self, sid: str) -> dict:
        return self._get(f"/v1/evolution/sessions/{sid}/detail")

    def labels_get(self, sid: str) -> dict:
        return self._get(f"/v1/evolution/sessions/{sid}/labels")

    def labels_post(self, sid: str, labeler: str, method: str, protocol: str,
                    segments: list[dict]) -> dict:
        return self._post(f"/v1/evolution/sessions/{sid}/labels",
                          {"labeler": labeler, "method": method,
                           "protocol": protocol, "segments": segments})

    def exclude(self, sid: str, reason: str) -> dict:
        return self._post(f"/v1/evolution/sessions/{sid}/exclude", {"reason": reason})

    def restore(self, sid: str, reason: str) -> dict:
        return self._post(f"/v1/evolution/sessions/{sid}/restore", {"reason": reason})

    def session_runs(self, sid: str) -> list[dict]:
        return self._get(f"/v1/evolution/sessions/{sid}/runs")["runs"]

    def mistakes(self, param_set_id: str | None = None) -> dict:
        return self._get("/v1/evolution/mistakes", param_set_id=param_set_id)

    def param_sets(self) -> list[dict]:
        return self._get("/v1/evolution/param_sets")["param_sets"]

    def param_sets_post(self, json_params: dict, origin: str, agent_name: str,
                        rationale: str, parent_id: str | None, version: str) -> dict:
        return self._post("/v1/evolution/param_sets",
                          {"json_params": json_params, "origin": origin,
                           "agent_name": agent_name, "rationale": rationale,
                           "parent_id": parent_id, "version": version})

    def evaluate(self, param_set_id: str) -> dict:
        # 재채점 잡 큐잉 — 결과는 jobs 폴링 후 report 로 (E4 §1.8). mock 은 동기 완료.
        return self._post(f"/v1/evolution/param_sets/{param_set_id}/evaluate",
                          timeout=60.0)

    def report(self, param_set_id: str) -> dict:
        return self._get(f"/v1/evolution/param_sets/{param_set_id}/report")

    def promote(self, param_set_id: str, confirm: bool) -> dict:
        return self._post(f"/v1/evolution/param_sets/{param_set_id}/promote",
                          {"confirm": confirm}, timeout=60.0)

    def reject(self, param_set_id: str, reason: str = "") -> dict:
        return self._post(f"/v1/evolution/param_sets/{param_set_id}/reject",
                          {"reason": reason})

    def rollback(self, param_set_id: str, reason: str = "") -> dict:
        return self._post(f"/v1/evolution/param_sets/{param_set_id}/rollback",
                          {"reason": reason})

    def jobs(self) -> list[dict]:
        return self._get("/v1/evolution/jobs")["jobs"]

    # ------------------------------------------------------------- 파생 헬퍼

    def active_param_set(self) -> dict | None:
        for ps in self.param_sets():
            if ps.get("active"):
                return ps
        return None


_client: OpsClient | None = None


def get_ops() -> OpsClient:
    global _client
    if _client is None:
        _client = OpsClient()
    return _client


def reset_ops() -> None:
    """테스트 전용."""
    global _client
    _client = None
