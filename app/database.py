from datetime import datetime

from sqlalchemy import BIGINT, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings

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


class UserSkill(Base):
    __tablename__ = "user_skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    userId: Mapped[int] = mapped_column(BIGINT)
    skillName: Mapped[str] = mapped_column(String)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    lastLesson: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("userId", "skillName", name="_user_skill_uc"),)


class NotionPage(Base):
    """Хранит Notion page_id для каждого пользователя."""
    __tablename__ = "notion_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    userId: Mapped[int] = mapped_column(BIGINT, unique=True)
    pageId: Mapped[str] = mapped_column(String)          # корневая страница пользователя
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # дата первого урока — от неё считаем 30-дневный trial
    trialStartedAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # предупреждение уже отправлено
    warningSent: Mapped[bool] = mapped_column(Boolean, default=False)


class UserPremium(Base):
    """Статус премиума пользователя."""
    __tablename__ = "user_premium"

    id: Mapped[int] = mapped_column(primary_key=True)
    userId: Mapped[int] = mapped_column(BIGINT, unique=True)
    isPremium: Mapped[bool] = mapped_column(Boolean, default=False)
    premiumUntil: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    activatedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)