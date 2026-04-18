from datetime import datetime

from sqlalchemy import BigInt, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings

db_url = settings.database_url.replace("postgres://", "postgresql+asyncpg://")

engine = create_async_engine(db_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class UserSkill(Base):
    __tablename__ = "user_skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    userId: Mapped[int] = mapped_column(BigInt)
    skillName: Mapped[str] = mapped_column(String)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    lastLesson: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("userId", "skillName", name="_user_skill_uc"),)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
