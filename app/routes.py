"""
HTTP API SkillStack v2.

Структура:
  Треки:    /create-track, /my-tracks, /track/{id}, /choose-mode
  Темы:     /start-topic, /submit-topic
  Экзамен:  /start-block-exam, /submit-block-exam   (заглушки — доделаем позже)
  Оценка:   /start-assessment, /submit-assessment
  AI:       /ask-ai
  Notion:   /notion-link, /cron/cleanup-expired-trials
  Прочее:   /webhook, /app, /
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import delete, select
from telegram import Update

from app.services.llm import llm_client
from app.bot import bot_service
from app.config import settings
from app.database import AsyncSessionLocal, NotionPage, UserPremium
from app.notion_service import NotionService
from app.schemas import (
    AskAIRequest, AskAIResponse,
    AssessmentContent, ChooseModeRequest, CreateTrackRequest,
    MyTracksResponse, NotionLinkResponse, StartAssessmentRequest,
    StartTopicRequest, SubmitAssessmentRequest, SubmitAssessmentResponse,
    SubmitTopicRequest, SubmitTopicResponse, TopicContent, TrackOverview,
)
from app.services import assessment as assessment_svc
from app.services import curriculum as curriculum_svc
from app.services import topic as topic_svc
from app.services.topic import write_topic_to_notion


logger = logging.getLogger(__name__)
router = APIRouter()

# Notion (опционально)
notion: NotionService | None = None
if settings.notion_token and settings.notion_root_page_id:
    notion = NotionService(settings.notion_token, settings.notion_root_page_id)


# ─────────────────────────────────────────────
# Helper: премиум-статус
# ─────────────────────────────────────────────

async def _is_premium(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserPremium).where(UserPremium.user_id == user_id)
        )
        row = result.scalar_one_or_none()
        if not row or not row.is_premium:
            return False
        if row.premium_until and row.premium_until < datetime.utcnow():
            return False
        return True


# ─────────────────────────────────────────────
# TELEGRAM WEBHOOK
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


# ─────────────────────────────────────────────
# TRACKS
# ─────────────────────────────────────────────

@router.post("/create-track", response_model=TrackOverview)
async def create_track(req: CreateTrackRequest):
    """Создать новый трек (или вернуть существующий) + отдать полный overview."""
    if not req.skill.strip():
        raise HTTPException(status_code=400, detail="skill is empty")

    try:
        track = await curriculum_svc.create_track_for_user(req.user_id, req.skill.strip())
    except Exception as e:
        logger.error(f"create_track failed: {e}")
        raise HTTPException(status_code=500, detail="Ошибка генерации плана. Попробуй ещё раз.")

    overview = await curriculum_svc.get_track_overview(req.user_id, track.id)
    if not overview:
        raise HTTPException(status_code=500, detail="Не удалось собрать overview")
    return overview


@router.get("/my-tracks", response_model=MyTracksResponse)
async def get_my_tracks(user_id: int):
    """Список всех треков пользователя — для главного экрана."""
    tracks = await curriculum_svc.get_user_tracks(user_id)
    return MyTracksResponse(tracks=tracks)


@router.get("/track/{track_id}", response_model=TrackOverview)
async def get_track(track_id: int, user_id: int):
    """Полный overview трека с прогрессом пользователя."""
    overview = await curriculum_svc.get_track_overview(user_id, track_id)
    if not overview:
        raise HTTPException(status_code=404, detail="Трек не найден")
    return overview


@router.post("/choose-mode")
async def choose_mode(req: ChooseModeRequest):
    """Устанавливает режим обучения: learn | assessed."""
    ok = await curriculum_svc.set_track_mode(req.user_id, req.track_id, req.mode)
    if not ok:
        raise HTTPException(status_code=404, detail="Трек не найден")
    return {"status": "success", "mode": req.mode}


# ─────────────────────────────────────────────
# TOPICS
# ─────────────────────────────────────────────

@router.post("/start-topic", response_model=TopicContent)
async def start_topic(req: StartTopicRequest):
    """Открывает тему — теория + вопросы."""
    content = await topic_svc.start_topic(req.user_id, req.topic_id)
    if not content:
        raise HTTPException(
            status_code=404,
            detail="Тема недоступна или не найдена",
        )
    return content


@router.post("/submit-topic", response_model=SubmitTopicResponse)
async def submit_topic(
    req: SubmitTopicRequest,
    background_tasks: BackgroundTasks,
    username: str = "user",
):
    """Принимает ответы, проверяет, обновляет прогресс."""
    result = await topic_svc.submit_topic(
        req.user_id, req.topic_id, req.answers, username=username
    )
    if result is None:
        raise HTTPException(status_code=400, detail="Не удалось проверить ответы")

    # Если прошёл — пишем тему в Notion в фоне
    if result.passed and notion:
        try:
            async with AsyncSessionLocal() as session:
                from app.database import Block, Topic, Track
                row = await session.execute(
                    select(Topic, Block, Track)
                    .join(Block, Topic.block_id == Block.id)
                    .join(Track, Block.track_id == Track.id)
                    .where(Topic.id == req.topic_id)
                )
                data = row.one_or_none()

            if data:
                topic_obj, block_obj, track_obj = data
                theory = (topic_obj.content_json or {}).get("theory", [])
                if theory:
                    background_tasks.add_task(
                        write_topic_to_notion,
                        req.user_id,
                        username,
                        track_obj.name,
                        block_obj.title,
                        topic_obj.title,
                        theory,
                    )
        except Exception as e:
            logger.error(f"Notion background task setup failed: {e}")

    return result


# ─────────────────────────────────────────────
# ASSESSMENT
# ─────────────────────────────────────────────

@router.post("/start-assessment", response_model=AssessmentContent)
async def start_assessment(req: StartAssessmentRequest):
    """Генерит оценочный тест для трека."""
    content = await assessment_svc.start_assessment(req.user_id, req.track_id)
    if not content:
        raise HTTPException(status_code=404, detail="Трек не найден или ошибка генерации")
    return content


@router.post("/submit-assessment", response_model=SubmitAssessmentResponse)
async def submit_assessment(req: SubmitAssessmentRequest):
    """
    Проверяет assessment. Фронт обязан передать те же вопросы,
    что получил из /start-assessment (поле questions в запросе).
    """
    # Конвертим Question-модели в dict для ai_check
    questions_dicts = [q.model_dump() for q in req.questions]

    result = await assessment_svc.submit_assessment(
        req.user_id, req.track_id, req.answers, questions_dicts,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Трек не найден")
    return result


# ─────────────────────────────────────────────
# AI-МЕНТОР (без изменений)
# ─────────────────────────────────────────────

@router.post("/ask-ai", response_model=AskAIResponse)
async def ask_ai(data: AskAIRequest):
    if not data.question.strip():
        return AskAIResponse(answer="Задай вопрос — я отвечу!")

    context_block = f"\nТема:\n{data.lesson_context}\n" if data.lesson_context else ""

    try:
        answer = await llm_client.generate(
            system=(
                f"Ты — AI-ментор по теме \"{data.skill}\" в SkillStack.\n"
                f"{context_block}\n"
                "Отвечай коротко (2-4 предложения), дружелюбно, на \"ты\", на русском."
            ),
            user=data.question,
            max_tokens=400,
        )
        return AskAIResponse(answer=answer.strip())
    except Exception as e:
        logger.error(f"AI-ментор ошибка: {e}")
        return AskAIResponse(answer="Не смог ответить. Попробуй переформулировать!")


# ─────────────────────────────────────────────
# NOTION
# ─────────────────────────────────────────────

@router.get("/notion-link", response_model=NotionLinkResponse)
async def get_notion_link(user_id: int):
    """Ссылка на Notion — только для Premium."""
    premium = await _is_premium(user_id)
    if not premium:
        return NotionLinkResponse(
            available=False,
            message="Конспекты в Notion доступны с Premium 🔒",
        )

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NotionPage).where(NotionPage.user_id == user_id)
        )
        row = result.scalar_one_or_none()

    if not row:
        return NotionLinkResponse(
            available=False,
            message="Конспект появится после первого урока",
        )

    page_url = f"https://notion.so/{row.page_id.replace('-', '')}"
    return NotionLinkResponse(available=True, url=page_url)


@router.post("/cron/cleanup-expired-trials")
async def cleanup_expired_trials(request: Request):
    """Ежедневный cron для удаления истёкших trial Notion-страниц."""
    secret = request.headers.get("X-Cron-Secret", "")
    if secret != settings.notion_token[:16]:
        return {"error": "Unauthorized"}

    now = datetime.utcnow()
    warn_threshold = now - timedelta(days=27)
    delete_threshold = now - timedelta(days=30)

    warned = 0
    deleted = 0

    async with AsyncSessionLocal() as session:
        pages_result = await session.execute(select(NotionPage))
        all_pages = pages_result.scalars().all()

        for page in all_pages:
            uid = page.user_id
            prem_result = await session.execute(
                select(UserPremium).where(UserPremium.user_id == uid)
            )
            prem = prem_result.scalar_one_or_none()
            has_premium = (
                prem is not None
                and prem.is_premium
                and (prem.premium_until is None or prem.premium_until > now)
            )
            if has_premium:
                continue

            if page.trial_started_at <= warn_threshold and not page.warning_sent:
                try:
                    await bot_service.application.bot.send_message(
                        chat_id=uid,
                        text=(
                            "⚠️ Через 3 дня твои конспекты в Notion будут удалены.\n\n"
                            "Активируй Premium, чтобы сохранить все материалы навсегда! 🔒"
                        ),
                    )
                    page.warning_sent = True
                    await session.commit()
                    warned += 1
                except Exception as e:
                    logger.error(f"Предупреждение {uid}: {e}")

            if page.trial_started_at <= delete_threshold:
                try:
                    if notion:
                        await notion.delete_user_page(page.page_id)
                    await session.execute(
                        delete(NotionPage).where(NotionPage.user_id == uid)
                    )
                    await session.commit()
                    deleted += 1
                except Exception as e:
                    logger.error(f"Удаление {uid}: {e}")

    return {"warned": warned, "deleted": deleted}


# ─────────────────────────────────────────────
# STATIC
# ─────────────────────────────────────────────

@router.get("/app")
async def serve_miniapp():
    return FileResponse(
        "static/index.html",
        headers={
            # Запрещаем кэширование index.html — он ссылается на
            # assets/index-XXXX.js с hash-именем, которое меняется при каждом
            # билде. Если index.html кэшируется, Telegram/браузер будет
            # просить несуществующие старые ассеты → "Not Found" / чёрный экран.
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/")
async def health():
    return {"status": "✅ SkillStack v2 is running!"}