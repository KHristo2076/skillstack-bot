import logging
from datetime import datetime
from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from telegram import Update
from prisma import Prisma

from app.bot import bot_service

logger = logging.getLogger(__name__)
router = APIRouter()

# Создаем объект базы данных
db = Prisma()

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
    # Логика из гайда: создаем или обновляем навык в базе
    skill = await db.userskill.upsert(
        where={"userId_skillName": {"userId": data.user_id, "skillName": data.skill}},
        data={
            "create": {
                "userId": data.user_id,
                "skillName": data.skill,
                "progress": 12 if data.action == "complete_lesson" else 0,
                "streak": 1
            },
            "update": {
                "progress": {"increment": 25} if data.action == "complete_lesson" else {},
                "lastLesson": datetime.now(),
                "streak": {"increment": 1} if data.action == "complete_lesson" else {}
            }
        }
    )
    logger.info(f"Сохранён навык {data.skill} для пользователя {data.user_id}")
    return {"status": "success"}

@router.get("/app")
async def serve_miniapp():
    return FileResponse("static/index.html")

@router.get("/")
async def health():
    return {"status": "✅ SkillStack Bot + DB is running!"}