"""S11: 수면세션 긴 버전(5분) 지원 — 릴리즈 protocol 구분 + 프로토콜 시퀀스 편집 테이블.

- relax_voice_releases.protocol('short' 기본 | 'long'): 버전별 독립 배포.
- enhance_protocol_configs(key/config_json/updated_at): 긴 버전 시퀀스(단계 길이·음악)의
  서버 정본. 앱은 GET /v1/app/enhance/relax_long_protocol 로 pull, 콘솔에서 편집.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c5e1f0a72b93"
down_revision: Union[str, None] = "b3f7c1a9d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "relax_voice_releases",
        sa.Column("protocol",
                  sa.Enum("short", "long", name="relax_voice_protocol",
                          native_enum=False, length=24),
                  nullable=False, server_default="short"),
    )
    op.create_table(
        "enhance_protocol_configs",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("config_json",
                  sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()),
                                         "postgresql"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("enhance_protocol_configs")
    op.drop_column("relax_voice_releases", "protocol")
