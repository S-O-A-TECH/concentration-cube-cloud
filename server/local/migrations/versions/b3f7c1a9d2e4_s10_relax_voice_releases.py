"""S10: 수면세션 음성 배포 릴리즈 테이블 (relax_voice_releases).

운영자가 /ops 에서 단계별 대사 mp3 를 업로드하고 배포일을 정해 배포하면 앱이
그 음성을 내려받아 폰 TTS 대신 재생한다. status(draft|published)/release_at(KST
배포일 0시를 UTC 로 저장)/files(JSON: {slot: 상대경로}) 로 상태를 표현한다.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b3f7c1a9d2e4"
down_revision: Union[str, None] = "7f2a91c04b1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "relax_voice_releases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status",
                  sa.Enum("draft", "published", name="relax_voice_status",
                          native_enum=False, length=24), nullable=False),
        sa.Column("release_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("files",
                  sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()),
                                         "postgresql"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("relax_voice_releases")
