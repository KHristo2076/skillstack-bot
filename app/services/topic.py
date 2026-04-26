"""
Topic service: lifecycle темы.

Главные функции:
  - start_topic()    : отдаёт контент темы, генерит если нужен
  - submit_topic()   : принимает ответы, считает score, обновляет прогресс,
                       разблокирует следующую тему, пишет в Notion
"""

import json
import logging
import re
from datetime import datetime

from app.services.llm import llm_client
from sqlalchemy import case as sa_case, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.database import (
    AsyncSessionLocal, Block, NotionPage, Topic, Track,
    UserTopicProgress, UserTrack,
)
from app.notion_service import NotionService
from app.schemas import (
    AnswerItem, Question, QuestionResult, SubmitTopicResponse, TopicContent,
)
from app.services.ai_check import check_answer

logger = logging.getLogger(__name__)

# Notion опционален
notion: NotionService | None = None
if settings.notion_token and settings.notion_root_page_id:
    notion = NotionService(settings.notion_token, settings.notion_root_page_id)


PASS_THRESHOLD = 70  # процент для прохождения темы


# ─────────────────────────────────────────────
# Генерация контента темы через Claude
# ─────────────────────────────────────────────

def _difficulty_for_topic(topic_order: int, block_order: int, total_blocks: int) -> str:
    """Определяет стиль подачи в зависимости от позиции темы в треке."""
    global_pos = block_order / max(total_blocks, 1)
    if global_pos < 0.33:
        return "простыми словами, с бытовыми аналогиями, разжёвывая термины"
    if global_pos < 0.66:
        return "практически, с примерами кода/применения из реальных проектов"
    return "углублённо: нюансы, edge cases, best practices, сравнения подходов"


def _pick_question_types(track_name: str) -> str:
    """Решает, какие типы вопросов подойдут для этого направления."""
    name_lower = track_name.lower()

    is_programming = any(kw in name_lower for kw in [
        "java", "python", "javascript", "js", "typescript", "ts", "c++", "c#",
        "go", "rust", "kotlin", "swift", "php", "ruby", "разработ", "программ",
        "backend", "frontend", "fullstack", "dev", "data science", "ml",
    ])
    is_language = any(kw in name_lower for kw in [
        "англ", "испан", "франц", "немец", "итальян", "китай",
        "english", "spanish", "french", "german", "b1", "b2", "c1", "ielts", "toefl",
    ])

    if is_programming:
        return (
            "Используй разнообразные типы вопросов:\n"
            "- 2-3 multiple_choice (концепции, терминология)\n"
            "- 1-2 text_input (короткие ответы, названия методов)\n"
            "- 1 code (написать короткий фрагмент кода, 3-7 строк)\n"
        )
    if is_language:
        return (
            "Используй разнообразные типы вопросов:\n"
            "- 2 multiple_choice (грамматика, выбор слова)\n"
            "- 1-2 text_input (вставь слово, заполни пропуск)\n"
            "- 1-2 translation (переведи фразу)\n"
        )
    return (
        "Используй разнообразные типы вопросов:\n"
        "- 3-4 multiple_choice\n"
        "- 1-2 text_input\n"
    )


async def _generate_topic_content(
    topic_title: str,
    block_title: str,
    track_name: str,
    track_description: str,
    topic_order: int,
    block_order: int,
    total_blocks: int,
    is_first_topic: bool,
) -> dict:
    """
    Генерит контент темы: теория + вопросы.
    Если это самая первая тема трека — делает вводную с планом и навыками.
    """
    difficulty = _difficulty_for_topic(topic_order, block_order, total_blocks)
    q_types = _pick_question_types(track_name)

    intro_hint = ""
    if is_first_topic:
        intro_hint = (
            "\n\nЭто ПЕРВАЯ тема трека — сделай её вводной: кратко опиши, "
            "о чём направление, где применяется, и чем ученик овладеет к концу. "
            "Теория должна быть мотивирующей и общей."
        )

    system_prompt = (
        "Ты — эксперт-преподаватель системы микро-обучения SkillStack.\n"
        "Твой ответ — ТОЛЬКО валидный JSON на русском языке. Без markdown, без пояснений."
    )

    user_prompt = f"""Создай микро-урок.

Направление: "{track_name}"
Блок: "{block_title}" (блок {block_order})
Тема урока: "{topic_title}" (тема {topic_order} в блоке)
Стиль подачи: {difficulty}{intro_hint}

Требования к теории:
- 3-5 пунктов, каждый пункт — 2-4 связных предложения
- Конкретные примеры, а не абстрактные фразы

Требования к вопросам (3-5 шт):
{q_types}

Формат вопросов:
- multiple_choice: {{"type": "multiple_choice", "text": "...", "options": ["A", "B", "C", "D"], "correct": 0}}
- text_input: {{"type": "text_input", "text": "...", "correct_answers": ["ответ1", "ответ2"], "match": "contains"}}
- code: {{"type": "code", "text": "...", "language": "java", "criteria": "что должен делать код (1 предложение)", "ai_check": true}}
- translation: {{"type": "translation", "text": "Переведи на ...: '...'", "correct_answers": ["пример 1", "пример 2"], "criteria": "смысл и грамматика", "ai_check": true}}

Верни строго JSON:
{{
  "theory": ["пункт 1", "пункт 2", "пункт 3"],
  "questions": [ ... ]
}}"""

    raw = await llm_client.generate(
        system=system_prompt,
        user=user_prompt,
        max_tokens=2500,
    )
    data = _parse_json(raw)

    # Валидация
    if "theory" not in data or "questions" not in data:
        raise ValueError(f"Невалидная структура: {list(data.keys())}")
    if not isinstance(data["theory"], list) or len(data["theory"]) < 2:
        raise ValueError("Мало пунктов теории")
    if not isinstance(data["questions"], list) or len(data["questions"]) < 1:
        raise ValueError("Нет вопросов")

    # Проставляем ai_check для code и translation (на случай если Claude забыл)
    for q in data["questions"]:
        if q.get("type") in ("code", "translation"):
            q["ai_check"] = True

    return data


def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError(f"JSON не найден: {raw[:200]}")
    return json.loads(cleaned[start:end])


# ─────────────────────────────────────────────
# PUBLIC: start_topic
# ─────────────────────────────────────────────

async def start_topic(user_id: int, topic_id: int) -> TopicContent | None:
    """
    Открывает тему. Если контент не сгенерирован — генерит и кэширует.
    Проверяет, что у юзера есть доступ (status='available' или 'passed').
    """
    async with AsyncSessionLocal() as session:
        # Получаем тему + блок + трек
        topic_res = await session.execute(
            select(Topic, Block, Track)
            .join(Block, Topic.block_id == Block.id)
            .join(Track, Block.track_id == Track.id)
            .where(Topic.id == topic_id)
        )
        row = topic_res.one_or_none()
        if not row:
            return None
        topic, block, track = row

        # Проверяем доступ
        prog_res = await session.execute(
            select(UserTopicProgress).where(
                UserTopicProgress.user_id == user_id,
                UserTopicProgress.topic_id == topic_id,
            )
        )
        progress = prog_res.scalar_one_or_none()
        if not progress or progress.status == "locked":
            logger.warning(f"User {user_id} tried to access locked topic {topic_id}")
            return None

        # Считаем общее число блоков (для difficulty)
        blocks_count_res = await session.execute(
            select(Block).where(Block.track_id == track.id)
        )
        total_blocks = len(blocks_count_res.scalars().all())

        # Проверяем, первая ли это тема в треке (order_num=1 в block order_num=1)
        is_first = (block.order_num == 1 and topic.order_num == 1)

        # Ленивая генерация
        if not topic.content_json:
            try:
                content = await _generate_topic_content(
                    topic_title=topic.title,
                    block_title=block.title,
                    track_name=track.name,
                    track_description=track.description,
                    topic_order=topic.order_num,
                    block_order=block.order_num,
                    total_blocks=total_blocks,
                    is_first_topic=is_first,
                )
                topic.content_json = content
                await session.commit()
                await session.refresh(topic)
            except Exception as e:
                logger.error(f"Topic generation failed: {e}")
                return None

        content = topic.content_json

    questions = [Question(**q) for q in content["questions"]]

    return TopicContent(
        topic_id=topic.id,
        track_id=track.id,
        block_id=block.id,
        title=topic.title,
        block_title=block.title,
        order_info=f"Блок {block.order_num} / Тема {topic.order_num}",
        theory=content["theory"],
        questions=questions,
    )


# ─────────────────────────────────────────────
# PUBLIC: submit_topic
# ─────────────────────────────────────────────

async def submit_topic(
    user_id: int,
    topic_id: int,
    answers: list[AnswerItem],
    username: str = "user",
) -> SubmitTopicResponse | None:
    """
    Принимает ответы, проверяет каждый (AI если нужно),
    считает score, обновляет прогресс, разблокирует следующую тему.

    Архитектура: две короткие сессии + AI-проверка в середине.
    1) Сессия A: читаем Topic/Block/Track, вынимаем всё в плоские переменные
    2) Без сессии: AI-проверка ответов (долго, не держим коннект)
    3) Сессия B: пишем прогресс, разблокируем следующую, пересчитываем %
    """
    # ── Сессия A: читаем контекст ──
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Topic, Block, Track)
            .join(Block, Topic.block_id == Block.id)
            .join(Track, Block.track_id == Track.id)
            .where(Topic.id == topic_id)
        )
        row = res.one_or_none()
        if not row:
            return None
        topic, block, track = row

        if not topic.content_json:
            logger.warning(f"Submit on topic {topic_id} without content")
            return None

        # Вынимаем в обычные переменные — ORM-объекты после закрытия сессии detached
        questions = topic.content_json.get("questions", [])
        block_id = block.id
        block_order = block.order_num
        track_id = track.id
        total_topics = track.total_topics
        topic_order = topic.order_num

    # ── AI-проверка ответов (вне сессии) ──
    per_question: list[QuestionResult] = []
    total_score = 0.0

    for i, q in enumerate(questions):
        if i >= len(answers):
            per_question.append(QuestionResult(correct=False, score=0.0, feedback="Ответ не дан"))
            continue
        user_val = answers[i].value
        result = await check_answer(q, user_val)
        total_score += result.score
        per_question.append(QuestionResult(
            correct=result.correct,
            score=result.score,
            feedback=result.feedback,
        ))

    total_q = len(questions)
    score_pct = (total_score / total_q) * 100 if total_q else 0.0
    correct_count = sum(1 for r in per_question if r.correct)
    passed = score_pct >= PASS_THRESHOLD

    # ── Сессия B: записываем все изменения одной транзакцией ──
    async with AsyncSessionLocal() as session:
        # 1. UserTopicProgress для текущей темы
        stmt = pg_insert(UserTopicProgress).values(
            user_id=user_id,
            topic_id=topic_id,
            status="passed" if passed else "available",
            score_pct=score_pct,
            attempts=1,
            passed_at=datetime.utcnow() if passed else None,
            source="learn",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "topic_id"],
            set_={
                "status": stmt.excluded.status,
                "score_pct": stmt.excluded.score_pct,
                "attempts": UserTopicProgress.attempts + 1,
                "passed_at": stmt.excluded.passed_at,
            },
        )
        await session.execute(stmt)

        # 2. Если passed — разблокируем следующую тему
        next_topic_id: int | None = None
        block_exam_available = False

        if passed:
            next_topic_id, block_exam_available = await _unlock_next_topic(
                session, user_id, track_id, block_id, block_order, topic_order
            )

        # 3. Пересчитать progress_pct трека
        progress_pct = await _recalc_track_progress(
            session, user_id, track_id, total_topics,
        )

        # 4. Обновить UserTrack (streak + last_activity + progress)
        ut_res = await session.execute(
            select(UserTrack).where(
                UserTrack.user_id == user_id,
                UserTrack.track_id == track_id,
            )
        )
        ut = ut_res.scalar_one_or_none()
        if ut:
            ut.progress_pct = progress_pct
            ut.last_activity = datetime.utcnow()
            if passed:
                ut.streak += 1

        await session.commit()

    return SubmitTopicResponse(
        topic_id=topic_id,
        score_pct=round(score_pct, 1),
        passed=passed,
        threshold=PASS_THRESHOLD,
        correct_count=correct_count,
        total=total_q,
        per_question=per_question,
        progress_pct=round(progress_pct, 1),
        next_topic_id=next_topic_id,
        block_exam_available=block_exam_available,
    )


# ─────────────────────────────────────────────
# Разблокировка следующей темы
# ─────────────────────────────────────────────

async def _unlock_next_topic(
    session,
    user_id: int,
    track_id: int,
    current_block_id: int,
    current_block_order: int,
    current_topic_order: int,
) -> tuple[int | None, bool]:
    """
    Ищет следующую тему:
      - в том же блоке (order_num > current)
      - если блок закончился → в следующем блоке → возвращает (topic_id, block_exam_available=True)
      - если трек закончился → (None, False)

    block_exam_available = True означает «все темы текущего блока пройдены,
    можно сдавать экзамен» — это сигнал для фронта показать кнопку экзамена.
    """
    # Следующая тема в том же блоке
    res = await session.execute(
        select(Topic)
        .where(
            Topic.block_id == current_block_id,
            Topic.order_num > current_topic_order,
        )
        .order_by(Topic.order_num)
        .limit(1)
    )
    next_in_block = res.scalar_one_or_none()

    if next_in_block:
        # Разблокируем (если ещё не)
        await _set_topic_status(session, user_id, next_in_block.id, "available")
        return next_in_block.id, False

    # Тем в блоке больше нет — block exam доступен
    # Ищем первую тему следующего блока (для подсказки после экзамена)
    next_block_res = await session.execute(
        select(Block)
        .where(
            Block.track_id == track_id,
            Block.order_num > current_block_order,
        )
        .order_by(Block.order_num)
        .limit(1)
    )
    next_block = next_block_res.scalar_one_or_none()

    if not next_block:
        # Трек закончился
        return None, True  # экзамен последнего блока доступен

    # Первая тема следующего блока НЕ разблокируется пока не сдан block_exam.
    # Поэтому next_topic_id = None, block_exam_available=True
    return None, True


async def _set_topic_status(session, user_id: int, topic_id: int, status: str):
    """
    Ставит статус теме юзера. Если записи нет — создаёт.
    Никогда не даунгрейдит passed → available (через CASE в SET).
    """
    stmt = pg_insert(UserTopicProgress).values(
        user_id=user_id,
        topic_id=topic_id,
        status=status,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "topic_id"],
        set_={
            # Если уже passed — оставляем passed, иначе ставим новый статус
            "status": sa_case(
                (UserTopicProgress.status == "passed", "passed"),
                else_=stmt.excluded.status,
            ),
        },
    )
    await session.execute(stmt)


# ─────────────────────────────────────────────
# Пересчёт прогресса трека
# ─────────────────────────────────────────────

async def _recalc_track_progress(
    session,
    user_id: int,
    track_id: int,
    total_topics: int,
) -> float:
    """
    Считает progress_pct = passed_topics / total_topics * 100.
    Ничего не мутирует — UserTrack обновляет вызывающий код.
    """
    if total_topics == 0:
        return 0.0

    # Считаем passed-темы юзера в этом треке
    passed_res = await session.execute(
        select(UserTopicProgress.id)
        .join(Topic, UserTopicProgress.topic_id == Topic.id)
        .join(Block, Topic.block_id == Block.id)
        .where(
            UserTopicProgress.user_id == user_id,
            Block.track_id == track_id,
            UserTopicProgress.status == "passed",
        )
    )
    passed_count = len(passed_res.scalars().all())
    return (passed_count / total_topics) * 100


# ─────────────────────────────────────────────
# Notion: запись пройденной темы
# ─────────────────────────────────────────────

async def write_topic_to_notion(
    user_id: int,
    username: str,
    track_name: str,
    block_title: str,
    topic_title: str,
    theory: list[str],
) -> None:
    """
    Пишет тему в Notion с вложенной структурой:
      Трек (page) → Блок (toggle) → Тема (toggle) → Пункты (bullets)

    Вызывается из routes.py через BackgroundTasks после submit_topic.

    Архитектура (как в submit_topic): БД-сессия короткая, Notion API вне сессии.
    Notion бывает медленным (до 10 сек), нельзя держать коннект.
    """
    if not notion:
        return

    try:
        # ── Шаг 1: получаем user_page_id из БД (или создаём если впервые) ──
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(NotionPage).where(NotionPage.user_id == user_id)
            )
            notion_row = res.scalar_one_or_none()
            existing_page_id = notion_row.page_id if notion_row else None

        # ── Шаг 2: если нет страницы юзера — создаём в Notion (вне сессии БД) ──
        if existing_page_id is None:
            new_page_id = await notion.create_user_page(username, user_id)

            # Сохраняем запись в БД — отдельной короткой сессией
            async with AsyncSessionLocal() as session:
                session.add(NotionPage(
                    user_id=user_id,
                    page_id=new_page_id,
                    trial_started_at=datetime.utcnow(),
                ))
                await session.commit()
            user_page_id = new_page_id
        else:
            user_page_id = existing_page_id

        # ── Шаг 3: все Notion-операции — вне БД-сессий ──
        track_page_id = await notion.get_or_create_skill_page(user_page_id, track_name)
        await notion.append_topic_nested(
            track_page_id=track_page_id,
            block_title=block_title,
            topic_title=topic_title,
            theory_points=theory,
        )

    except Exception as e:
        logger.error(f"Notion write error для user {user_id}: {e}")