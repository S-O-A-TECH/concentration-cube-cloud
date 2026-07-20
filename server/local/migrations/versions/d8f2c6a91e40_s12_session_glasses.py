"""s12: sessions.glasses — 안경 착용 여부(기기 자동 감지, null=미상)

기술문서 v4.1 의 "eyeglass flag" 를 v4.2 세션 계약으로 운반한다(2026-07-08).
진화 분석의 그룹 축 + QC 해석용 메타데이터 — 채점 로직 분기에는 쓰지 않는다.
"""
from alembic import op
import sqlalchemy as sa

revision = "d8f2c6a91e40"
down_revision = "c5e1f0a72b93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("glasses", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "glasses")
