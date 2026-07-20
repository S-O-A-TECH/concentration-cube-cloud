"""가짜 에이전트 어댑터 — 상태 머신·검증 테스트용 (E2 §1.8 mock 에이전트).

FakeGoodAgent 는 '완벽한 에이전트' 를 흉내낸다: 작업장의 train_lite 를 실제
분류 코어로 자가 시험해 정직한 self_test 를 기입한다 — 4단계 재계산 대조를
통과하는 유일한 방법이 '정직' 임을 테스트가 증명하게 된다.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd

from app.agents.base import AgentAdapter, AgentOutput, AuthResult, DetectResult
from app.loop.sim import classify_core as core


class _FakeBase(AgentAdapter):
    name = "fake"
    display = "Fake Agent"

    def detect(self) -> DetectResult:
        return DetectResult(True, version="0.0-test", command="fake --version")

    def check_auth(self) -> AuthResult:
        return AuthResult(True, detail="OK", command="fake", checked_at=self.now())

    def _emit(self, workspace: Path, proposal: dict) -> AgentOutput:
        out = workspace / "output" / "proposal.json"
        out.write_text(json.dumps(proposal, ensure_ascii=False, indent=1), encoding="utf-8")
        return AgentOutput(True, proposal=proposal, proposal_source="file",
                           command="fake propose", stdout="fake run", duration_sec=0.1,
                           cost_usd=0.001)


def _build_honest_proposal(workspace: Path, key: str = "blank_stare.stare_dispersion_th",
                           value: float = 0.055) -> dict:
    cur = json.loads((workspace / "input" / "current_params.json").read_text(encoding="utf-8"))
    new_params = copy.deepcopy(cur)
    node = new_params
    parts = key.split(".")
    for p in parts[:-1]:
        node = node[p]
    old = node[parts[-1]]
    node[parts[-1]] = value
    df = pd.read_parquet(workspace / "data" / "train_lite.parquet")
    before = core.eval_train_lite(df, cur)["per_state"]
    after = core.eval_train_lite(df, new_params)["per_state"]
    fmt = lambda m: {st: {"sens": v["sens"], "spec": v["spec"]} for st, v in m.items()}
    return {
        "schema": "proposal.v1", "level": 1,
        "diagnosis": "blank_stare 미검출 — 놓친 bin 의 dispersion p50 이 현 임계(0.035)보다 높다",
        "new_params": new_params,
        "changes": [{"key": key, "from": old, "to": value,
                     "reason": "feature_stats: 놓친 멍때림 bin 의 dispersion 분포 근거"}],
        "self_test": {"n_simulate_runs": 2, "train_before": fmt(before),
                      "train_after": fmt(after)},
        "risk": "focus 특이도 소폭 저하 가능 — train 에서는 -2%p 이내",
        "rationale": "임계 상향으로 놓친 멍때림 bin 을 회수한다. 단일 키 변경.",
    }


class FakeGoodAgent(_FakeBase):
    """정직한 자가시험 — 검증 4단계 전부 통과해야 정상."""

    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        return self._emit(workspace, _build_honest_proposal(workspace))


class FakeForgedAgent(_FakeBase):
    """자가시험 수치 위조 — ★4단계 재계산 대조에서 기각되어야 한다 (E4 DoD)."""

    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        p = _build_honest_proposal(workspace)
        for st in p["self_test"]["train_after"].values():
            if st["sens"] is not None:
                # 정직값이 1.0 에 포화여도 반드시 ±0.2 어긋나게 (위조 검출 테스트의 요지)
                st["sens"] = round(st["sens"] - 0.2 if st["sens"] >= 0.5 else st["sens"] + 0.2, 4)
        return self._emit(workspace, p)


class FakeSchemaBadAgent(_FakeBase):
    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        return self._emit(workspace, {"schema": "proposal.v1", "level": 1,
                                      "new_params": {}, "changes": []})  # diagnosis 등 누락


class FakeOutOfTargetsAgent(_FakeBase):
    """관측 실패 모드와 무관한 파라미터 변경 — 3단계 기각."""

    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        p = _build_honest_proposal(workspace, key="drowsy.perclos_th", value=0.3)
        p["changes"][0]["reason"] = "무관 파라미터"
        return self._emit(workspace, p)


class FakeTimeoutAgent(_FakeBase):
    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        return AgentOutput(False, command="fake propose", error=f"타임아웃({int(timeout_sec)}s) 초과",
                           duration_sec=timeout_sec)
