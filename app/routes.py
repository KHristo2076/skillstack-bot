import logging
import json
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select, delete

from anthropic import AsyncAnthropic
from telegram import Update

from app.bot import bot_service
from app.database import AsyncSessionLocal, UserSkill, NotionPage, UserPremium
from app.config import settings
from app.notion_service import NotionService

logger = logging.getLogger(__name__)
router = APIRouter()

anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

# Notion включён только если оба ключа заданы
notion: NotionService | None = None
if settings.notion_token and settings.notion_root_page_id:
    notion = NotionService(settings.notion_token, settings.notion_root_page_id)


# ─────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────

def determine_level(progress: int, streak: int) -> dict:
    if progress == 0 and streak == 0:
        return {
            "label": "новичок",
            "theory_style": "объясняй очень просто, используй аналогии из жизни",
            "question_style": "базовые вопросы на понимание",
            "lesson_number_hint": "это ПЕРВЫЙ урок — начни с самых основ",
        }
    elif progress < 30:
        return {
            "label": "начинающий",
            "theory_style": "объясняй с нуля, вводи термины с пояснением",
            "question_style": "вопросы на определения и базовые концепции",
            "lesson_number_hint": f"пройдено {streak} уроков — строй на предыдущих знаниях",
        }
    elif progress < 65:
        return {
            "label": "средний",
            "theory_style": "практически, с примерами из реального применения",
            "question_style": "вопросы на применение, выбор правильного подхода",
            "lesson_number_hint": f"прогресс {progress}% — углубляй знания",
        }
    else:
        return {
            "label": "продвинутый",
            "theory_style": "углублённо: нюансы, edge cases, best practices",
            "question_style": "сложные вопросы на тонкости и профессиональный выбор",
            "lesson_number_hint": f"высокий прогресс {progress}% — фокус на мастерстве",
        }


def safe_parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError(f"JSON не найден. Raw: {raw[:200]}")
    return json.loads(cleaned[start:end])


def validate_lesson(lesson: dict) -> bool:
    for key in ["title", "theory", "questions"]:
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


async def is_premium(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserPremium).where(UserPremium.userId == user_id)
        )
        row = result.scalar_one_or_none()
        if not row or not row.isPremium:
            return False
        if row.premiumUntil and row.premiumUntil < datetime.utcnow():
            return False
        return True


# ─────────────────────────────────────────────
# Notion: фоновая запись урока
# ─────────────────────────────────────────────

async def write_lesson_to_notion(
    user_id: int,
    username: str,
    skill: str,
    lesson_title: str,
    theory: list[str],
) -> None:
    if not notion:
        return
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(NotionPage).where(NotionPage.userId == user_id)
            )
            notion_row = result.scalar_one_or_none()

            if notion_row is None:
                page_id = await notion.create_user_page(username, user_id)
                notion_row = NotionPage(
                    userId=user_id,
                    pageId=page_id,
                    trialStartedAt=datetime.utcnow(),
                )
                session.add(notion_row)
                await session.commit()

            user_page_id = notion_row.pageId

        skill_page_id = await notion.get_or_create_skill_page(user_page_id, skill)
        await notion.append_lesson(skill_page_id, lesson_title, theory)

    except Exception as e:
        logger.error(f"Notion write error для {user_id}: {e}")


# ─────────────────────────────────────────────
# Модели данных
# ─────────────────────────────────────────────

class SkillData(BaseModel):
    user_id: int
    skill: str
    action: str = "select"
    lesson_title: str = ""
    theory: list[str] = []
    username: str = "user"


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
async def save_skill(data: SkillData, background_tasks: BackgroundTasks):
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
                    "progress": UserSkill.progress + 12,
                    "streak": UserSkill.streak + 1,
                    "lastLesson": datetime.utcnow(),
                },
            )
            # Пишем в Notion в фоне
            if notion and data.lesson_title and data.theory:
                background_tasks.add_task(
                    write_lesson_to_notion,
                    data.user_id,
                    data.username,
                    data.skill,
                    data.lesson_title,
                    data.theory,
                )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["userId", "skillName"])

        await session.execute(stmt)
        await session.commit()

    return {"status": "success"}


@router.post("/start-lesson")
async def start_lesson(data: dict):
    skill = data.get("skill", "")
    user_id = data.get("user_id")

    if not skill:
        return {"error": "Навык не указан"}

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
            logger.warning(f"Не удалось получить прогресс: {e}")

    level = determine_level(user_progress, user_streak)

    system_prompt = """Ты — эксперт-преподаватель системы микро-обучения SkillStack.
КРИТИЧЕСКИ ВАЖНО: твой ответ — ТОЛЬКО валидный JSON. Без markdown, без пояснений."""

    user_prompt = f"""Создай микро-урок по навыку: "{skill}"

Профиль:
- Уровень: {level['label']}
- {level['lesson_number_hint']}
- Стиль: {level['theory_style']}

Верни строго этот JSON:
{{
  "title": "конкретное название (не 'Урок по X')",
  "level": "{level['label']}",
  "theory": ["пункт 1", "пункт 2", "пункт 3"],
  "questions": [
    {{"text": "...", "options": ["A", "B", "C", "D"], "correct": 0}},
    {{"text": "...", "options": ["A", "B", "C", "D"], "correct": 2}},
    {{"text": "...", "options": ["A", "B", "C", "D"], "correct": 1}}
  ]
}}"""

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
            raise ValueError(f"Невалидная структура: {list(lesson.keys())}")
        return lesson

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}. Raw: {raw[:300] if raw else 'нет'}")
        return {
            "title": f"Основы {skill}",
            "level": level["label"],
            "theory": [
                "AI-генератор временно недоступен. Попробуй через минуту.",
                "Пока можешь повторить материал из предыдущих уроков.",
                "Если проблема сохраняется — напиши в поддержку.",
            ],
            "questions": [{
                "text": "Ты готов продолжить обучение позже?",
                "options": ["Да, жду!", "Конечно", "Обязательно", "Уже жду"],
                "correct": 0,
            }],
        }


@router.post("/ask-ai")
async def ask_ai(data: ChatMessage):
    if not data.question.strip():
        return {"answer": "Задай вопрос — я отвечу!"}

    context_block = f"\nТема:\n{data.lesson_context}\n" if data.lesson_context else ""

    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            system=f"""Ты — AI-ментор по теме "{data.skill}" в SkillStack.
{context_block}
Отвечай коротко (2-4 предложения), дружелюбно, на "ты", на русском.""",
            messages=[{"role": "user", "content": data.question}],
        )
        return {"answer": response.content[0].text.strip()}
    except Exception as e:
        logger.error(f"AI-ментор ошибка: {e}")
        return {"answer": "Не смог ответить. Попробуй переформулировать!"}


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
                    "progress": min(s.progress, 100),
                    "streak": s.streak,
                    "level": determine_level(s.progress, s.streak)["label"],
                }
                for s in skills
            ]
        }


@router.get("/notion-link")
async def get_notion_link(user_id: int):
    """Ссылка на Notion — только для Premium."""
    premium = await is_premium(user_id)
    if not premium:
        return {
            "available": False,
            "message": "Конспекты в Notion доступны с Premium 🔒",
        }

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NotionPage).where(NotionPage.userId == user_id)
        )
        row = result.scalar_one_or_none()

    if not row:
        return {"available": False, "message": "Конспект появится после первого урока"}

    page_url = f"https://notion.so/{row.pageId.replace('-', '')}"
    return {"available": True, "url": page_url}


@router.post("/cron/cleanup-expired-trials")
async def cleanup_expired_trials(request: Request):
    """Вызывать ежедневно через cron-job.org."""
    secret = request.headers.get("X-Cron-Secret", "")
    if secret != settings.notion_token[:16]:
        return {"error": "Unauthorized"}

    now = datetime.utcnow()
    warn_threshold = now - timedelta(days=27)
    delete_threshold = now - timedelta(days=30)

    async with AsyncSessionLocal() as session:
        pages_result = await session.execute(select(NotionPage))
        all_pages = pages_result.scalars().all()

        warned = 0
        deleted = 0

        for page in all_pages:
            uid = page.userId
            prem_result = await session.execute(
                select(UserPremium).where(UserPremium.userId == uid)
            )
            prem = prem_result.scalar_one_or_none()
            has_premium = (
                prem is not None
                and prem.isPremium
                and (prem.premiumUntil is None or prem.premiumUntil > now)
            )
            if has_premium:
                continue

            if page.trialStartedAt <= warn_threshold and not page.warningSent:
                try:
                    await bot_service.application.bot.send_message(
                        chat_id=uid,
                        text=(
                            "⚠️ Через 3 дня твои конспекты в Notion будут удалены.\n\n"
                            "Активируй Premium, чтобы сохранить все материалы навсегда! 🔒"
                        ),
                    )
                    page.warningSent = True
                    await session.commit()
                    warned += 1
                except Exception as e:
                    logger.error(f"Предупреждение {uid}: {e}")

            if page.trialStartedAt <= delete_threshold:
                try:
                    if notion:
                        await notion.delete_user_page(page.pageId)
                    await session.execute(
                        delete(NotionPage).where(NotionPage.userId == uid)
                    )
                    await session.commit()
                    deleted += 1
                except Exception as e:
                    logger.error(f"Удаление {uid}: {e}")

    return {"warned": warned, "deleted": deleted}


@router.get("/app")
async def serve_miniapp():
    return FileResponse("static/index.html")


@router.get("/")
async def health():
    return {"status": "✅ SkillStack Bot + Claude AI is running!"}