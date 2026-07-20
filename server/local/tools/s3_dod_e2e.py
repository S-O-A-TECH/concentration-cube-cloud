"""S3 DoD — 실서버(compose) E2E: 업로드 → 개입 없이 자동 채점 → result (≤30초).

실행:  .venv\\Scripts\\python tools\\s3_dod_e2e.py <factory_token> [serial] [base_url]
LLM 리포트가 켜져 있으면 추가로 최대 120초 대기해 llm_report 필드 확인(선택).
"""
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.device_sim import DeviceSim  # noqa: E402

SCENARIO = [("focus", 300), ("blank_stare", 60), ("focus", 240),
            ("off_task", 90), ("focus", 510)]     # 20분 (SFI-20)

CONTRACT_KEYS = ("schema", "algo_version", "param_set_version", "confidence", "sfi",
                 "components", "focused_minutes", "max_focus_streak_min",
                 "timeline", "events", "coach_text", "quality")


def main():
    token = sys.argv[1]
    serial = sys.argv[2] if len(sys.argv) > 2 else "WEBCAM_PROTO_001"
    base = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8100"

    client = httpx.Client(base_url=base, timeout=60)
    sim = DeviceSim(client, serial, token)
    out = sim.run_session(SCENARIO, mode="SFI-20", chunk_size=600)
    sid = out["sid"]
    print(f"sid: {sid} / finish: count_match={out['finish'].json().get('count_match')}")

    # 개입 없이 30초 내 result (S3 DoD)
    deadline = time.time() + 30
    result = None
    while time.time() < deadline:
        r = client.get(f"/v1/sessions/{sid}/result", headers=sim._headers())
        if r.status_code == 200:
            result = r.json()
            break
        time.sleep(2)
    if result is None:
        status = client.get(f"/v1/sessions/{sid}/status", headers=sim._headers()).json()
        print(f"FAIL: 30초 내 result 없음 — status={status}")
        sys.exit(1)

    missing = [k for k in CONTRACT_KEYS if k not in result]
    print(f"sfi: {result['sfi']} / confidence: {result['confidence']} "
          f"/ focused_min: {result['focused_minutes']}")
    print(f"timeline segments: {len(result['timeline'])} / events: {len(result['events'])}")
    if missing:
        print(f"FAIL: 계약 필드 누락 {missing}")
        sys.exit(1)

    # LLM 리포트 (선택) — 최대 120초
    llm_deadline = time.time() + 120
    while time.time() < llm_deadline:
        r = client.get(f"/v1/sessions/{sid}/result", headers=sim._headers())
        body = r.json()
        if "llm_report" in body:
            print(f"llm_report(student): {body['llm_report']['student'][:80]}...")
            break
        time.sleep(5)
    else:
        print("(llm_report 미생성 — 비활성이거나 지연: 채점 DoD 와는 무관)")

    print("S3 DoD: PASS")


if __name__ == "__main__":
    main()
