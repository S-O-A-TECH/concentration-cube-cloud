# -*- coding: utf-8 -*-
# ================================================================
# simulate 러너 — 작업장 tools/simulate.py 의 뒷부분.
# workspace.py 가 classify_core.py 소스 뒤에 이 파일을 그대로 이어붙여
# 자립형(standalone) 스크립트를 만든다. 따라서 이 파일은 classify_core 의
# 함수들이 같은 네임스페이스에 있다고 가정하며, app.* 을 import 하지 않는다.
#
# 사용 (에이전트 관점):
#   python tools/simulate.py candidate.json
#   → stdout 에 JSON: {"per_state": {...sens/spec...}, "confusion": {...}, "n_bins": N}
# candidate.json = params 전체 (current_params.json 과 같은 구조)
# ================================================================

def _simulate_cli():
    import json
    import sys
    from pathlib import Path

    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: python tools/simulate.py <candidate_params.json>"}))
        sys.exit(2)
    root = Path(__file__).resolve().parent.parent  # 작업장 루트
    cand_path = Path(sys.argv[1])
    if not cand_path.is_absolute():
        cand_path = Path.cwd() / cand_path
    try:
        with open(cand_path, encoding="utf-8") as f:
            params = json.load(f)
    except Exception as e:
        print(json.dumps({"error": f"candidate params 읽기 실패: {e}"}))
        sys.exit(2)
    data = root / "data" / "train_lite.parquet"
    if not data.exists():
        print(json.dumps({"error": "data/train_lite.parquet 없음"}))
        sys.exit(2)
    df = pd.read_parquet(data)
    out = eval_train_lite(df, params)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    _simulate_cli()
