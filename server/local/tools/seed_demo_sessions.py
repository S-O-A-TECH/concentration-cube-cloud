"""데모 시딩 — 빈 DB 에 라벨된 LAB-5 세션을 채워 자가진화 루프를 돌릴 수 있게 한다.

새로 배포한 클라우드(ECS)의 DB 는 비어 있다. 자가진화 엔진은 train 라벨 10개
이상을 요구하므로(SPEC-05 게이트) 데모 전에 이 도구로 합성 세션을 올린다.

핵심: 'blank_hard' 프로파일(dispersion 0.045)을 섞는다. v1.0 임계
stare_dispersion_th=0.035 는 이 멍때림을 focus 로 오분류한다 — 즉 일부러
개선 여지를 남긴 데이터다. 진화 엔진이 좁힐 표적이 바로 이 격차다.
(live-evolution-server/tests/mocks/synth_sessions.py 와 같은 사상.)

컨테이너 안에서 실행하는 것을 기본으로 한다 — api 컨테이너에는 EVOLUTION_TOKEN
과 DATABASE_URL 이 이미 주입되어 있어 인자 없이도 동작한다:

    docker compose -f docker-compose.cloud.yml exec api \\
        python tools/seed_demo_sessions.py --sessions 18

설정 우선순위는 어디서나 CLI 인자 > 환경변수 > 기본값.
기기 시리얼은 app.db.seed 의 정본을 그대로 읽고, factory_token 은 DB 에 sha256
해시만 남아 평문을 되읽을 수 없으므로 --factory-token 이 없으면 DB 에 직접
재발급한다(app/db/seed.py 가 문서화한 '분실 시 재발급 = 해시 갱신' 경로).

tests/ 는 운영 이미지에 넣지 않으므로 tests/device_sim.py·tests/synth.py 에
의존하지 않는다 — 필요한 최소 로직만 아래에 옮겨 담았다. app.* 만 참조한다.

종료 코드: 0 정상 / 1 오류 / 2 train 라벨이 임계 미만(배포 스크립트 게이트용).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
import time
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.seed import SEED_DEVICE_SERIAL  # noqa: E402
from app.focus_scoring.records import SAMPLE_RATE_HZ, empty_record  # noqa: E402
from app.services.integrity import nan_to_none, records_crc32  # noqa: E402
from app.services.split import split_for  # noqa: E402

DEFAULT_SERVER = "http://127.0.0.1:8100"   # 컨테이너 내부에서 api 자기 자신
TRAIN_LABEL_MIN = 10                       # 진화 트리거 최소 train 라벨 수

# tests/synth.py 프로파일 + 'blank_hard'(v1.0 이 놓치는 멍때림 — 개선 표적).
_PROFILES = {
    "focus": dict(prob=0.92, disp=0.08, sacc=2.5, fix_ms=250.0, line=True,
                  ear=0.28, openness=0.9, alt=1.5),
    "off_task": dict(prob=0.05, disp=0.15, sacc=1.0, fix_ms=300.0, line=False,
                     ear=0.28, openness=0.9, alt=0.5),
    "blank_stare": dict(prob=0.92, disp=0.012, sacc=0.2, fix_ms=900.0, line=False,
                        ear=0.26, openness=0.8, alt=0.0),
    "blank_hard": dict(prob=0.92, disp=0.045, sacc=0.3, fix_ms=900.0, line=False,
                       ear=0.26, openness=0.8, alt=0.0),
}

# 5분(300초) 시나리오 3종 — (프로파일, 초, 정답지 라벨).
# 프로파일 blank_hard 도 정답지에서는 blank_stare 다: 사람이 보면 멍때림인데
# v1.0 엔진만 focus 라 우기는 구간 — 이 불일치가 진화의 연료다.
SCENARIOS = [
    [("focus", 120, "focus"), ("blank_hard", 60, "blank_stare"),
     ("focus", 60, "focus"), ("off_task", 60, "off_task")],
    [("focus", 90, "focus"), ("blank_stare", 30, "blank_stare"),
     ("blank_hard", 60, "blank_stare"), ("focus", 60, "focus"),
     ("off_task", 60, "off_task")],
    [("focus", 150, "focus"), ("off_task", 60, "off_task"),
     ("blank_hard", 60, "blank_stare"), ("focus", 30, "focus")],
]


# ---------------------------------------------------------------- 합성 레코드

def make_records(scenario: list[tuple[str, float]], seed: int) -> list[dict]:
    """(프로파일, 초) 목록 → 10Hz record 목록. 같은 seed 면 같은 결과."""
    rng = np.random.default_rng(seed)
    records: list[dict] = []
    idx = 0
    t_ms = 0
    for state, seconds in scenario:
        for _ in range(int(seconds * SAMPLE_RATE_HZ)):
            idx += 1
            t_ms += 100
            r = empty_record(idx, t_ms)
            p = _PROFILES[state]
            jitter = rng.normal(0, 0.01)
            r.update(
                face_valid=True, both_eyes_valid=True, gaze_valid=True,
                gaze_x=0.5 + jitter, gaze_y=0.5 + jitter,
                gaze_on_page_prob=float(np.clip(p["prob"] + rng.normal(0, 0.03), 0, 1)),
                ear_mean=p["ear"], eye_openness=p["openness"],
                blink_count=0, long_blink_flag=False,
                head_yaw_deg=float(rng.normal(0, 2)),
                head_pitch_deg=float(rng.normal(-10, 2)),
                head_roll_deg=0.0,
                distance_cm=float(45 + rng.normal(0, 1.5)),
                frame_brightness=120.0, valid_frame_ratio=1.0,
                saccade_count_1s=max(0.0, p["sacc"] + float(rng.normal(0, 0.2))),
                mean_fixation_ms=p["fix_ms"],
                gaze_dispersion_1s=max(0.001, p["disp"] + float(rng.normal(0, 0.003))),
                line_progression_flag=bool(p["line"]),
                region_alternation_1s=p["alt"],
            )
            records.append(r)
    return records


# ---------------------------------------------------------------- 기기 업로드

class DeviceUploader:
    """auth → start → chunk×N → finish. tests/device_sim.DeviceSim 의 최소 이식."""

    def __init__(self, client: httpx.Client, serial: str, factory_token: str):
        self.client = client
        self.serial = serial
        self.factory_token = factory_token
        self.token: str | None = None

    def _headers(self) -> dict:
        import uuid as _uuid
        return {"Authorization": f"Bearer {self.token}",
                "X-Nonce": _uuid.uuid4().hex,
                "X-Timestamp": str(int(time.time()))}

    def auth(self) -> None:
        r = self.client.post("/v1/devices/auth",
                             json={"serial": self.serial,
                                   "factory_token": self.factory_token})
        if r.status_code != 200:
            raise SystemExit(f"기기 인증 실패 ({r.status_code}): {r.text[:200]}\n"
                             f"  → factory_token 이 맞는지 확인하거나, DB 접근이 가능한 곳"
                             f"(api 컨테이너 안)에서 인자 없이 실행해 재발급하세요.")
        self.token = r.json()["access_token"]

    def upload(self, scenario: list[tuple[str, float]], seed: int,
               chunk_size: int = 600) -> tuple[str, dict]:
        duration = int(sum(sec for _, sec in scenario))
        r = self.client.post("/v1/sessions/start", headers=self._headers(), json={
            "firmware_version": "seed-demo-0.1",
            "schema_version": "4.2",
            "session_mode": "LAB-5",
            "duration_sec": duration,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "upload_policy": "AFTER_SESSION_ONLY",
            "raw_video_uploaded": False,
            "research_mode": True,
            "glasses": None,
        })
        if r.status_code != 200:
            raise SystemExit(f"세션 start 실패 ({r.status_code}): {r.text[:200]}")
        sid = r.json()["session_id"]

        records = nan_to_none(make_records(scenario, seed))
        for ci, off in enumerate(range(0, len(records), chunk_size)):
            part = records[off:off + chunk_size]
            rc = self.client.post(f"/v1/sessions/{sid}/chunk", headers=self._headers(),
                                  json={"chunk_index": ci,
                                        "first_sample_index": part[0]["sample_index"],
                                        "crc32": records_crc32(part),
                                        "records": part})
            if rc.status_code != 200:
                raise SystemExit(f"chunk {ci} 실패 ({rc.status_code}): {rc.text[:200]}")

        canonical = nan_to_none(sorted(records, key=lambda r: r["sample_index"]))
        fin = self.client.post(f"/v1/sessions/{sid}/finish", headers=self._headers(),
                               json={"expected_samples": duration * SAMPLE_RATE_HZ,
                                     "session_crc": records_crc32(canonical)})
        if fin.status_code != 200:
            raise SystemExit(f"finish 실패 ({fin.status_code}): {fin.text[:200]}")
        return sid, fin.json()


# ---------------------------------------------------------------- 설정 해석

def resolve_factory_token(cli_value: str | None) -> str:
    """CLI > env > DB 재발급 순. DB 에는 해시만 있어 평문을 읽을 수는 없다."""
    token = cli_value or os.environ.get("SEED_FACTORY_TOKEN")
    if token:
        return token

    # DB 가 닿는 곳(api 컨테이너 안)이라면 재발급이 정석 — seed.py 가 문서화한 경로.
    try:
        from sqlalchemy import select

        from app.db.models import Device
        from app.db.session import make_session_factory
    except Exception as exc:                       # pragma: no cover - 방어
        raise SystemExit(f"DB 모듈 로드 실패: {exc}\n"
                         f"  → --factory-token 또는 SEED_FACTORY_TOKEN 을 지정하세요.")

    try:
        factory = make_session_factory()
        with factory() as db:
            device = db.execute(
                select(Device).where(Device.serial == SEED_DEVICE_SERIAL)
            ).scalar_one_or_none()
            if device is None:
                raise SystemExit(
                    f"기기 {SEED_DEVICE_SERIAL} 가 DB 에 없습니다 — 시드가 아직 안 돌았습니다.\n"
                    f"  → python -m app.db.seed 를 먼저 실행하세요.")
            new_token = secrets.token_hex(16)
            device.factory_token_hash = hashlib.sha256(
                new_token.encode("utf-8")).hexdigest()
            db.commit()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            f"DB 접근 실패: {exc}\n"
            f"  → 이 도구는 api 컨테이너 안에서 실행하는 것이 기본입니다. 밖에서 쓰려면\n"
            f"    --factory-token 또는 SEED_FACTORY_TOKEN 으로 평문 토큰을 넘기세요.")

    print(f"factory_token 재발급 완료 ({SEED_DEVICE_SERIAL}) — 해시만 DB 에 갱신")
    return new_token


def resolve_evolution_token(cli_value: str | None) -> str:
    token = cli_value or os.environ.get("EVOLUTION_TOKEN")
    if not token:
        try:
            from app.config import get_settings
            configured = get_settings().evolution_token
            if configured and configured != "change-me":
                token = configured
        except Exception:
            pass
    if not token:
        raise SystemExit(
            "EVOLUTION_TOKEN 이 없습니다.\n"
            "  → 컨테이너 안에서 실행하면 compose 가 주입합니다"
            " (docker-compose.cloud.yml 의 x-app-env).\n"
            "  → 밖에서 쓰려면 --evolution-token 또는 EVOLUTION_TOKEN 을 지정하세요.")
    return token


def segments_for(scenario) -> list[dict]:
    """시나리오 → 정답지 구간. 프로파일이 아니라 사람이 매길 라벨을 쓴다."""
    segs, t = [], 0.0
    for _, sec, label in scenario:
        segs.append({"t0": round(t, 1), "t1": round(t + sec, 1), "label": label})
        t += sec
    return segs


# ---------------------------------------------------------------- 메인

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="데모용 라벨된 LAB-5 세션 시딩 (자가진화 루프 가동 조건 충족)")
    p.add_argument("--server", default=os.environ.get("SEED_SERVER_URL", DEFAULT_SERVER),
                   help=f"운영 서버 URL (env SEED_SERVER_URL, 기본 {DEFAULT_SERVER})")
    p.add_argument("--sessions", type=int, default=18, help="업로드할 세션 수 (기본 18)")
    p.add_argument("--serial", default=os.environ.get("SEED_DEVICE_SERIAL"),
                   help=f"기기 시리얼 (기본: app.db.seed 정본 {SEED_DEVICE_SERIAL})")
    p.add_argument("--factory-token", default=None,
                   help="기기 factory_token 평문 (env SEED_FACTORY_TOKEN). "
                        "생략하면 DB 에서 재발급")
    p.add_argument("--evolution-token", default=None,
                   help="X-Evolution-Token (env EVOLUTION_TOKEN)")
    p.add_argument("--seed", type=int, default=7, help="합성 난수 시드 기준값 (기본 7)")
    p.add_argument("--min-train", type=int, default=TRAIN_LABEL_MIN,
                   help=f"이 수보다 train 라벨이 적으면 종료코드 2 (기본 {TRAIN_LABEL_MIN})")
    p.add_argument("--dry-run", action="store_true",
                   help="네트워크 호출 없이 실행 계획만 출력")
    return p


def main() -> int:
    args = build_parser().parse_args()
    serial = args.serial or SEED_DEVICE_SERIAL

    if args.sessions < 1:
        print("--sessions 는 1 이상이어야 합니다.", file=sys.stderr)
        return 1

    print(f"대상 서버 : {args.server}")
    print(f"기기      : {serial}")
    print(f"세션 수   : {args.sessions} (시나리오 {len(SCENARIOS)}종 순환, 각 5분/3000샘플)")

    if args.dry_run:
        print("\n[dry-run] 실제 업로드 없이 계획만 출력합니다.")
        for i in range(args.sessions):
            scenario = SCENARIOS[i % len(SCENARIOS)]
            plan = " ".join(f"{name}:{sec}s" for name, sec, _ in scenario)
            print(f"  [{i + 1}/{args.sessions}] seed={args.seed + i} {plan}")
        print("\n[dry-run] split(train/holdout)은 서버가 발급한 session_id 해시로 정해지므로"
              " 업로드 전에는 알 수 없습니다.")
        print("[dry-run] 토큰은 조회하지 않았습니다.")
        return 0

    evo_token = resolve_evolution_token(args.evolution_token)
    factory_token = resolve_factory_token(args.factory_token)

    client = httpx.Client(base_url=args.server, timeout=120)
    uploader = DeviceUploader(client, serial, factory_token)
    uploader.auth()
    print(f"기기 인증 OK ({serial})\n")

    uploaded = []
    for i in range(args.sessions):
        scenario = SCENARIOS[i % len(SCENARIOS)]
        sid, fin = uploader.upload([(name, sec) for name, sec, _ in scenario],
                                   seed=args.seed + i)
        sp = split_for(sid)
        uploaded.append({"sid": sid, "split": sp,
                         "segments": segments_for(scenario),
                         "protocol": f"기본5분 변형{i % len(SCENARIOS) + 1}"})
        print(f"  [{i + 1}/{args.sessions}] {sid} split={sp} "
              f"count_match={fin.get('count_match')}")

    n_train = sum(1 for u in uploaded if u["split"] == "train")
    n_holdout = sum(1 for u in uploaded if u["split"] == "holdout")
    print(f"\n업로드 완료: {len(uploaded)}세션 (train {n_train} / holdout {n_holdout})")

    # finish 직후 upload_state 가 complete 로 확정될 시간을 준다 — 라벨 POST 의 전제.
    print("upload_state=complete 확정 대기…")
    time.sleep(3)

    headers = {"X-Evolution-Token": evo_token}
    ok = skip = fail = 0
    for u in uploaded:
        rr = client.post(f"/v1/evolution/sessions/{u['sid']}/labels",
                         json={"labeler": "admin", "method": "realtime_instructed",
                               "protocol": u["protocol"], "segments": u["segments"]},
                         headers=headers, timeout=30)
        if rr.status_code == 200:
            ok += 1
        elif rr.status_code == 409:
            # 라벨은 불변 — 이미 있으면 그대로 둔다(재실행 안전).
            skip += 1
        else:
            fail += 1
            print(f"  ! {u['sid']}: {rr.status_code} {rr.text[:160]}")
    print(f"정답지 POST: 신규 {ok} / 기존유지 {skip} / 실패 {fail}")

    ov = client.get("/v1/evolution/overview", headers=headers, timeout=15)
    if ov.status_code != 200:
        print(f"overview 조회 실패 ({ov.status_code}): {ov.text[:200]}", file=sys.stderr)
        return 1
    o = ov.json()
    print(f"\n=== 최종 상태 ===")
    print(f"sessions_total   : {o['sessions_total']}")
    print(f"labeled_realtime : {o['labeled_realtime']}")
    print(f"train_labeled    : {o['train_labeled']}")
    print(f"holdout_labeled  : {o['holdout_labeled']}")

    if fail:
        print(f"\n실패한 라벨 POST 가 {fail}건 있습니다.", file=sys.stderr)
        return 1
    if o["train_labeled"] < args.min_train:
        print(f"\ntrain_labeled({o['train_labeled']}) < {args.min_train} "
              f"— 진화 루프를 돌리기에 부족합니다. --sessions 를 늘려 다시 실행하세요.",
              file=sys.stderr)
        return 2
    print(f"\ntrain 라벨 {o['train_labeled']}건 — 자가진화 실행 조건을 만족합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
