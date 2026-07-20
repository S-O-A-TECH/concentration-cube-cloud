# -*- coding: utf-8 -*-
"""Qwen API 어댑터 (SPEC-01 확장) — CLI subprocess 대신 Alibaba Cloud Model Studio
OpenAI 호환 엔드포인트로 파라미터 제안을 받는다.

claude/codex 와 결정적으로 다른 두 가지 (의도된 단순화):

  1) LLM 에게 파일·도구 접근을 주지 않는다. 증거(input/*)를 프롬프트에 인라인으로 담아
     한 번(회귀 시 최대 두 번) 호출하고, JSON 응답만 받는다. tools/simulate.py 를
     LLM 이 돌리지 않는다.
  2) self_test 수치는 LLM 을 절대 신뢰하지 않는다 ("LLM 출력을 신뢰하지 않는다").
     서버가 작업장의 같은 simulate 코어(classify_core.eval_train_lite)로 train_before /
     train_after 를 직접 재계산해 output/proposal.json(proposal.v1)을 조립한다.
     LLM 은 "어떤 키를 어디로 옮길지"라는 최소 결정(changes)만 제공하고,
     new_params·self_test·검증은 전부 결정적 코드가 만든다.

따라서 machine.py·validate.py 는 CLI 어댑터와 완전히 동일하게 동작한다 — 파일이 정본,
4중 검증(재계산 대조 포함) 그대로. propose() 의 계약은 "작업장에 output/proposal.json 을
남긴다"로 CLI 와 같다.

설정(env 우선, .env 파일 폴백):
  QWEN_API_KEY        (없으면 DASHSCOPE_API_KEY 로 폴백)
  QWEN_BASE_URL       기본 https://ws-...aliyuncs.com/compatible-mode/v1
  QWEN_MODEL          기본 qwen3.7-max (워크스페이스에서 승인된 모델이어야 한다)
"""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

import httpx
import pandas as pd

from .. import ROOT_DIR
from ..config import _load_env_file, get_config
from ..loop.bounds import flatten
from ..loop.sim import classify_core as core
from .base import AgentAdapter, AgentOutput, AuthResult, DetectResult, extract_json_block

# 국제(싱가포르) 공용 엔드포인트. 전용 워크스페이스를 쓰면 QWEN_BASE_URL 로 덮어쓴다:
#   https://ws-<workspace-id>.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "qwen3.7-max"           # 워크스페이스에서 승인된 모델이어야 한다
_HTTP_TIMEOUT = 180.0                    # 요청 1건 상한 (propose 총 타임아웃과 별개로 캡)
_RETRIES = 2                             # 5xx·타임아웃·네트워크 오류에 한해 재시도
_BACKOFF = (0.5, 2.0)                    # 재시도 간 대기(초)
_TEMPERATURE = 0.3
_MAX_CHANGES = 8                         # validate.MAX_CHANGES 와 동일 (변경 키 상한)
_MAX_DROP_PP = 0.02                      # 게이트 회귀 감지 임계 (gate_rules max_drop_pp=2.0)


class QwenError(Exception):
    """사용자 문장으로 변환된 Qwen API 오류 (OpsError 와 같은 취지)."""


# ------------------------------------------------------------------ HTTP 심(seam)

def _post_once(url: str, headers: dict, payload: dict, timeout: float) -> httpx.Response:
    """단일 POST — 테스트가 이 함수 하나만 monkeypatch 하면 CLI 없이(그리고 키 없이) 검증된다
    (test_agents.py 가 run_cli 를 갈아끼우는 것과 같은 패턴)."""
    return httpx.post(url, headers=headers, json=payload, timeout=timeout)


# ------------------------------------------------------------------ 시스템 프롬프트

_SYSTEM_PROMPT = (
    "너는 시선추적 집중 판정 엔진의 파라미터 튜너다. 파일·도구·네트워크 접근이 전혀 없다. "
    "주어진 증거만으로 판단하고, 지정된 JSON 오브젝트 하나만 출력한다. "
    "코드블록·설명·인사말을 붙이지 말고 순수 JSON 만 응답하라."
)

# 인라인 증거 각 파일의 최대 길이 (토큰 폭주 방지 — 백분위·혼동행렬은 이 안에 충분히 담긴다)
_EVIDENCE_LIMIT = {
    "current_params.json": 6000,
    "confusion.json": 6000,
    "targets_bounds.json": 6000,
    "feature_stats.json": 9000,
    "gate_rules.json": 1500,
    "history.json": 6000,
    "mistakes.jsonl": 4000,
}


class QwenApiAdapter(AgentAdapter):
    name = "qwen"
    display = "Qwen (API)"
    install_hint = "API 키 설정: local/.env 에 QWEN_API_KEY=... (또는 DASHSCOPE_API_KEY) 추가 후 서버 재시작"

    # ------------------------------------------------------------ 설정 해석

    def _conf(self) -> dict:
        """env → .env 파일 → 기본값 순으로 설정을 해석한다.

        config.Config 는 정해진 키만 로드하므로 QWEN_* 는 여기서 직접 읽는다
        (.env 파일은 재시작이 필요한 값의 원천이라는 규율과 일치)."""
        file_vals = _load_env_file(ROOT_DIR / ".env")
        try:
            file_vals.update(_load_env_file(get_config().root / ".env"))
        except Exception:
            pass  # config 초기화 실패해도 ROOT_DIR 폴백으로 동작

        def val(key: str, default: str = "") -> str:
            return (os.environ.get(f"LEV_{key}") or os.environ.get(key)
                    or file_vals.get(key) or default)

        api_key = val("QWEN_API_KEY") or val("DASHSCOPE_API_KEY")
        return {
            "api_key": api_key.strip(),
            "model": val("QWEN_MODEL", _DEFAULT_MODEL).strip(),
            "base_url": val("QWEN_BASE_URL", _DEFAULT_BASE_URL).strip().rstrip("/"),
        }

    def _shown_cmd(self, conf: dict) -> str:
        return f"POST {conf['base_url']}/chat/completions (model={conf['model']})"

    # ------------------------------------------------------------ 인터페이스

    def detect(self) -> DetectResult:
        """CLI 가 아니므로 '설치'는 곧 'API 키 구성'이다."""
        conf = self._conf()
        if not conf["api_key"]:
            return DetectResult(False, command=self._shown_cmd(conf),
                                error=f"{self.display} 키가 없습니다. {self.install_hint}")
        return DetectResult(True, version=conf["model"], command=self._shown_cmd(conf))

    def check_auth(self) -> AuthResult:
        """초소형 실호출로 키·엔드포인트 유효성을 확인한다 (SPEC-01 §2 '초소형 실호출')."""
        conf = self._conf()
        shown = self._shown_cmd(conf)
        if not conf["api_key"]:
            return AuthResult(False, command=shown, checked_at=self.now(),
                              detail="QWEN_API_KEY(또는 DASHSCOPE_API_KEY)가 없습니다. "
                                     "local/.env 에 키를 넣고 서버를 재시작해 주세요.")
        url = f"{conf['base_url']}/chat/completions"
        headers = {"Authorization": f"Bearer {conf['api_key']}", "Content-Type": "application/json"}
        payload = {"model": conf["model"], "temperature": 0, "max_tokens": 8,
                   "messages": [{"role": "user", "content": "reply with exactly: OK"}]}
        try:
            r = _post_once(url, headers, payload, min(_HTTP_TIMEOUT, 30.0))
        except Exception as e:  # 네트워크·타임아웃 — httpx.HTTPError 등
            return AuthResult(False, command=shown, checked_at=self.now(),
                              detail=f"응답 없음 — {type(e).__name__}. 엔드포인트/네트워크를 확인해 주세요.")
        if r.status_code in (401, 403):
            return AuthResult(False, command=shown, checked_at=self.now(),
                              detail="인증 실패 — QWEN_API_KEY 가 올바른지 확인해 주세요.")
        if r.status_code >= 400:
            return AuthResult(False, command=shown, checked_at=self.now(),
                              detail=f"HTTP {r.status_code}: {(r.text or '')[:160]}")
        return AuthResult(True, command=shown, checked_at=self.now(),
                          detail=f"{conf['model']} 응답 확인")

    def terminal_command(self) -> list[str]:
        """API 어댑터는 OAuth 터미널 로그인이 없다 — 키 설정 안내창을 띄운다."""
        return ["cmd", "/c", "start", "Qwen 설정 안내", "cmd", "/k",
                "echo Qwen 은 CLI 로그인이 없습니다. local\\.env 에 QWEN_API_KEY 를 넣고 서버를 재시작하세요."]

    # ------------------------------------------------------------ 제안

    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        conf = self._conf()
        shown = self._shown_cmd(conf)
        if not conf["api_key"]:
            return AgentOutput(False, command=shown, error="QWEN_API_KEY 미설정 — .env 에 키를 넣어 주세요")

        # 증거 로드 (CLI 는 파일을 직접 읽지만 우리는 서버가 읽어 프롬프트에 담는다)
        try:
            current_params = _read_json(workspace / "input" / "current_params.json")
            tb = _read_json(workspace / "input" / "targets_bounds.json")
            df = pd.read_parquet(workspace / "data" / "train_lite.parquet")
        except Exception as e:
            return AgentOutput(False, command=shown, error=f"작업장 증거 읽기 실패: {e}")

        before = core.eval_train_lite(df, current_params)["per_state"]
        cur_flat = flatten(current_params)
        prompt = self._build_prompt(workspace, tb)
        messages = [{"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}]

        t0 = time.monotonic()
        try:
            content, usage = self._chat(conf, messages, timeout_sec)
        except QwenError as e:
            return AgentOutput(False, command=shown, error=str(e),
                               duration_sec=time.monotonic() - t0)

        cand = _parse_json(content)
        if cand is None:
            return AgentOutput(False, command=shown, stdout=content[-8000:],
                               duration_sec=time.monotonic() - t0,
                               error="LLM 응답에서 JSON 오브젝트를 파싱하지 못했습니다")
        applied = _coerce_changes(_candidate_changes(cand, cur_flat), cur_flat, tb)
        if not applied:
            return AgentOutput(False, command=shown, stdout=content[-8000:],
                               duration_sec=time.monotonic() - t0,
                               error="유효한 변경이 없습니다 (allowed_keys·bounds 안의 실제 값 변화 0건)")
        new_params = _apply(current_params, applied)
        after = core.eval_train_lite(df, new_params)["per_state"]
        n_runs = 2  # before + after (서버가 돌린 simulate 횟수)
        transcript = content

        # 회귀 1회 정련 — 게이트가 깨질 회귀가 보이면 simulate 결과를 돌려주고 재제안받는다
        if _regressed(before, after):
            follow = self._followup_prompt(before, after, applied, tb)
            try:
                content2, usage2 = self._chat(
                    conf, messages + [{"role": "assistant", "content": content},
                                      {"role": "user", "content": follow}], timeout_sec)
                cand2 = _parse_json(content2)
                applied2 = _coerce_changes(_candidate_changes(cand2 or {}, cur_flat), cur_flat, tb)
                if applied2:
                    np2 = _apply(current_params, applied2)
                    after2 = core.eval_train_lite(df, np2)["per_state"]
                    n_runs += 1
                    transcript += "\n\n----- 정련 재제안 -----\n" + content2
                    if _score(before, after2, tb) > _score(before, after, tb):
                        cand, applied, new_params, after = cand2, applied2, np2, after2
                    usage = usage2 or usage
            except QwenError:
                pass  # 정련 실패는 무시 — 1차 제안을 그대로 쓴다

        proposal = _assemble(cand, new_params, applied, before, after, n_runs, conf["model"])
        out_path = workspace / "output" / "proposal.json"
        out_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=1), encoding="utf-8")

        collected, source = self.collect_proposal(workspace, "")
        return AgentOutput(True, proposal=collected or proposal, proposal_source=source or "file",
                           command=shown, stdout=transcript[-50000:],
                           duration_sec=time.monotonic() - t0, exit_code=0,
                           extra={"usage": usage, "model": conf["model"], "n_simulate_runs": n_runs})

    # ------------------------------------------------------------ 프롬프트

    def _build_prompt(self, workspace: Path, tb: dict) -> str:
        mission = _read_text(workspace / "MISSION.md", 8000)
        parts = [
            "아래는 CLI 에이전트용 원본 임무문이다. 너는 파일·도구가 없으므로 "
            "simulate 실행 지시와 'output/proposal.json 파일 저장' 지시는 무시하라. "
            "self_test 수치는 서버가 같은 코드로 재계산하니 네가 채우지 않아도 된다. "
            "오직 맨 아래 'API 응답 형식'의 JSON 만 출력하라.",
            "\n===== 임무문(원본) =====\n" + mission,
            "\n===== 인라인 증거 (input/ 파일 내용) =====",
        ]
        for name, limit in _EVIDENCE_LIMIT.items():
            text = _read_text(workspace / "input" / name, limit)
            if text:
                parts.append(f"\n--- {name} ---\n{text}")
        parts.append(_output_contract(tb))
        return "\n".join(parts)

    def _followup_prompt(self, before: dict, after: dict, applied: list[dict], tb: dict) -> str:
        return (
            "위 제안을 서버가 simulate 로 재채점한 결과, 일부 상태에서 게이트 기준(-2%p 이내)"
            "을 넘는 저하가 관측되었다. 아래 수치를 보고 회귀를 없애면서 표적 상태를 개선하는 "
            "더 나은 제안 1개를 같은 JSON 형식으로 다시 내라 (변경 폭을 줄이는 것이 대개 안전하다).\n"
            f"- 적용했던 변경: {json.dumps([{'key': c['key'], 'to': c['to']} for c in applied], ensure_ascii=False)}\n"
            f"- train_before(현재): {json.dumps(_st_block(before), ensure_ascii=False)}\n"
            f"- train_after(제안): {json.dumps(_st_block(after), ensure_ascii=False)}\n"
            f"- 표적 실패 모드: {json.dumps(tb.get('observed_failure_modes', []), ensure_ascii=False)}"
        )

    # ------------------------------------------------------------ API 호출

    def _chat(self, conf: dict, messages: list[dict], timeout_sec: float) -> tuple[str, dict]:
        """chat/completions 1왕복. 5xx·타임아웃·네트워크 오류는 _RETRIES 회 재시도.
        → (content, usage). 실패는 QwenError."""
        url = f"{conf['base_url']}/chat/completions"
        headers = {"Authorization": f"Bearer {conf['api_key']}", "Content-Type": "application/json"}
        payload = {"model": conf["model"], "temperature": _TEMPERATURE,
                   "response_format": {"type": "json_object"}, "messages": messages}
        req_timeout = min(float(timeout_sec), _HTTP_TIMEOUT)
        last = ""
        for attempt in range(_RETRIES + 1):
            try:
                r = _post_once(url, headers, payload, req_timeout)
            except Exception as e:  # 네트워크·타임아웃
                last = f"{type(e).__name__}: {e}"
                if attempt < _RETRIES:
                    time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                    continue
                raise QwenError(f"Qwen API 응답 없음 — {last}")
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                if attempt < _RETRIES:
                    time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                    continue
                raise QwenError(f"Qwen API 5xx 반복 — {last}: {(r.text or '')[:300]}")
            if r.status_code in (401, 403):
                raise QwenError(f"Qwen API 인증 실패(HTTP {r.status_code}) — QWEN_API_KEY 확인")
            if r.status_code >= 400:
                # response_format 미지원 모델이면 한 번 빼고 재시도 (그레이스풀 폴백)
                if payload.pop("response_format", None) is not None:
                    continue
                raise QwenError(f"Qwen API 오류(HTTP {r.status_code}): {(r.text or '')[:300]}")
            try:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as e:
                raise QwenError(f"Qwen 응답 형식 오류: {type(e).__name__}: {e}")
            return content or "", (data.get("usage") or {})
        raise QwenError(last or "Qwen API 알 수 없는 오류")


# ------------------------------------------------------------------ 순수 헬퍼

def _read_text(path: Path, limit: int) -> str:
    try:
        t = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return t if len(t) <= limit else t[:limit] + "\n…(생략)…"


def _read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _parse_json(text: str) -> dict | None:
    """마크다운 펜스 제거 → 직파싱 → 균형 파서 폴백 (base.extract_json_block 재사용)."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.lstrip("`")
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip().strip("`").strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return extract_json_block(text)


def _candidate_changes(cand: dict, cur_flat: dict) -> list[dict]:
    """LLM 응답에서 changes 를 뽑는다. changes 가 없고 new_params 만 주면 현재값과 diff.
    (LLM 의 from 은 신뢰하지 않는다 — _coerce_changes 가 현재값으로 다시 채운다)."""
    changes = cand.get("changes")
    if isinstance(changes, list) and changes:
        return [c for c in changes if isinstance(c, dict)]
    npar = cand.get("new_params")
    if isinstance(npar, dict) and npar:
        nf = flatten(npar)
        return [{"key": k, "to": v} for k, v in nf.items()
                if k in cur_flat and nf[k] != cur_flat[k]]
    return []


def _coerce_changes(changes: list[dict], cur_flat: dict, tb: dict) -> list[dict]:
    """LLM 변경을 안전한 형태로 정규화 (LLM 출력 불신 원칙의 집행부):
      - allowed_keys 밖·미지 키 제거   - bounds 로 클램프   - from 은 현재값으로 강제
      - no-op(값 동일) 제거            - 상한 8개.
    검증 2·3단계가 통과하도록 만든다 (통과 못 할 변경은 애초에 넣지 않는다)."""
    allowed = set(tb.get("allowed_keys") or [])
    bounds = tb.get("bounds") or {}
    out: list[dict] = []
    seen: set[str] = set()
    for ch in changes:
        key = ch.get("key")
        if not isinstance(key, str) or key in seen or key not in cur_flat:
            continue
        if allowed and key not in allowed:
            continue
        cur = cur_flat[key]
        to = ch.get("to")
        if isinstance(cur, bool):
            continue  # 튜닝 대상에 bool 없음
        if isinstance(cur, (int, float)):
            if isinstance(to, str):
                try:
                    to = float(to)
                except ValueError:
                    continue
            if not isinstance(to, (int, float)) or isinstance(to, bool):
                continue
            b = bounds.get(key)
            if b:
                to = min(max(float(to), b["min"]), b["max"])
            if isinstance(cur, int) and float(to).is_integer():
                to = int(to)
            if abs(float(to) - float(cur)) < 1e-12:
                continue  # no-op
        else:
            if to == cur or not isinstance(to, str):
                continue
        seen.add(key)
        out.append({"key": key, "from": cur, "to": to,
                    "reason": str(ch.get("reason") or "").strip()[:400] or "(LLM 미기재)"})
        if len(out) >= _MAX_CHANGES:
            break
    return out


def _apply(base: dict, applied: list[dict]) -> dict:
    """검증된 changes 를 현재 params 사본에 dotted-key 로 적용 → new_params.
    키셋은 base 와 정확히 같게 유지된다 (validate 2단계 계약)."""
    new = copy.deepcopy(base)
    for ch in applied:
        cur = new
        parts = ch["key"].split(".")
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = ch["to"]
    return new


def _st_block(per_state: dict) -> dict:
    """eval_train_lite per_state → self_test 블록 {상태: {sens, spec}} (sens/spec 만)."""
    return {st: {"sens": (per_state.get(st) or {}).get("sens"),
                 "spec": (per_state.get(st) or {}).get("spec")}
            for st in core.LABEL_STATES}


def _metric(per_state: dict, st: str, m: str) -> float | None:
    v = (per_state.get(st) or {}).get(m)
    return None if v is None else float(v)


def _regressed(before: dict, after: dict, max_drop: float = _MAX_DROP_PP) -> bool:
    """어느 상태의 sens/spec 이든 max_drop 초과로 떨어지면 회귀로 본다 (게이트 위험)."""
    for st in core.LABEL_STATES:
        for m in ("sens", "spec"):
            b, a = _metric(before, st, m), _metric(after, st, m)
            if b is not None and a is not None and (b - a) > max_drop:
                return True
    return False


def _score(before: dict, after: dict, tb: dict) -> float:
    """정련 두 후보 비교용 스칼라 — 표적 개선은 +, 어떤 상태든 저하는 크게 -."""
    targets = set(tb.get("observed_failure_modes") or [])
    reward = penalty = 0.0
    for st in core.LABEL_STATES:
        for m in ("sens", "spec"):
            b, a = _metric(before, st, m), _metric(after, st, m)
            if b is None or a is None:
                continue
            d = a - b
            if st in targets:
                reward += d
            if d < 0:
                penalty += -d
    return reward - 3.0 * penalty


def _assemble(cand: dict, new_params: dict, applied: list[dict],
              before: dict, after: dict, n_runs: int, model: str) -> dict:
    """proposal.v1 조립 — self_test 는 서버 재계산값(before/after)만 기입한다.
    (LLM 이 준 diagnosis/risk/rationale 은 서술 필드로만 사용)."""
    return {
        "schema": "proposal.v1",
        "level": 1,
        "diagnosis": str(cand.get("diagnosis") or "").strip() or "(LLM 진단 미제공)",
        "new_params": new_params,
        "changes": applied,
        "self_test": {
            "n_simulate_runs": n_runs,
            "train_before": _st_block(before),
            "train_after": _st_block(after),
        },
        "risk": str(cand.get("risk") or "").strip(),
        "rationale": str(cand.get("rationale") or "").strip(),
        "meta": {"agent": "qwen", "model": model, "self_test_source": "server_recompute"},
    }


def _output_contract(tb: dict) -> str:
    allowed = tb.get("allowed_keys") or []
    return (
        "\n===== API 응답 형식 (반드시 이 JSON 오브젝트 하나만) =====\n"
        "{\n"
        '  "diagnosis": "주된 실패 모드와 원인 피처를 데이터 근거로 서술",\n'
        '  "changes": [{"key": "blank_stare.stare_dispersion_th", "to": 0.05, '
        '"reason": "feature_stats 근거"}],\n'
        '  "risk": "예상 부작용·holdout 격차 가능성",\n'
        '  "rationale": "3~5문장 요약"\n'
        "}\n"
        "규칙: changes 는 1~8개. key 는 아래 allowed_keys 안에서만, 값은 bounds 안에서만. "
        "sfi_weights 를 건드리면 합이 정확히 100 이어야 한다. from 은 적지 않아도 된다"
        "(서버가 현재값으로 채운다). self_test 수치는 넣지 마라 — 서버가 같은 simulate 로 재계산한다.\n"
        f"allowed_keys = {json.dumps(allowed, ensure_ascii=False)}"
    )
