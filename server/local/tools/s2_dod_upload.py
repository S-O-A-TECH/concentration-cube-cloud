"""S2 DoD — 실서버(compose, 127.0.0.1:8100)에 20분 합성 세션(SFI-20) 업로드.

실행:  .venv\\Scripts\\python tools\\s2_dod_upload.py <serial> <factory_token>
       (시드 기기: WEBCAM_PROTO_001 — factory_token 은 시드 로그에 1회 출력)
"""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.device_sim import DeviceSim  # noqa: E402

SCENARIO_20MIN = [
    ("focus", 300), ("blank_stare", 60), ("focus", 240),
    ("off_task", 90), ("focus", 510),
]  # 합계 1200초 = SFI-20


def main():
    serial = sys.argv[1] if len(sys.argv) > 1 else "WEBCAM_PROTO_001"
    token = sys.argv[2]
    base = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8100"

    client = httpx.Client(base_url=base, timeout=60)
    sim = DeviceSim(client, serial, token)
    out = sim.run_session(SCENARIO_20MIN, mode="SFI-20", chunk_size=600)
    fin = out["finish"].json()
    print(f"sid: {out['sid']}")
    print(f"finish: {fin}")
    status = client.get(f"/v1/sessions/{out['sid']}/status",
                        headers=sim._headers()).json()
    print(f"status: {status}")
    ok = fin.get("count_match") is True and status.get("upload_state") == "complete"
    print("S2 DoD:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
