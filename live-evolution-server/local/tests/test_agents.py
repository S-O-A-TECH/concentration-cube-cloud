"""E2 DoD: 어댑터 감지·인증 응답 파싱 (subprocess 는 monkeypatch — CLI 없이 검증)."""
from __future__ import annotations

import json

from app.agents import base as base_mod
from app.agents.claude_cli import ClaudeAdapter
from app.agents.codex_cli import CodexAdapter
from app.agents.base import extract_json_block


def test_extract_json_block_from_noisy_stdout():
    noisy = 'log line\n{"schema": "proposal.v1", "level": 1, "x": {"y": "z}"}}\ntrailer'
    out = extract_json_block(noisy)
    assert out["schema"] == "proposal.v1"
    assert out["x"]["y"] == "z}"          # 중괄호가 든 문자열도 균형 파서가 살아남는다
    assert extract_json_block("no json here") is None
    assert extract_json_block("{broken") is None


def _patch_run(monkeypatch, module, code, out, err="", error=""):
    monkeypatch.setattr(module, "run_cli",
                        lambda cmd, cwd, timeout, input_text=None: (code, out, err, 0.5, error))
    monkeypatch.setattr(module, "resolve_exe", lambda name: f"C:/fake/{name}.cmd")


def test_claude_detect_version_parse(env, monkeypatch):
    _patch_run(monkeypatch, base_mod, 0, "2.5.14 (Claude Code)")
    # detect 는 base 모듈의 run_cli/resolve_exe 를 쓴다
    d = ClaudeAdapter().detect()
    assert d.installed and d.version.startswith("2.5")


def test_claude_auth_ok_json(env, monkeypatch):
    import app.agents.claude_cli as mod
    payload = json.dumps({"type": "result", "subtype": "success",
                          "result": "OK", "total_cost_usd": 0.001})
    _patch_run(monkeypatch, mod, 0, payload)
    a = ClaudeAdapter().check_auth()
    assert a.ok and a.checked_at


def test_claude_auth_login_needed_hint(env, monkeypatch):
    import app.agents.claude_cli as mod
    _patch_run(monkeypatch, mod, 1, "", err="Invalid API key · Please run /login")
    a = ClaudeAdapter().check_auth()
    assert not a.ok
    assert "login" in a.detail.lower()


def test_codex_auth_ok(env, monkeypatch):
    import app.agents.codex_cli as mod
    _patch_run(monkeypatch, mod, 0, "OK\n")
    a = CodexAdapter().check_auth()
    assert a.ok


def test_propose_collects_file_over_stdout(env, monkeypatch, tmp_path):
    """파일이 정본, stdout 은 폴백 (E2 리스크)."""
    import app.agents.claude_cli as mod
    ws = tmp_path
    (ws / "output").mkdir()
    (ws / "MISSION.md").write_text("m", encoding="utf-8")
    file_prop = {"schema": "proposal.v1", "from": "file"}
    (ws / "output" / "proposal.json").write_text(json.dumps(file_prop), encoding="utf-8")
    stdout_prop = json.dumps({"result": "done", "total_cost_usd": 0.02})
    _patch_run(monkeypatch, mod, 0, stdout_prop)
    out = ClaudeAdapter().propose(ws, 60)
    assert out.ok and out.proposal_source == "file"
    assert out.proposal["from"] == "file"
    assert out.cost_usd == 0.02


def test_propose_stdout_fallback(env, monkeypatch, tmp_path):
    import app.agents.codex_cli as mod
    ws = tmp_path
    (ws / "output").mkdir()
    (ws / "MISSION.md").write_text("m", encoding="utf-8")
    _patch_run(monkeypatch, mod, 0,
               'preamble {"schema": "proposal.v1", "level": 1} done')
    out = CodexAdapter().propose(ws, 60)
    assert out.ok and out.proposal_source == "stdout"


def test_propose_no_output_is_failure(env, monkeypatch, tmp_path):
    import app.agents.claude_cli as mod
    (tmp_path / "output").mkdir()
    (tmp_path / "MISSION.md").write_text("m", encoding="utf-8")
    _patch_run(monkeypatch, mod, 0, "그냥 텍스트만")
    out = ClaudeAdapter().propose(tmp_path, 60)
    assert not out.ok
    assert "proposal.v1" in out.error
