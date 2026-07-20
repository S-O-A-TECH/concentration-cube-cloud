"""시드 데모 데이터 — 합성 LAB-5 세션 생성 + mock 운영 서버에 정답지 POST.

사용:
  1) python run_mock_ops.py            (mock 을 먼저 켠다)
  2) .venv\\Scripts\\python tools\\seed_demo_data.py

라벨은 실제 API(POST labels)로 넣는다 — 1회 불변(409) 규칙까지 리허설되는 셈.
이미 라벨이 있으면(재실행) 건너뛴다. 실제 웹캠 세션에는 손대지 않는다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_config  # noqa: E402
from tests.mocks.synth_sessions import generate_batch  # noqa: E402


def main():
    cfg = get_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=cfg.server_url)
    ap.add_argument("--token", default=cfg.evolution_token)
    ap.add_argument("--out", default=str(ROOT / "tests" / "mocks" / "demo_sessions"))
    ap.add_argument("--no-labels", action="store_true", help="세션 파일만 생성")
    args = ap.parse_args()

    out_dir = Path(args.out)
    batch = generate_batch(out_dir)
    n_train = len([b for b in batch if b["split"] == "train"])
    n_hold = len([b for b in batch if b["split"] == "holdout"])
    print(f"합성 세션 {len(batch)}개 생성 → {out_dir}  (train {n_train} / holdout {n_hold})")

    if args.no_labels:
        return
    headers = {"X-Evolution-Token": args.token}
    ok = skip = fail = 0
    for b in batch:
        try:
            r = httpx.post(f"{args.server}/v1/evolution/sessions/{b['sid']}/labels",
                           json={"labeler": "admin", "method": "realtime_instructed",
                                 "protocol": b["protocol"], "segments": b["segments"]},
                           headers=headers, timeout=10)
        except httpx.HTTPError as e:
            print(f"  ! {b['sid']}: 연결 실패 — mock 이 켜져 있나요? ({e})")
            fail += 1
            continue
        if r.status_code == 200:
            ok += 1
        elif r.status_code == 409:
            skip += 1   # 이미 라벨 있음 — 불변 규칙 정상 동작
        else:
            print(f"  ! {b['sid']}: {r.status_code} {r.text[:120]}")
            fail += 1
    print(f"정답지 POST: 신규 {ok} / 기존 유지 {skip} / 실패 {fail}")
    if fail == 0:
        print("완료 — 대시보드(http://127.0.0.1:8200)에서 데이터 현황을 확인하세요.")


if __name__ == "__main__":
    main()
