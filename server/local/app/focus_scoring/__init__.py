"""focus_scoring — 순수 채점 패키지.

원칙 (phase2 §4 순수 함수 원칙과 동일):
- 네트워크·DB·하드웨어 접근 금지. 같은 입력이면 언제나 같은 출력.
- 입력: 10Hz record DataFrame + param_set(dict) / 출력: result dict
"""
import json
from pathlib import Path

_PARAMS_DIR = Path(__file__).resolve().parent / "params"


def load_params(path=None) -> dict:
    """param_set 로딩. 기본은 params/default.json."""
    p = Path(path) if path else _PARAMS_DIR / "default.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def score(df, params: dict) -> dict:
    """채점 진입점: record DataFrame + params → result dict (SPEC §4.4 계약).

    순수 함수 — session/서버 코드는 이 함수 하나만 호출한다.
    """
    from . import qc, events, sfi, report

    quality = qc.assess(df, params)
    if not quality["scorable"]:
        return report.build_unscorable(df, params, quality)
    states, ev = events.analyze(df, params, quality)
    components, sfi_value, confidence = sfi.compute(df, params, states, ev, quality)
    return report.build(df, params, states, ev, components, sfi_value, confidence, quality)
