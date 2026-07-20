"""S9: 구독에 country(구매 국가)·months(기간) 추가 — 대시보드 국가별 드릴다운.

country 원천: Google Play RTDN 검증(purchases.subscriptionsv2)의 regionCode.
null = 국가 미상(수동 부여·테스트) → 대시보드 '테스트' 버킷으로 집계.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f2a91c04b1e"
down_revision: Union[str, None] = "01caa43c3bc5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("country", sa.String(2), nullable=True))
    op.add_column("subscriptions", sa.Column("months", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "months")
    op.drop_column("subscriptions", "country")
