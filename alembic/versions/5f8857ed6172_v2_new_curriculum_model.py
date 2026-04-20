"""v2 new curriculum model

Revision ID: 5f8857ed6172
Revises: 
Create Date: 2026-04-20 17:46:21.897936

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5f8857ed6172'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── 1. Дропаем старые таблицы (если есть) ───
    op.execute("DROP TABLE IF EXISTS user_skills CASCADE")
    op.execute("DROP TABLE IF EXISTS notion_pages CASCADE")
    op.execute("DROP TABLE IF EXISTS user_premium CASCADE")

    # ─── 2. tracks ───
    op.create_table(
        "tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BIGINT(), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("where_applied", sa.Text(), nullable=False),
        sa.Column("final_skills", sa.JSON(), nullable=False),
        sa.Column("curriculum_json", sa.JSON(), nullable=False),
        sa.Column("total_topics", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notion_page_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # ─── 3. blocks ───
    op.create_table(
        "blocks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_num", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("notion_page_id", sa.String(), nullable=True),
    )

    # ─── 4. topics ───
    op.create_table(
        "topics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("block_id", sa.Integer(), sa.ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_num", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=True),
        sa.Column("notion_written", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # ─── 5. user_tracks ───
    op.create_table(
        "user_tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BIGINT(), nullable=False, index=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("progress_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mode", sa.String(), nullable=False, server_default="learn"),
        sa.Column("last_activity", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "track_id", name="_user_track_uc"),
    )

    # ─── 6. user_topic_progress ───
    op.create_table(
        "user_topic_progress",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BIGINT(), nullable=False, index=True),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="locked"),
        sa.Column("score_pct", sa.Float(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="learn"),
        sa.UniqueConstraint("user_id", "topic_id", name="_user_topic_uc"),
    )

    # ─── 7. block_exams ───
    op.create_table(
        "block_exams",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BIGINT(), nullable=False, index=True),
        sa.Column("block_id", sa.Integer(), sa.ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score_pct", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("taken_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # ─── 8. assessments ───
    op.create_table(
        "assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BIGINT(), nullable=False, index=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score_pct", sa.Float(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("start_topic_id", sa.Integer(), sa.ForeignKey("topics.id"), nullable=True),
        sa.Column("taken_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # ─── 9. notion_pages (пересоздаём с новым именованием полей) ───
    op.create_table(
        "notion_pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BIGINT(), nullable=False, unique=True),
        sa.Column("page_id", sa.String(), nullable=False),
        sa.Column("trial_started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("warning_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # ─── 10. user_premium (пересоздаём) ───
    op.create_table(
        "user_premium",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BIGINT(), nullable=False, unique=True),
        sa.Column("is_premium", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("premium_until", sa.DateTime(), nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    # Откат: дропаем всё v2-шное. Старые таблицы не восстанавливаем (они и так пустые были)
    op.drop_table("user_premium")
    op.drop_table("notion_pages")
    op.drop_table("assessments")
    op.drop_table("block_exams")
    op.drop_table("user_topic_progress")
    op.drop_table("user_tracks")
    op.drop_table("topics")
    op.drop_table("blocks")
    op.drop_table("tracks")

