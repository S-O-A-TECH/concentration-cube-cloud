"""SQLAlchemy Declarative Base — 모든 모델의 뿌리. Alembic env.py 가 이 metadata 를 바라본다."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
