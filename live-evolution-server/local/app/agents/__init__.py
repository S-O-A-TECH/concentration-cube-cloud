"""AI 에이전트 어댑터 (SPEC-01) — claude / codex 는 subprocess 헤드리스,
qwen 은 Alibaba Cloud Model Studio OpenAI 호환 API (파일·도구 없이 JSON 제안)."""
from __future__ import annotations

from .base import AgentAdapter
from .claude_cli import ClaudeAdapter
from .codex_cli import CodexAdapter
from .qwen_api import QwenApiAdapter

ADAPTERS: dict[str, AgentAdapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "qwen": QwenApiAdapter(),
}


def get_adapter(name: str) -> AgentAdapter:
    if name not in ADAPTERS:
        raise KeyError(f"알 수 없는 에이전트: {name}")
    return ADAPTERS[name]
