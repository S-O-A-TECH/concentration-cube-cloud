"""Qwen API 어댑터 — httpx 를 monkeypatch 해 실 API·키 없이 검증
(test_agents.py 가 run_cli 를 갈아끼우는 것과 같은 패턴).

핵심 계약 검증:
  - self_test 는 LLM 이 준 수치가 아니라 서버가 simulate 로 재계산한 값이다.
  - 파일(output/proposal.json)이 정본이고, 그 제안이 validate 4단계를 통과한다.
  - 형식 오류·재시도(5xx)가 계약대로 처리된다.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import app.agents.qwen_api as qwen
from app.agents.qwen_api import QwenApiAdapter
from app.loop.bounds import build_targets_bounds
from app.loop.sim import classify_core as core
from app.loop.validate import validate_proposal
from tests.mocks.synth_sessions import generate_batch

_PARAMS = json.loads(
    (Path(__file__).parent / "mocks" / "params_v1.json").read_text(encoding="utf-8"))
_TB = build_targets_bounds(["blank_stare"])
_CHANGE_KEY = "blank_stare.stare_dispersion_th"


def _no_key(monkeypatch):
    """'키 없음' 상태를 만든다.

    환경변수를 지우는 것만으로는 부족하다 — 어댑터는 .env 파일도 폴백으로 읽으므로
    (_conf() 의 _load_env_file), 개발자 PC 에 실제 키가 든 .env 가 있으면 테스트가
    거짓 통과/실패한다. 파일 경로까지 함께 차단해야 격리가 성립한다.
    """
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("LEV_QWEN_API_KEY", raising=False)
    monkeypatch.delenv("LEV_DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(qwen, "_load_env_file", lambda _p: {})


# ------------------------------------------------------------------ 가짜 HTTP 응답

class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _chat_ok(content: str, usage: dict | None = None) -> _FakeResp:
    return _FakeResp(200, {"choices": [{"message": {"content": content}}],
                          "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20}})


def _llm_changes_json(to=0.055, extra: dict | None = None) -> str:
    """LLM 이 낸다고 가정하는 응답 — 일부러 거짓 self_test 를 끼워 넣어
    서버가 그것을 무시함을 증명한다."""
    body = {
        "diagnosis": "blank_stare 미검출 — dispersion 임계가 낮음",
        "changes": [{"key": _CHANGE_KEY, "to": to, "reason": "feature_stats 근거"}],
        "risk": "낮음", "rationale": "단일 키 상향",
        "self_test": {"train_after": {s: {"sens": 0.999, "spec": 0.999}
                                      for s in core.LABEL_STATES}},  # 조작값 — 무시돼야 한다
    }
    if extra:
        body.update(extra)
    return json.dumps(body, ensure_ascii=False)


# ------------------------------------------------------------------ 미니 작업장

@pytest.fixture(scope="module")
def ws(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("qws")
    (tmp / "data").mkdir()
    (tmp / "input").mkdir()
    (tmp / "output").mkdir()
    sess_dir = tmp_path_factory.mktemp("qsess")
    batch = generate_batch(sess_dir, n_train_min=4, n_holdout_min=0)
    frames = []
    for b in batch[:4]:
        df = pd.read_parquet(sess_dir / b["sid"] / "record.parquet",
                             columns=list(core.CLASSIFIER_COLUMNS))
        df["sid"] = b["sid"]
        t = df["t_ms"].to_numpy() / 1000.0
        truth = np.full(len(df), "", dtype=object)
        for seg in b["segments"]:
            truth[(t >= seg["t0"]) & (t < seg["t1"])] = seg["label"]
        df["truth"] = truth
        frames.append(df)
    pd.concat(frames, ignore_index=True).to_parquet(tmp / "data" / "train_lite.parquet", index=False)
    (tmp / "input" / "current_params.json").write_text(json.dumps(_PARAMS), encoding="utf-8")
    (tmp / "input" / "targets_bounds.json").write_text(json.dumps(_TB, ensure_ascii=False),
                                                       encoding="utf-8")
    (tmp / "MISSION.md").write_text("임무문 스텁 — 도메인 설명", encoding="utf-8")
    return tmp


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "test-key-123")
    monkeypatch.setattr(qwen.time, "sleep", lambda *a, **k: None)  # 재시도 대기 제거


def _fresh_output(ws: Path) -> Path:
    p = ws / "output" / "proposal.json"
    if p.exists():
        p.unlink()
    return p


# ------------------------------------------------------------------ 순수 함수

def test_parse_json_strips_markdown_fence():
    assert qwen._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert qwen._parse_json('앞말 {"schema": "x", "n": {"k": 2}} 뒷말')["schema"] == "x"
    assert qwen._parse_json("json 없음") is None


def test_coerce_changes_filters_and_clamps():
    cur_flat = qwen.flatten(_PARAMS)
    raw = [
        {"key": _CHANGE_KEY, "to": "0.9", "reason": "문자열이자 경계초과"},  # 문자열→float, 0.10 으로 클램프
        {"key": "drowsy.perclos_th", "to": 0.3},                          # allowed 밖 → 제거
        {"key": "blank_stare.min_sec", "to": 8.0},                        # no-op(현재값) → 제거
        {"key": "unknown.key", "to": 1},                                  # 미지 키 → 제거
    ]
    out = qwen._coerce_changes(raw, cur_flat, _TB)
    assert len(out) == 1
    assert out[0]["key"] == _CHANGE_KEY
    assert out[0]["to"] == 0.10                    # bounds max 로 클램프
    assert out[0]["from"] == cur_flat[_CHANGE_KEY]  # from 은 현재값으로 강제


def test_coerce_changes_caps_at_eight():
    cur_flat = qwen.flatten(_PARAMS)
    tb = build_targets_bounds(["blank_stare", "off_task", "fatigue"])
    raw = [{"key": k, "to": (v * 1.05 if isinstance(v, float) else v)} for k, v in cur_flat.items()
           if k in set(tb["allowed_keys"]) and isinstance(v, (int, float)) and not isinstance(v, bool)]
    out = qwen._coerce_changes(raw, cur_flat, tb)
    assert len(out) <= 8


# ------------------------------------------------------------------ detect / auth

def test_detect_no_key(monkeypatch):
    _no_key(monkeypatch)
    d = QwenApiAdapter().detect()
    assert not d.installed
    assert "키" in d.error


def test_detect_with_key(with_key):
    d = QwenApiAdapter().detect()
    assert d.installed and d.version  # version = 모델명


def test_check_auth_ok(with_key, monkeypatch):
    monkeypatch.setattr(qwen, "_post_once",
                        lambda url, headers, payload, timeout: _chat_ok("OK"))
    a = QwenApiAdapter().check_auth()
    assert a.ok and a.checked_at


def test_check_auth_401(with_key, monkeypatch):
    monkeypatch.setattr(qwen, "_post_once",
                        lambda url, headers, payload, timeout: _FakeResp(401, text="unauthorized"))
    a = QwenApiAdapter().check_auth()
    assert not a.ok and "인증" in a.detail


def test_check_auth_no_key(monkeypatch):
    _no_key(monkeypatch)
    a = QwenApiAdapter().check_auth()
    assert not a.ok and "QWEN_API_KEY" in a.detail


# ------------------------------------------------------------------ propose (핵심)

def test_propose_happy_path_server_computes_self_test(ws, with_key, monkeypatch):
    """행복 경로: 파일이 써지고, self_test 는 LLM 조작값이 아니라 재계산값이며,
    그 제안이 validate 4단계를 통과한다."""
    _fresh_output(ws)
    monkeypatch.setattr(qwen, "_post_once",
                        lambda url, headers, payload, timeout: _chat_ok(_llm_changes_json(to=0.055)))
    out = QwenApiAdapter().propose(ws, 60)
    assert out.ok, out.error
    assert out.proposal_source == "file"

    saved = json.loads((ws / "output" / "proposal.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "proposal.v1"
    assert saved["changes"][0]["key"] == _CHANGE_KEY
    assert saved["changes"][0]["from"] == _PARAMS["blank_stare"]["stare_dispersion_th"]

    # self_test 는 서버 재계산값 — LLM 이 우긴 0.999 가 아니다
    df = pd.read_parquet(ws / "data" / "train_lite.parquet")
    recompute = core.eval_train_lite(df, saved["new_params"])["per_state"]
    for st in core.LABEL_STATES:
        assert saved["self_test"]["train_after"][st]["sens"] == recompute[st]["sens"]
        assert saved["self_test"]["train_after"][st]["sens"] != 0.999

    # 그리고 실제 검증기를 4단계까지 통과한다
    vr = validate_proposal(saved, _PARAMS, _TB, [], workspace=ws, do_recompute=True)
    assert vr.ok, vr.errors
    assert vr.stage == 4


def test_propose_malformed_json_fails_gracefully(ws, with_key, monkeypatch):
    """형식 오류 → 실패 반환, proposal.json 미생성."""
    out_path = _fresh_output(ws)
    monkeypatch.setattr(qwen, "_post_once",
                        lambda url, headers, payload, timeout: _chat_ok("죄송하지만 JSON 을 못 만들었습니다"))
    out = QwenApiAdapter().propose(ws, 60)
    assert not out.ok
    assert "JSON" in out.error
    assert not out_path.exists()


def test_propose_no_valid_changes_fails(ws, with_key, monkeypatch):
    """allowed 밖·no-op 뿐이면 유효 변경 0 → 실패, 파일 없음."""
    out_path = _fresh_output(ws)
    bad = json.dumps({"diagnosis": "x", "risk": "y", "rationale": "z",
                      "changes": [{"key": "drowsy.perclos_th", "to": 0.3}]})  # allowed 밖
    monkeypatch.setattr(qwen, "_post_once",
                        lambda url, headers, payload, timeout: _chat_ok(bad))
    out = QwenApiAdapter().propose(ws, 60)
    assert not out.ok and "변경" in out.error
    assert not out_path.exists()


def test_propose_retries_on_500(ws, with_key, monkeypatch):
    """첫 호출 500 → 재시도 → 성공. 호출 2회."""
    _fresh_output(ws)
    calls = {"n": 0}

    def flaky(url, headers, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp(500, text="internal error")
        return _chat_ok(_llm_changes_json(to=0.052))

    monkeypatch.setattr(qwen, "_post_once", flaky)
    out = QwenApiAdapter().propose(ws, 60)
    assert out.ok, out.error
    assert calls["n"] == 2
    assert (ws / "output" / "proposal.json").exists()


def test_propose_no_key(ws, monkeypatch):
    _no_key(monkeypatch)
    out = QwenApiAdapter().propose(ws, 60)
    assert not out.ok and "QWEN_API_KEY" in out.error
