import logging
import json
import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select

from anthropic import AsyncAnthropic
from telegram import Update

from app.bot import bot_service
from app.database import AsyncSessionLocal, UserSkill
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

# ─────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────

def determine_level(progress: int, streak: int) -> dict:
    """Определяем уровень и стиль урока по прогрессу пользователя."""
    if progress == 0 and streak == 0:
        return {
            "label": "новичок",
            "theory_style": "объясняй очень просто, используй аналогии из повседневной жизни, избегай сложных терминов",
            "question_style": "базовые вопросы на понимание простых концепций",
            "lesson_number_hint": "это ПЕРВЫЙ урок пользователя — начни с самых основ",
        }
    elif progress < 30:
        return {
            "label": "начинающий",
            "theory_style": "объясняй с нуля, но можно вводить термины с пояснением",
            "question_style": "вопросы на понимание определений и базовых концепций",
            "lesson_number_hint": f"пройдено {streak} уроков — строй на предыдущих знаниях",
        }
    elif progress < 65:
        return {
            "label": "средний",
            "theory_style": "объясняй практически, с примерами из реального применения, можно использовать термины",
            "question_style": "вопросы на применение знаний, выбор правильного подхода",
            "lesson_number_hint": f"прогресс {progress}% — углубляй и расширяй знания",
        }
    else:
        return {
            "label": "продвинутый",
            "theory_style": "углублённые концепции, нюансы, edge cases, best practices",
            "question_style": "сложные вопросы на понимание тонкостей и профессиональный выбор",
            "lesson_number_hint": f"высокий прогресс {progress}% — фокус на мастерстве и деталях",
        }


def safe_parse_json(raw: str) -> dict:
    """Надёжный парсер JSON — находит объект даже если вокруг мусор."""
    # Убираем markdown-обёртку если есть
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()

    # Ищем первый { и последний }
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1

    if start == -1 or end <= start:
        raise ValueError(f"JSON-объект не найден в ответе модели. Raw: {raw[:200]}")

    json_str = cleaned[start:end]
    return json.loads(json_str)


def validate_lesson(lesson: dict) -> bool:
    """Проверяем что урок имеет нужную структуру."""
    required = ["title", "theory", "questions"]
    for key in required:
        if key not in lesson:
            return False
    if not isinstance(lesson["theory"], list) or len(lesson["theory"]) < 2:
        return False
    if not isinstance(lesson["questions"], list) or len(lesson["questions"]) < 1:
        return False
    for q in lesson["questions"]:
        if not all(k in q for k in ["text", "options", "correct"]):
            return False
    return True


# ─────────────────────────────────────────────
# Модели данных
# ─────────────────────────────────────────────

class SkillData(BaseModel):
    user_id: int
    skill: str
    action: str = "select"


class ChatMessage(BaseModel):
    user_id: int
    skill: str
    question: str
    lesson_context: str = ""


# ─────────────────────────────────────────────
# Роуты
# ─────────────────────────────────────────────

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
            progress=0,
            streak=0,
            lastLesson=datetime.utcnow(),
        )

        if data.action == "complete_lesson":
            stmt = stmt.on_conflict_do_update(
                index_elements=["userId", "skillName"],
                set_={
                    "progress": UserSkill.progress + 12,  # +12% за урок
                    "streak": UserSkill.streak + 1,
                    "lastLesson": datetime.utcnow(),
                },
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["userId", "skillName"])

        await session.execute(stmt)
        await session.commit()

    logger.info(f"Сохранён навык '{data.skill}' для пользователя {data.user_id}")
    return {"status": "success"}


@router.post("/start-lesson")
async def start_lesson(data: dict):
    """
    Генерирует персонализированный урок через Claude API.
    Учитывает прогресс пользователя, уровень, количество пройденных уроков.
    """
    skill = data.get("skill", "")
    user_id = data.get("user_id")

    if not skill:
        return {"error": "Навык не указан"}

    # ── Шаг 1: получаем прогресс из БД ──────────────────
    user_progress = 0
    user_streak = 0

    if user_id:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(UserSkill).where(
                        UserSkill.userId == user_id,
                        UserSkill.skillName == skill,
                    )
                )
                row = result.scalar_one_or_none()
                if row:
                    user_progress = row.progress
                    user_streak = row.streak
        except Exception as e:
            logger.warning(f"Не удалось получить прогресс пользователя: {e}")

    # ── Шаг 2: определяем уровень ────────────────────────
    level = determine_level(user_progress, user_streak)

    # ── Шаг 3: формируем промпт ──────────────────────────
    system_prompt = """Ты — эксперт-преподаватель системы микро-обучения SkillStack.
Твоя задача — создавать короткие, плотные, практичные уроки.

КРИТИЧЕСКИ ВАЖНО: твой ответ должен быть ТОЛЬКО валидным JSON-объектом.
— Никаких слов до или после JSON
— Никаких markdown-блоков (``` или ~~~)
— Никаких комментариев или пояснений
— Только чистый JSON, начинающийся с { и заканчивающийся }"""

    user_prompt = f"""Создай микро-урок (7-10 минут) по навыку: "{skill}"

Профиль студента:
- Уровень: {level['label']}
- {level['lesson_number_hint']}
- Стиль объяснения: {level['theory_style']}
- Стиль вопросов: {level['question_style']}

Требования к уроку:
- Название: конкретное и мотивирующее (НЕ просто "Урок по {skill}")
- Теория: ровно 3 пункта, каждый — одно чёткое, полезное утверждение или факт
- Вопросы: 3 вопроса, все варианты ответов правдоподобные (не очевидный мусор)
- correct — индекс правильного ответа (0, 1, 2 или 3)

Верни строго этот JSON:
{{
  "title": "...",
  "level": "{level['label']}",
  "theory": ["пункт 1", "пункт 2", "пункт 3"],
  "questions": [
    {{"text": "...", "options": ["A", "B", "C", "D"], "correct": 0}},
    {{"text": "...", "options": ["A", "B", "C", "D"], "correct": 2}},
    {{"text": "...", "options": ["A", "B", "C", "D"], "correct": 1}}
  ]
}}"""

    # ── Шаг 4: вызываем Claude API ───────────────────────
    raw = ""
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text
        lesson = safe_parse_json(raw)

        if not validate_lesson(lesson):
            raise ValueError(f"Невалидная структура урока: {list(lesson.keys())}")

        logger.info(f"Урок сгенерирован: '{lesson['title']}' для пользователя {user_id}, уровень {level['label']}")
        return lesson

    except Exception as e:
        logger.error(f"Ошибка генерации урока: {e}. Raw: {raw[:300] if raw else 'нет ответа'}")
        # Фоллбэк — минимальный урок чтобы пользователь не видел белый экран
        return {
            "title": f"Основы {skill}",
            "level": level["label"],
            "theory": [
                "AI-генератор временно недоступен. Попробуй обновить через минуту.",
                "Пока можешь повторить материал из предыдущих уроков.",
                "Если проблема сохраняется — напиши в поддержку."
            ],
            "questions": [
                {
                    "text": "Ты готов продолжить обучение позже?",
                    "options": ["Да, жду!", "Конечно", "Обязательно", "Уже жду"],
                    "correct": 0
                }
            ]
        }


@router.post("/ask-ai")
async def ask_ai(data: ChatMessage):
    """
    AI-ментор внутри урока — студент задаёт вопрос по теме.
    Это Premium-фича: в будущем ограничить для free-пользователей.
    """
    if not data.question.strip():
        return {"answer": "Задай вопрос — я отвечу!"}

    context_block = ""
    if data.lesson_context:
        context_block = f"\nТекущая тема урока:\n{data.lesson_context}\n"

    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            system=f"""Ты — дружелюбный AI-ментор по теме "{data.skill}" в приложении SkillStack.
{context_block}
Правила ответа:
— Коротко: 2-4 предложения максимум
— Дружелюбно, на "ты", без формальностей
— На русском языке
— Если вопрос не по теме — мягко верни к теме
— Не повторяй вопрос пользователя в ответе""",
            messages=[{"role": "user", "content": data.question}],
        )
        answer = response.content[0].text.strip()
        logger.info(f"AI-ментор ответил пользователю {data.user_id}")
        return {"answer": answer}

    except Exception as e:
        logger.error(f"Ошибка AI-ментора: {e}")
        return {"answer": "Не смог ответить прямо сейчас. Попробуй переформулировать — или чуть позже!"}


@router.get("/my-skills")
async def get_my_skills(user_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserSkill).where(UserSkill.userId == user_id)
        )
        skills = result.scalars().all()
        return {
            "skills": [
                {
                    "skill": s.skillName,
                    "progress": min(s.progress, 100),  # не даём превысить 100%
                    "streak": s.streak,
                    "level": determine_level(s.progress, s.streak)["label"],
                }
                for s in skills
            ]
        }


@router.get("/app")
async def serve_miniapp():
    return FileResponse("static/index.html")


@router.get("/")
async def health():
    return {"status": "✅ SkillStack Bot + Claude AI is running!"}