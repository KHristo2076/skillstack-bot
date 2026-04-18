import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select
from telegram import Update
import json
import re

from groq import AsyncGroq

from app.bot import bot_service
from app.database import AsyncSessionLocal, UserSkill
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

groq_client = AsyncGroq(api_key=settings.groq_api_key)


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


@router.post("/start-lesson")
async def start_lesson(data: dict):
    """Генерируем урок через Groq AI"""
    skill = data.get("skill")

    prompt = f"""
Ты — лучший преподаватель микро-уроков. Создай короткий урок (7–10 минут) по навыку: {skill}.

Структура урока:
1. Теория (3–4 коротких пункта)
2. 3 вопроса с 4 вариантами ответов каждый (укажи правильный вариант)

Ответ строго в JSON:
{{
  "title": "Урок по {skill}",
  "theory": ["пункт1", "пункт2", "пункт3"],
  "questions": [
    {{"text": "вопрос1", "options": ["A", "B", "C", "D"], "correct": 0}},
    {{"text": "вопрос2", "options": ["A", "B", "C", "D"], "correct": 2}},
    {{"text": "вопрос3", "options": ["A", "B", "C", "D"], "correct": 1}}
  ]
}}
"""

    try:
        chat_completion = await groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1200,
        )
        raw = chat_completion.choices[0].message.content
        # Strip markdown code fences if model wraps JSON in ```json ... ```
        match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
        lesson_json = match.group(1) if match else raw.strip()
        lesson = json.loads(lesson_json)
        return lesson
    except Exception as e:
        logger.error(f"AI error: {e}")
        # fallback
        return {
            "title": f"Урок по {skill}",
            "theory": ["AI временно недоступен", "Попробуй позже"],
            "questions": []
        }


@router.get("/my-skills")
async def get_my_skills(user_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserSkill).where(UserSkill.userId == user_id))
        skills = result.scalars().all()
        return {"skills": [{"skill": s.skillName, "progress": s.progress, "streak": s.streak} for s in skills]}


@router.get("/app")
async def serve_miniapp():
    return FileResponse("static/index.html")


@router.get("/")
async def health():
    return {"status": "✅ SkillStack Bot + AI is running!"}