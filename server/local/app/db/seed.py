"""멱등 시드 — S1 구현계획 §1.4.

- param_sets: v1.0 (웹캠 프로토 focus_scoring/params/default.json 스냅샷,
  origin=seed, status=adopted) — json_params 는 pydantic 검증 후 저장.
- devices: WEBCAM_PROTO_001 (research_flag=true) — factory_token 은 생성 시
  sha256 해시만 저장하고 평문은 이때 1회만 출력한다 (분실 시 재발급 = 해시 갱신).

실행: python -m app.db.seed   (컨테이너 기동 시 compose command 에서 자동 실행)
멱등: 이미 있으면 건드리지 않는다 — 재실행 안전.
"""
import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Device, EnhanceProtocolConfig, ParamSet
from app.db.session import make_session_factory
from app.services.param_schema import validate_params
from app.services.relax_voice import (DEFAULT_RELAX_LONG_PROTOCOL,
                                      RELAX_LONG_PROTOCOL_KEY)

PARAMS_PATH = Path(__file__).resolve().parents[1] / "focus_scoring" / "params" / "default.json"
SEED_PARAM_VERSION = "v1.0"
SEED_DEVICE_SERIAL = "WEBCAM_PROTO_001"


def seed(db: Session) -> dict:
    """시드 실행. 새로 만든 것만 결과 dict 에 담아 반환 (factory_token 평문 포함)."""
    created: dict = {}

    if db.execute(select(ParamSet).where(ParamSet.version == SEED_PARAM_VERSION)).scalar_one_or_none() is None:
        raw = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
        db.add(ParamSet(
            version=SEED_PARAM_VERSION,
            engine_version="builtin",
            json_params=validate_params(raw),
            origin="seed",
            rationale="웹캠 프로토 params/default.json 시드 (S1)",
            status="adopted",
            adopted_at=datetime.now(timezone.utc),
            decided_by="seed",
        ))
        created["param_set"] = SEED_PARAM_VERSION

    if db.execute(select(Device).where(Device.serial == SEED_DEVICE_SERIAL)).scalar_one_or_none() is None:
        token = secrets.token_hex(16)
        db.add(Device(
            serial=SEED_DEVICE_SERIAL,
            hw_rev="webcam-proto",
            fw_ver="7A",
            factory_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            research_flag=True,
        ))
        created["device"] = SEED_DEVICE_SERIAL
        created["factory_token_plaintext"] = token

    # 긴 버전 수면 프로토콜(시퀀스·음악) 정본 — 앱 내장 relax_long_v1.json 과 동일.
    # 멱등: 운영자가 콘솔에서 편집한 값을 덮어쓰지 않는다(없을 때만 시드).
    if db.get(EnhanceProtocolConfig, RELAX_LONG_PROTOCOL_KEY) is None:
        db.add(EnhanceProtocolConfig(key=RELAX_LONG_PROTOCOL_KEY,
                                     config_json=DEFAULT_RELAX_LONG_PROTOCOL))
        created["enhance_protocol"] = RELAX_LONG_PROTOCOL_KEY

    db.commit()
    return created


def seed_demo(db: Session) -> dict:
    """데모 계정·프로필 시드 — 운영 콘솔 화면 검증용 (S6 리스크 표). 멱등.

    운영 DB 자동 시드에는 포함되지 않는다 — `python -m app.db.seed --demo` 로만.
    """
    from app.db.models import Profile, User
    created: dict = {}
    if db.execute(select(User).where(User.email == "demo@example.com")).scalar_one_or_none() is None:
        user = User(email="demo@example.com", auth_provider="google")
        db.add(user)
        db.flush()
        db.add(Profile(user_id=user.id, nickname="데모학생",
                       birth_date=datetime(2013, 3, 1, tzinfo=timezone.utc).date()))
        db.add(Profile(user_id=user.id, nickname="데모동생",
                       birth_date=datetime(2015, 7, 15, tzinfo=timezone.utc).date()))
        created["demo_user"] = "demo@example.com (프로필 2개)"
    db.commit()
    return created


def main() -> None:
    import sys
    factory = make_session_factory()
    with factory() as db:
        created = seed(db)
        if "--demo" in sys.argv:
            created.update(seed_demo(db))
    if not created:
        print("seed: 변경 없음 (이미 시드됨)")
        return
    if "param_set" in created:
        print(f"seed: param_set {created['param_set']} (adopted) 생성")
    if "device" in created:
        print(f"seed: device {created['device']} 생성")
        print(f"seed: FACTORY_TOKEN (1회 출력 — 웹캠 프로토 .env 에 보관): {created['factory_token_plaintext']}")
    if "demo_user" in created:
        print(f"seed: 데모 계정 {created['demo_user']} 생성")


if __name__ == "__main__":
    main()
