"""S1 DoD: 모델 CRUD 스모크 + 시드 멱등 + 활성 param_set 조회 (sqlite in-memory).

모델이 sqlite 호환 타입(Uuid/JSON variant/native_enum=False)으로 정의되어 있어
PG 없이도 스키마 생성·CRUD 가 그대로 돈다. PG 전용 왕복은 test_migrations.py.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import models
from app.db.base import Base
from app.db.seed import seed
from app.services.param_sets import get_active_param_set


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def test_crud_smoke(db):
    device = models.Device(serial="DEV_TEST_001", research_flag=True)
    db.add(device)
    db.flush()

    session = models.StudySession(device_id=device.id, mode="SFI-20", split="train")
    db.add(session)
    db.flush()

    ps = models.ParamSet(version="v0.9-test", json_params={"k": 1}, origin="manual",
                         status="adopted", adopted_at=datetime.now(timezone.utc))
    db.add(ps)
    db.flush()

    run = models.ScoringRun(session_id=session.id, param_set_id=ps.id, sfi=77.5,
                            timeline_json=[{"t0": 0, "t1": 60, "state": "focus"}])
    db.add(run)
    db.flush()

    db.add(models.PromotedResult(session_id=session.id, scoring_run_id=run.id))
    db.add(models.Label(session_id=session.id, labeler="admin",
                        segments_json=[{"t0": 0, "t1": 120, "label": "focus"}]))
    db.commit()

    fetched = db.execute(
        select(models.StudySession).where(models.StudySession.id == session.id)
    ).scalar_one()
    assert isinstance(fetched.id, uuid.UUID)
    assert fetched.upload_state == "open"          # default
    promoted = db.get(models.PromotedResult, session.id)
    assert promoted.scoring_run_id == run.id


def test_seed_idempotent_and_active_param_set(db, capsys):
    created_first = seed(db)
    assert created_first.get("param_set") == "v1.0"
    assert "factory_token_plaintext" in created_first

    created_again = seed(db)                        # 멱등 — 두 번째는 아무것도 안 만든다
    assert created_again == {}

    active = get_active_param_set(db)
    assert active is not None
    assert active.version == "v1.0"
    assert active.origin == "seed"
    assert active.json_params["sfi_weights"]["gaze_on_page"] == 35


def test_seed_params_pass_schema_validation(db):
    """json_params 는 pydantic 스키마 검증을 통과한 값만 저장된다 (S1 리스크 대응)."""
    from app.services.param_schema import validate_params
    import pydantic

    seed(db)
    active = get_active_param_set(db)
    validate_params(active.json_params)             # 재검증 무오류

    with pytest.raises(pydantic.ValidationError):
        validate_params({**active.json_params, "unknown_section": {}})
