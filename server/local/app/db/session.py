"""엔진·세션 팩토리 — API(S2)와 잡(S3)이 공유한다.

잡 계층이 호출마다 새 엔진을 만드는 것은 의도된 동작: RQ 는 잡마다 fork 한
work-horse 프로세스에서 실행·종료되므로 엔진 수명이 잡 수명과 같다
(부모 프로세스의 엔진을 fork 로 공유하면 오히려 커넥션 오염 위험).
API 프로세스는 deps._session_factory 가 lru_cache 로 1회만 만든다.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def make_engine(url: str | None = None):
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(url), expire_on_commit=False)
