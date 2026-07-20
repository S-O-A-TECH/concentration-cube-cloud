"""S1 DoD: Alembic downgrade base && upgrade head 왕복 무오류 (실 PG 필요).

PG 컨테이너가 없으면 skip — 나머지 테스트는 sqlite 로 이미 커버.
운영 DB(cube)를 건드리지 않도록 스크래치 DB(cube_mig_test)에서 왕복한다.
"""
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import get_settings

SCRATCH_DB = "cube_mig_test"


def _admin_engine():
    base = get_settings().database_url.rsplit("/", 1)[0]
    return create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT",
                         connect_args={"connect_timeout": 2})


def test_migration_roundtrip():
    try:
        admin = _admin_engine()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS {SCRATCH_DB}'))
            conn.execute(text(f'CREATE DATABASE {SCRATCH_DB}'))
    except Exception:
        pytest.skip("PostgreSQL 미가동 — compose 로 postgres 를 띄운 뒤 실행하세요")

    base = get_settings().database_url.rsplit("/", 1)[0]
    scratch_url = f"{base}/{SCRATCH_DB}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    try:
        command.upgrade(cfg, "head")
        scratch = create_engine(scratch_url)
        tables = set(inspect(scratch).get_table_names())
        scratch.dispose()
        expected = {"users", "profiles", "devices", "sessions", "param_sets",
                    "scoring_runs", "promoted_results", "labels",
                    "intervention_events", "intervention_effectiveness",
                    "subscriptions", "audit_log"}
        assert expected <= tables, f"누락 테이블: {expected - tables}"

        command.downgrade(cfg, "base")
        scratch = create_engine(scratch_url)
        remaining = set(inspect(scratch).get_table_names()) - {"alembic_version"}
        scratch.dispose()
        assert remaining == set(), f"downgrade 후 잔존 테이블: {remaining}"

        command.upgrade(cfg, "head")      # 왕복 재상승
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)'))
        admin.dispose()
