"""AgentAdapter 인터페이스 (SPEC-01 §1) + 공용 subprocess 실행기.

에이전트의 OAuth 는 호스트 사용자 프로필에 있다 — 이 서버는 토큰을 저장하지 않고
설치 감지 → 초소형 실호출 인증 확인 → 터미널 로그인 안내만 한다 (SPEC-01 §2, SPEC-04 §1).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DetectResult:
    installed: bool
    version: str = ""
    command: str = ""
    error: str = ""


@dataclass
class AuthResult:
    ok: bool
    detail: str = ""
    command: str = ""
    checked_at: str = ""


@dataclass
class AgentOutput:
    ok: bool
    proposal: dict | None = None
    proposal_source: str = ""      # file | stdout
    command: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_sec: float = 0.0
    cost_usd: float | None = None
    error: str = ""
    extra: dict = field(default_factory=dict)


def run_cli(cmd: list[str], cwd: Path | None, timeout: float,
            input_text: str | None = None) -> tuple[int | None, str, str, float, str]:
    """→ (exit_code, stdout, stderr, duration, error). 타임아웃/미설치는 error 로.

    input_text 가 있으면 stdin 파이프로 전달한다. ★여러 줄 프롬프트(MISSION)는 반드시
    이 경로로 — npm 이 만드는 Windows .cmd 셔틀은 argv 를 cmd.exe 의 %* 로 재전개하므로
    인자 속 개행에서 잘리고 한글 코드페이지도 깨진다 (리뷰에서 실증됨). stdin 은
    셔틀을 그대로 통과하므로 안전하다. 없으면 DEVNULL (CLI 의 stdin 3s 대기 회피).
    """
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True,
            input=input_text,
            **({} if input_text is not None else {"stdin": subprocess.DEVNULL}),
            timeout=timeout, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p.returncode, p.stdout or "", p.stderr or "", time.monotonic() - t0, ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return None, out, err, time.monotonic() - t0, f"타임아웃({int(timeout)}s) 초과"
    except FileNotFoundError:
        return None, "", "", time.monotonic() - t0, "실행 파일을 찾을 수 없습니다"
    except OSError as e:
        return None, "", "", time.monotonic() - t0, f"실행 실패: {e}"


def resolve_exe(name: str) -> str | None:
    """Windows 에서 .cmd/.exe 셔틀까지 해석."""
    for cand in (name, f"{name}.cmd", f"{name}.exe"):
        p = shutil.which(cand)
        if p:
            return p
    return None


def extract_json_block(text: str) -> dict | None:
    """stdout 폴백 파서 (E2 리스크: '파일 대신 stdout 에만 출력') — 첫 균형 JSON 오브젝트."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


class AgentAdapter:
    name: str = ""
    display: str = ""
    install_hint: str = ""

    @staticmethod
    def auth_timeout() -> float:
        """인증 확인 타임아웃 — SPEC-01 은 15s 로 계획했으나 이 PC 실측 43~65s (콜드 스타트,
        날에 따라 60s 도 초과). 계획과 어긋나는 실측은 설정으로 흡수한다
        (settings agent_auth_timeout_sec, 기본 120s)."""
        from ..db import get_setting
        return float(get_setting("agent_auth_timeout_sec", 120))

    # ------------------------------------------------------------ 인터페이스

    def detect(self) -> DetectResult:
        exe = resolve_exe(self.name)
        if not exe:
            return DetectResult(False, command=f"{self.name} --version",
                                error=f"{self.display} 가 설치되어 있지 않아요. {self.install_hint}")
        code, out, err, _, error = run_cli([exe, "--version"], None, timeout=5.0)
        if error or code != 0:
            return DetectResult(False, command=f'"{exe}" --version',
                                error=error or err.strip() or f"exit {code}")
        m = re.search(r"\d+[\.\w-]*", out)
        return DetectResult(True, version=m.group(0) if m else out.strip()[:40],
                            command=f'"{exe}" --version')

    def check_auth(self) -> AuthResult:  # 초소형 실호출 (15s)
        raise NotImplementedError

    def propose(self, workspace: Path, timeout_sec: float) -> AgentOutput:
        """작업장 cwd 헤드리스 실행 → output/proposal.json 수거 (파일이 정본)."""
        raise NotImplementedError

    def terminal_command(self) -> list[str]:
        """[터미널 열기] — CLI 가 OAuth 브라우저 창을 띄우는 명령 (SPEC-01 §2 ③)."""
        raise NotImplementedError

    # ------------------------------------------------------------ 공용 수거

    def collect_proposal(self, workspace: Path, stdout: str) -> tuple[dict | None, str]:
        p = workspace / "output" / "proposal.json"
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f), "file"
            except json.JSONDecodeError:
                pass
        block = extract_json_block(stdout)
        if block and block.get("schema") == "proposal.v1":
            return block, "stdout"
        return None, ""

    @staticmethod
    def now() -> str:
        return dt.datetime.now().astimezone().isoformat(timespec="seconds")
