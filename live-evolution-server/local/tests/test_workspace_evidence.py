"""E2/E4 DoD: Evidence 7파일, holdout 물리적 부재, simulate 자립 실행, 개인정보 차단."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.loop.evidence import build_evidence
from app.loop.prompts import FORBIDDEN_IDENTIFIER_PATTERNS
from app.loop.workspace import create_workspace, verify_no_holdout
from app.server_client import OpsClient

EXPECTED_FILES = {"confusion.json", "mistakes.jsonl", "feature_stats.json",
                  "current_params.json", "targets_bounds.json", "gate_rules.json",
                  "history.json"}


@pytest.fixture
def evidence(env):
    return build_evidence(OpsClient())


def test_evidence_pack_complete(evidence):
    assert set(evidence["files"]) == EXPECTED_FILES
    conf = evidence["files"]["confusion.json"]
    assert conf["per_state"]["blank_stare"]["sens"] is not None
    # 시드 데이터의 의도된 실패 모드: blank_stare 미검출
    assert "blank_stare" in conf["observed_failure_modes"]
    assert conf["per_state"]["blank_stare"]["sens"] < 0.85
    tb = evidence["files"]["targets_bounds.json"]
    assert "blank_stare.stare_dispersion_th" in tb["allowed_keys"]
    assert len(evidence["train_lite"]) > 0


def test_evidence_uses_train_only(evidence):
    train_sids = set(evidence["train_lite"]["sid"].unique())
    holdout = set(evidence["meta"]["holdout_sids"])
    assert holdout, "테스트 데이터에 holdout 이 있어야 의미가 있다"
    assert not (train_sids & holdout)


def test_workspace_layout_and_holdout_absence(env, evidence):
    ws = create_workspace("genTEST", evidence)
    assert (ws / "MISSION.md").exists()
    assert (ws / "data" / "train_lite.parquet").exists()
    assert (ws / "tools" / "simulate.py").exists()
    assert (ws / "output").is_dir() and not list((ws / "output").iterdir())
    assert (ws / "manifest.json").exists()
    for f in EXPECTED_FILES:
        assert (ws / "input" / f).exists()
    # holdout sid 가 작업장 어느 텍스트에도 없다 (E2 DoD)
    verify_no_holdout(ws, evidence["meta"]["holdout_sids"])
    # 오염 주입 → 즉시 검출
    (ws / "input" / "bad.json").write_text(
        json.dumps({"leak": evidence["meta"]["holdout_sids"][0]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="holdout"):
        verify_no_holdout(ws, evidence["meta"]["holdout_sids"])


def test_simulate_standalone_runs(env, evidence):
    """작업장 simulate.py 는 우리 앱 없이 순수 파일로 돈다 — 에이전트의 연습장 실증."""
    ws = create_workspace("genSIM", evidence)
    cand = ws / "cand.json"
    params = json.loads((ws / "input" / "current_params.json").read_text(encoding="utf-8"))
    params["blank_stare"]["stare_dispersion_th"] = 0.055
    cand.write_text(json.dumps(params), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ws / "tools" / "simulate.py"), str(cand)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=ws)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert set(out["per_state"]) == {"focus", "off_task", "blank_stare"}
    # 임계 상향 → 놓친 멍때림 회수 (시드 데이터의 의도된 개선 방향)
    base = evidence["files"]["confusion.json"]["per_state"]["blank_stare"]["sens"]
    assert out["per_state"]["blank_stare"]["sens"] > base


def test_no_personal_identifiers_in_workspace(env, evidence):
    """SPEC-04 §3: 에이전트에 넘기는 것은 수치 요약뿐 — identifier 필드 차단 테스트."""
    ws = create_workspace("genPII", evidence)
    for p in ws.rglob("*"):
        if not p.is_file() or p.suffix == ".parquet":
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat in FORBIDDEN_IDENTIFIER_PATTERNS:
            assert pat not in text, f"{p.name} 에 금지 패턴: {pat}"


def test_mission_mentions_output_twice(env, evidence):
    """E2 리스크 대응: output/proposal.json 저장을 2회 이상 명시."""
    ws = create_workspace("genMSN", evidence)
    mission = (ws / "MISSION.md").read_text(encoding="utf-8")
    assert mission.count("output/proposal.json") >= 2
    assert "simulate.py" in mission
