"""Codex CLI 어댑터 (SPEC-01) — claude 와 동일 계약, 샌드박스=작업장.

  인증 확인: codex exec "reply with exactly: OK"          (15s)
  제안 실행: codex exec <MISSION 전문>  (cwd=작업장,
             --sandbox workspace-write --skip-git-repo-check)
             ※ 작업장은 git 저장소가 아니므로 skip-git-repo-check 필요.
"""
from __future__ import annotations

from pathlib import Path

from ..db import get_setting
from .base import AgentAdapter, AgentOutput, AuthResult, resolve_exe, run_cli

_DEFAULT_ARGS = ["--sandbox", "workspace-write", "--skip-git-repo-check"]


class CodexAdapter(AgentAdapter):
    name = "codex"
    display = "Codex"
    install_hint = "Install: npm install -g @openai/codex"

    def _extra_args(self) -> list[str]:
        v = get_setting("agent_codex_args")
        return list(v) if isinstance(v, list) else _DEFAULT_ARGS

    def check_auth(self) -> AuthResult:
        exe = resolve_exe(self.name)
        if not exe:
            return AuthResult(False, detail="Not installed.", checked_at=self.now())
        cmd = [exe, "exec", "reply with exactly: OK", "--skip-git-repo-check"]
        code, out, err, _, error = run_cli(cmd, None, timeout=self.auth_timeout())
        shown = f'"{exe}" exec "reply OK"'
        if error:
            return AuthResult(False, detail=f"No response — {error}. Login may be required.",
                              command=shown, checked_at=self.now())
        ok = code == 0 and "OK" in out.upper()
        detail = (out or err).strip()[-200:]
        if not ok:
            low = (out + err).lower()
            if any(k in low for k in ("login", "auth", "credential", "api key", "unauthorized")):
                detail = "Login required. Use [Open terminal] to run `codex login`, then press [Check again]."
        return AuthResult(ok, detail=detail, command=shown, checked_at=self.now())

    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        exe = resolve_exe(self.name)
        if not exe:
            return AgentOutput(False, error="codex CLI not installed")
        mission = (workspace / "MISSION.md").read_text(encoding="utf-8")
        # "codex exec -" = stdin 에서 프롬프트 읽기 — argv 금지 (.cmd 셔틀 개행 손상 방지)
        cmd = [exe, "exec", "-", *self._extra_args()]
        code, out, err, dur, error = run_cli(cmd, workspace, timeout=timeout_sec,
                                             input_text=mission)
        proposal, source = self.collect_proposal(workspace, out)
        shown_cmd = f'type MISSION.md | "{exe}" exec - ' + " ".join(self._extra_args())
        if error:
            return AgentOutput(False, command=shown_cmd, stdout=out, stderr=err,
                               exit_code=code, duration_sec=dur, error=error)
        if proposal is None:
            return AgentOutput(False, command=shown_cmd, stdout=out, stderr=err,
                               exit_code=code, duration_sec=dur,
                               error="output/proposal.json is missing and no proposal.v1 was found in stdout")
        return AgentOutput(True, proposal=proposal, proposal_source=source,
                           command=shown_cmd, stdout=out, stderr=err, exit_code=code,
                           duration_sec=dur)

    def terminal_command(self) -> list[str]:
        return ["cmd", "/c", "start", "Codex login", "cmd", "/k", "codex login"]
