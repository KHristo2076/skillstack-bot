import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert
from telegram import Update

from app.bot import bot_service
from app.database import AsyncSessionLocal, UserSkill

logger = logging.getLogger(__name__)
router = APIRouter()


class SkillData(BaseModel):
    user_id: int
    skill: str
    action: str = "select"


@router.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot_service.application.bot)
        await bot_service.application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error"}


@router.post("/save-skill")
async def save_skill(data: SkillData):
    async with AsyncSessionLocal() as session:
        stmt = insert(UserSkill).values(
            userId=data.user_id,
            skillName=data.skill,
            progress=12 if data.action == "complete_lesson" else 0,
            streak=1 if data.action == "complete_lesson" else 0,
            lastLesson=datetime.utcnow(),
        )

        if data.action == "complete_lesson":
            stmt = stmt.on_conflict_do_update(
                index_elements=["userId", "skillName"],
                set_={
                    "progress": UserSkill.progress + 25,
                    "streak": UserSkill.streak + 1,
                    "lastLesson": datetime.utcnow(),
                },
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["userId", "skillName"])

        await session.execute(stmt)
        await session.commit()

    logger.info(f"Сохранён навык {data.skill} для пользователя {data.user_id}")
    return {"status": "success"}


@router.get("/app")
async def serve_miniapp():
    return FileResponse("static/index.html")


@router.get("/")
async def health():
    return {"status": "✅ SkillStack Bot + DB is running!"}
