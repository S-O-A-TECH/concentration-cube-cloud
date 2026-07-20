"""Claude Code 어댑터 (SPEC-01, SPEC-06 §4).

명령 계약:
  인증 확인: claude -p "reply with exactly: OK" --output-format json   (15s)
  제안 실행: claude -p <MISSION 전문> --output-format json
             --allowedTools ...   (작업장 cwd — Read/Write/Edit/Bash 자가 시뮬 루프)

CLI 플래그는 버전업으로 변할 수 있다 (E2 리스크) — 추가 인자는 settings
'agent_claude_args' 로 조정 가능하게 두고, 실패 시 실행한 명령 원문을 화면에 보인다.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..db import get_setting
from .base import AgentAdapter, AgentOutput, AuthResult, resolve_exe, run_cli

# MISSION.md 는 Windows 에서 $(cat ...) 셸 치환이 없으므로 파이썬이 파일을 읽어
# -p 인자로 그대로 넘긴다 — 계약("MISSION 전문이 프롬프트 본문")은 동일.
_DEFAULT_ARGS = ["--allowedTools", "Read,Write,Edit,Bash"]


class ClaudeAdapter(AgentAdapter):
    name = "claude"
    display = "Claude Code"
    install_hint = "설치: npm install -g @anthropic-ai/claude-code"

    def _extra_args(self) -> list[str]:
        v = get_setting("agent_claude_args")
        return list(v) if isinstance(v, list) else _DEFAULT_ARGS

    def check_auth(self) -> AuthResult:
        exe = resolve_exe(self.name)
        if not exe:
            return AuthResult(False, detail="설치되어 있지 않아요.", checked_at=self.now())
        cmd = [exe, "-p", "reply with exactly: OK", "--output-format", "json"]
        code, out, err, _, error = run_cli(cmd, None, timeout=self.auth_timeout())
        shown = " ".join(cmd[:1] + cmd[1:3]) + " --output-format json"
        if error:
            return AuthResult(False, detail=f"응답 없음 — {error}. 로그인이 필요할 수 있어요.",
                              command=shown, checked_at=self.now())
        ok = False
        detail = ""
        try:
            j = json.loads(out.strip().splitlines()[-1])
            ok = (j.get("subtype") == "success"
                  or "OK" in str(j.get("result", "")).upper())
            detail = str(j.get("result", ""))[:200]
        except (json.JSONDecodeError, IndexError):
            ok = code == 0 and "OK" in out.upper()
            detail = (out or err).strip()[:200]
        if not ok:
            low = (out + err).lower()
            if any(k in low for k in ("login", "auth", "credential", "api key", "unauthorized")):
                detail = "로그인이 필요해요. [터미널 열기]로 로그인을 마친 뒤 [다시 확인]을 눌러 주세요."
        return AuthResult(ok, detail=detail, command=shown, checked_at=self.now())

    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        exe = resolve_exe(self.name)
        if not exe:
            return AgentOutput(False, error="claude CLI 미설치")
        mission = (workspace / "MISSION.md").read_text(encoding="utf-8")
        # MISSION 전문은 stdin 으로 (argv 금지 — .cmd 셔틀의 개행 손상, run_cli 주석 참고)
        cmd = [exe, "-p", "--output-format", "json", *self._extra_args()]
        code, out, err, dur, error = run_cli(cmd, workspace, timeout=timeout_sec,
                                             input_text=mission)
        cost = None
        try:
            j = json.loads(out.strip().splitlines()[-1])
            cost = j.get("total_cost_usd")
        except (json.JSONDecodeError, IndexError):
            pass
        proposal, source = self.collect_proposal(workspace, out)
        shown_cmd = f'type MISSION.md | "{exe}" -p --output-format json ' + " ".join(self._extra_args())
        if error:
            return AgentOutput(False, command=shown_cmd, stdout=out, stderr=err,
                               exit_code=code, duration_sec=dur, cost_usd=cost, error=error)
        if proposal is None:
            return AgentOutput(False, command=shown_cmd, stdout=out, stderr=err,
                               exit_code=code, duration_sec=dur, cost_usd=cost,
                               error="output/proposal.json 이 없고 stdout 에서도 proposal.v1 을 찾지 못했습니다")
        return AgentOutput(True, proposal=proposal, proposal_source=source,
                           command=shown_cmd, stdout=out, stderr=err, exit_code=code,
                           duration_sec=dur, cost_usd=cost)

    def terminal_command(self) -> list[str]:
        return ["cmd", "/c", "start", "Claude 로그인", "cmd", "/k", "claude"]
