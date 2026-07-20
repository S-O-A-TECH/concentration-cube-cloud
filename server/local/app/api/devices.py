"""기기 인증 — POST /v1/devices/auth (SPEC-02 §1.1, §2.1)."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (DEVICE_TOKEN_TTL_SEC, get_db, make_device_token,
                          require_device)
from app.db.models import Device, ParamSet

router = APIRouter(prefix="/v1/devices", tags=["devices"])


class DeviceAuthIn(BaseModel):
    serial: str
    factory_token: str


@router.post("/auth")
def device_auth(body: DeviceAuthIn, db: Session = Depends(get_db)) -> dict:
    device = db.execute(
        select(Device).where(Device.serial == body.serial)
    ).scalar_one_or_none()
    token_hash = hashlib.sha256(body.factory_token.encode("utf-8")).hexdigest()
    # 존재 여부를 구분해 알려주지 않는다 (serial 열거 방지)
    if device is None or device.factory_token_hash != token_hash:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return {
        "access_token": make_device_token(device),
        "token_type": "bearer",
        "expires_in": DEVICE_TOKEN_TTL_SEC,
        "device_id": device.serial,
    }


# ── 간이 판정·넛지 파라미터 배포 (2026-07-08 결정) ──────────────────────────
# 기기는 세션 시작 시 이걸 pull 한다 — 판정법 진화(파라미터 채택)가 **펌웨어
# 업데이트 없이** 기기의 실시간 간이 판정·개입(차임)까지 따라가게 하는 통로.
# 채점(서버 정본)은 틀려도 소급 재채점이 되지만 개입(소리)은 되돌릴 수 없다 —
# 그래서 넛지 값에는 서버가 클램프를 강제한다(진화가 어떤 값을 채택해도
# 아이를 과잉 방해할 수 없게). 기본값은 2026-07-08 원장 확정: 연속 이탈 60초
# → 부드러운 차임, 쿨다운 120초, 세션 최대 3회(멍때림은 트리거 제외 — 리포트 전용).
NUDGE_DEFAULTS = {"offtask_sec": 60, "cooldown_sec": 120, "max_per_session": 3}
NUDGE_CLAMPS = {"offtask_sec": (20, 600), "cooldown_sec": (60, 1800),
                "max_per_session": (0, 5)}


@router.get("/params")
def device_params(device: Device = Depends(require_device),
                  db: Session = Depends(get_db)) -> dict:
    ps = db.execute(
        select(ParamSet).where(ParamSet.status == "adopted")
        .order_by(ParamSet.adopted_at.desc(), ParamSet.id.desc())
    ).scalars().first()
    params = dict(ps.json_params) if ps else {}
    nudge = {**NUDGE_DEFAULTS, **(params.get("nudge") or {})}
    for k, (lo, hi) in NUDGE_CLAMPS.items():
        nudge[k] = max(lo, min(hi, int(nudge[k])))
    return {
        "param_set_version": ps.version if ps else None,
        "engine_version": ps.engine_version if ps else None,
        # 실시간 간이 판정에 필요한 부분집합만 — 서버 채점 전용 값은 내려보내지 않음
        "live": {"gaze": params.get("gaze") or {}},
        "nudge": nudge,
    }
