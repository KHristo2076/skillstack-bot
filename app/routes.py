import logging
from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from telegram import Update

from app.bot import bot_service

logger = logging.getLogger(__name__)

router = APIRouter()

user_skills: Dict[int, list] = {}


class SkillData(BaseModel):
    user_id: int
    skill: str
    action: str


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
    if data.user_id not in user_skills:
        user_skills[data.user_id] = []
    if data.skill not in user_skills[data.user_id]:
        user_skills[data.user_id].append(data.skill)
    logger.info(f"Сохранён навык {data.skill} для пользователя {data.user_id}")
    return {"status": "success", "skills": user_skills[data.user_id]}


@router.get("/app")
async def serve_miniapp():
    return FileResponse("static/index.html")


@router.get("/")
async def health():
    return {"status": "✅ SkillStack Bot + Mini App is running!"}
