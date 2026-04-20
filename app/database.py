from datetime import datetime

from sqlalchemy import (
    BIGINT, JSON, Boolean, DateTime, Float, ForeignKey, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings

# ─────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────

_raw_url = settings.database_url
if _raw_url.startswith("postgres://"):
    db_url = _raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif _raw_url.startswith("postgresql://"):
    db_url = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    db_url = _raw_url

engine = create_async_engine(db_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
# Трек = Направление (Java Senior, English B2+...)
# ─────────────────────────────────────────────

class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BIGINT, index=True)           # owner
    name: Mapped[str] = mapped_column(String)                          # "Java Senior"
    description: Mapped[str] = mapped_column(Text)                     # о чём трек
    where_applied: Mapped[str] = mapped_column(Text)                   # где применяется
    final_skills: Mapped[list] = mapped_column(JSON, default=list)     # ["X", "Y"]
    curriculum_json: Mapped[dict] = mapped_column(JSON, default=dict)  # план целиком
    total_topics: Mapped[int] = mapped_column(Integer, default=0)
    notion_page_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    blocks: Mapped[list["Block"]] = relationship(back_populates="track", cascade="all, delete-orphan")


class Block(Base):
    __tablename__ = "blocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    order_num: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String)
    notion_page_id: Mapped[str | None] = mapped_column(String, nullable=True)

    track: Mapped["Track"] = relationship(back_populates="blocks")
    topics: Mapped[list["Topic"]] = relationship(back_populates="block", cascade="all, delete-orphan")


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("blocks.id", ondelete="CASCADE"))
    order_num: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String)
    # content_json = {"theory": [...], "questions": [...]} — null пока не сгенерирован
    content_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notion_written: Mapped[bool] = mapped_column(Boolean, default=False)

    block: Mapped["Block"] = relationship(back_populates="topics")


# ─────────────────────────────────────────────
# Прогресс пользователя
# ─────────────────────────────────────────────

class UserTrack(Base):
    """Привязка пользователя к его трекам + общий прогресс."""
    __tablename__ = "user_tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BIGINT, index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String, default="learn")  # 'learn' | 'assessed'
    last_activity: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "track_id", name="_user_track_uc"),)


class UserTopicProgress(Base):
    """Статус и результат пользователя по конкретной теме."""
    __tablename__ = "user_topic_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BIGINT, index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String, default="locked")  # locked | available | passed
    score_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    passed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String, default="learn")  # learn | exam | assessment

    __table_args__ = (UniqueConstraint("user_id", "topic_id", name="_user_topic_uc"),)


class BlockExam(Base):
    """Результаты экзамена по блоку."""
    __tablename__ = "block_exams"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BIGINT, index=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("blocks.id", ondelete="CASCADE"))
    score_pct: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Assessment(Base):
    """Результаты 'Оценить свой уровень'."""
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BIGINT, index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    score_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String)  # beginner | middle | senior
    # топик с которого начинать (все темы до него помечены как passed)
    start_topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"), nullable=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# Оставляем как было (с небольшими правками naming'а)
# ─────────────────────────────────────────────

class NotionPage(Base):
    __tablename__ = "notion_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BIGINT, unique=True)
    page_id: Mapped[str] = mapped_column(String)
    trial_started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    warning_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserPremium(Base):
    __tablename__ = "user_premium"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BIGINT, unique=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    premium_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)