"""에이전트 작업장 구성기 (SPEC-06 §3).

workspace/<gen_id>/
├─ MISSION.md            고정 임무문 (prompts.render_mission)
├─ input/                Evidence Pack 7파일
├─ data/train_lite.parquet
├─ tools/simulate.py     classify_core.py 소스 + simulate_main.py 를 이어붙인 자립 스크립트
│                        → 에이전트의 연습장과 우리의 재계산이 '같은 코드'가 되는 장치
├─ output/               proposal.json 수거 대상 (비워 둔다)
└─ manifest.json         입력 파일 해시 (SPEC-04 §4 재현성)

★ holdout 은 이 폴더 어디에도 존재하지 않는다 — verify_no_holdout 이 강제.
★ 자격증명(.env·토큰) 은 절대 복사하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from ..config import get_config
from . import prompts
from .sim import classify_core

_SIM_DIR = Path(__file__).resolve().parent / "sim"


def _render_simulate_py() -> str:
    core_src = (_SIM_DIR / "classify_core.py").read_text(encoding="utf-8")
    main_src = (_SIM_DIR / "simulate_main.py").read_text(encoding="utf-8")
    return core_src + "\n\n" + main_src


def create_workspace(gen_id: str, evidence: dict, feedback: str = "") -> Path:
    cfg = get_config()
    ws = cfg.workspaces_dir / gen_id
    if ws.exists():
        shutil.rmtree(ws)
    (ws / "input").mkdir(parents=True)
    (ws / "data").mkdir()
    (ws / "tools").mkdir()
    (ws / "output").mkdir()

    (ws / "MISSION.md").write_text(
        prompts.render_mission(cfg.python_exe, feedback=feedback), encoding="utf-8")

    for name, content in evidence["files"].items():
        p = ws / "input" / name
        if isinstance(content, str):
            p.write_text(content, encoding="utf-8")
        else:
            p.write_text(json.dumps(content, ensure_ascii=False, indent=1), encoding="utf-8")

    evidence["train_lite"].to_parquet(ws / "data" / "train_lite.parquet",
                                      engine="pyarrow", index=False)
    (ws / "tools" / "simulate.py").write_text(_render_simulate_py(), encoding="utf-8")

    manifest = {"gen_id": gen_id, "files": {}}
    for p in sorted(ws.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            manifest["files"][str(p.relative_to(ws)).replace("\\", "/")] = \
                hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    (ws / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    verify_no_holdout(ws, evidence["meta"].get("holdout_sids", []))
    return ws


def verify_no_holdout(ws: Path, holdout_sids: list[str]) -> None:
    """작업장 어디에도 holdout 세션이 없음을 강제 (위반 = 프로그래밍 오류 → 즉시 중단)."""
    if not holdout_sids:
        return
    import pandas as pd
    lite = ws / "data" / "train_lite.parquet"
    if lite.exists():
        sids = set(pd.read_parquet(lite, columns=["sid"])["sid"].unique())
        leaked = sids & set(holdout_sids)
        if leaked:
            raise RuntimeError(f"holdout 세션이 train_lite 에 섞였습니다: {sorted(leaked)}")
    for p in ws.rglob("*"):
        if not p.is_file() or p.suffix in (".parquet",):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for sid in holdout_sids:
            if sid in text:
                raise RuntimeError(f"holdout sid {sid} 가 작업장 파일에 노출: {p.name}")


def read_proposal(ws: Path) -> dict | None:
    p = ws / "output" / "proposal.json"
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None
