"""복구 리허설 자동 검증 (RUNBOOK §4) — 복구 환경(기본 18100)에서
health → ops 로그인 → 세션 목록 → 첫 complete 세션 result 조회.

실행: .venv\\Scripts\\python tools\\verify_restore.py [base_url]
"""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18100"
    s = get_settings()
    client = httpx.Client(base_url=base, timeout=30)

    health = client.get("/health").json()
    print(f"health: {health}")
    if not (health["ok"] or (health["db"] and health["storage"])):
        print("FAIL: health")
        return 1

    r = client.post("/v1/ops/login", json={"id": s.ops_admin_id, "pw": s.ops_admin_pw})
    if r.status_code != 200:
        print(f"FAIL: ops login {r.status_code}")
        return 1

    sessions = client.get("/v1/ops/sessions?state=complete").json()["sessions"]
    print(f"complete sessions: {len(sessions)}")
    if not sessions:
        print("FAIL: 복원된 세션 없음")
        return 1

    scored = [x for x in sessions if x["sfi"] is not None]
    target = (scored or sessions)[0]
    r = client.get(f"/v1/ops/sessions/{target['sid']}/report")
    if r.status_code != 200:
        print(f"FAIL: report {r.status_code} — {r.text[:120]}")
        return 1
    result = r.json()["result"]
    print(f"restored report OK — sid {target['sid'][:8]}… sfi={result['sfi']} "
          f"timeline={len(result['timeline'])}구간")
    print("RESTORE REHEARSAL: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
