"""S7 부하 스모크 — device_sim 5대 병렬 × 3세션(DEV-2), 오류율 0 + 채점 지연 <2분.

전제: 기기 5대가 등록돼 있어야 한다 — 스크립트가 ops 로그인으로 자동 등록한다.
실행: .venv\\Scripts\\python tools\\s7_load_smoke.py [base_url]
"""
import concurrent.futures as cf
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings          # noqa: E402
from tests.device_sim import DeviceSim       # noqa: E402

N_DEVICES = 5
N_SESSIONS = 3
SCENARIO = [("focus", 60), ("blank_stare", 30), ("focus", 30)]   # 2분


def ensure_devices(base: str) -> list[tuple[str, str]]:
    s = get_settings()
    client = httpx.Client(base_url=base, timeout=30)
    r = client.post("/v1/ops/login", json={"id": s.ops_admin_id, "pw": s.ops_admin_pw})
    r.raise_for_status()
    creds = []
    for i in range(N_DEVICES):
        serial = f"LOAD_DEV_{i:03d}"
        r = client.post("/v1/ops/devices", json={"serial": serial})
        if r.status_code == 200:
            creds.append((serial, r.json()["factory_token"]))
        elif r.status_code == 409:   # 이미 등록 → 토큰 재발급
            devices = client.get("/v1/ops/devices").json()["devices"]
            dev_id = next(d["id"] for d in devices if d["serial"] == serial)
            r2 = client.post(f"/v1/ops/devices/{dev_id}/reissue_token")
            creds.append((serial, r2.json()["factory_token"]))
        else:
            raise RuntimeError(f"device setup failed: {r.text}")
    return creds


def run_device(base: str, serial: str, token: str) -> list[str]:
    client = httpx.Client(base_url=base, timeout=60)
    sim = DeviceSim(client, serial, token)
    sids = []
    for _ in range(N_SESSIONS):
        out = sim.run_session(SCENARIO, mode="DEV-2")
        assert out["finish"].status_code == 200, out["finish"].text
        assert out["finish"].json()["count_match"] is True
        sids.append(out["sid"])
    return sids


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
    creds = ensure_devices(base)
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=N_DEVICES) as pool:
        futures = [pool.submit(run_device, base, s, t) for s, t in creds]
        all_sids = [sid for f in futures for sid in f.result()]
    upload_sec = time.time() - t0
    print(f"업로드 {len(all_sids)}세션 완료 — {upload_sec:.1f}s, 오류 0")

    # 채점 완료 대기 (<2분)
    s = get_settings()
    client = httpx.Client(base_url=base, timeout=30)
    client.post("/v1/ops/login", json={"id": s.ops_admin_id, "pw": s.ops_admin_pw})
    deadline = time.time() + 120
    remaining = set(all_sids)
    while remaining and time.time() < deadline:
        sessions = client.get("/v1/ops/sessions?limit=200").json()["sessions"]
        state = {x["sid"]: x for x in sessions}
        remaining = {sid for sid in remaining
                     if state.get(sid, {}).get("scoring_state") != "done"}
        if remaining:
            time.sleep(3)
    scoring_sec = time.time() - t0
    if remaining:
        print(f"FAIL: 2분 내 미채점 {len(remaining)}건")
        return 1
    print(f"채점 완료 — 총 {scoring_sec:.1f}s (기준 <120s)")
    print("S7 LOAD SMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
