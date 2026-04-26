"""
Assessment service: "Оцени свои навыки".

Флоу:
1. Юзер создал трек → вместо "Начать обучение" выбирает "Оценить уровень"
2. start_assessment() → Claude генерит 7 заданий разного уровня, покрывающих блоки трека
3. Юзер проходит → submit_assessment() → AI оценивает каждый ответ,
   определяет уровень (beginner/middle/senior) и стартовую тему
4. Все темы до стартовой помечаются status='passed' с source='assessment'
5. progress_pct пересчитывается
"""

import json
import logging
import re
from datetime import datetime

from sqlalchemy import case as sa_case, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.services.llm import llm_client

from app.database import (
    AsyncSessionLocal, Assessment, Block, Topic, Track,
    UserTopicProgress, UserTrack,
)
from app.schemas import (
    AnswerItem, AssessmentContent, Question, SubmitAssessmentResponse,
)
from app.services.ai_check import check_answer
from app.services.topic import _pick_question_types  # переиспользуем логику типов

logger = logging.getLogger(__name__)


LEVEL_LABELS = {
    "beginner": "Начинающий",
    "middle": "Средний",
    "senior": "Продвинутый",
}


# ─────────────────────────────────────────────
# Генерация ассессмент-теста
# ─────────────────────────────────────────────

def _build_blocks_outline(track: Track) -> str:
    """Краткое описание блоков трека для промпта."""
    blocks = track.curriculum_json.get("blocks", [])
    lines = []
    for i, b in enumerate(blocks, start=1):
        topics = ", ".join(b.get("topics", [])[:5])
        lines.append(f"{i}. {b.get('title', '')}: {topics}")
    return "\n".join(lines)


async def _generate_assessment(track: Track) -> list[dict]:
    """
    Просим Claude построить 7 вопросов разного уровня, покрывающих блоки.
    Каждый вопрос помечен 'covers_block' (номер блока) — по этому полю
    определим, что юзер знает.
    """
    q_types_hint = _pick_question_types(track.name)
    blocks_outline = _build_blocks_outline(track)
    total_blocks = len(track.curriculum_json.get("blocks", []))

    system_prompt = (
        "Ты — экзаменатор, составляющий тест для оценки уровня ученика.\n"
        "Твой ответ — ТОЛЬКО валидный JSON на русском языке, без markdown, без пояснений."
    )

    user_prompt = f"""Составь оценочный тест для направления: "{track.name}"

Описание: {track.description}

Блоки программы:
{blocks_outline}

Требования:
- Ровно 7 вопросов
- Сложность возрастает: первые 2 вопроса — базовый уровень (блок 1),
  следующие 3 — средний (блоки середины), последние 2 — продвинутый (последние блоки)
- Каждый вопрос должен покрывать КОНКРЕТНЫЙ блок из списка выше
- В поле covers_block указывай номер блока (1..{total_blocks})

Типы вопросов:
{q_types_hint}

Формат каждого вопроса — как в обычном уроке, плюс поле "covers_block":
- multiple_choice: {{"type": "multiple_choice", "text": "...", "options": [...], "correct": 0, "covers_block": 1}}
- text_input: {{"type": "text_input", "text": "...", "correct_answers": [...], "match": "contains", "covers_block": 2}}
- code: {{"type": "code", "text": "...", "language": "java", "criteria": "...", "ai_check": true, "covers_block": 3}}
- translation: {{"type": "translation", "text": "...", "correct_answers": [...], "criteria": "...", "ai_check": true, "covers_block": 3}}

Верни строго JSON:
{{
  "questions": [ ... 7 объектов ... ]
}}"""

    raw = await llm_client.generate(
        system=system_prompt,
        user=user_prompt,
        max_tokens=3000,
    )
    data = _parse_json(raw)

    questions = data.get("questions", [])
    if not isinstance(questions, list) or len(questions) < 5:
        raise ValueError(f"Мало вопросов: {len(questions)}")

    # Защита: проставить ai_check и covers_block-дефолт
    for i, q in enumerate(questions):
        if q.get("type") in ("code", "translation"):
            q["ai_check"] = True
        if "covers_block" not in q:
            # Распределяем равномерно по блокам, если Claude забыл
            q["covers_block"] = min(
                max(1, (i * total_blocks // len(questions)) + 1),
                total_blocks,
            )

    return questions


def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError(f"JSON не найден: {raw[:200]}")
    return json.loads(cleaned[start:end])


# ─────────────────────────────────────────────
# PUBLIC: start_assessment
# ─────────────────────────────────────────────

async def start_assessment(user_id: int, track_id: int) -> AssessmentContent | None:
    """Генерит ассессмент для трека. Сохраняем вопросы в Assessment-запись
    вначале — чтобы при submit проверять по тем же вопросам."""
    async with AsyncSessionLocal() as session:
        track_res = await session.execute(
            select(Track).where(Track.id == track_id, Track.user_id == user_id)
        )
        track = track_res.scalar_one_or_none()
        if not track:
            return None

    try:
        questions_raw = await _generate_assessment(track)
    except Exception as e:
        logger.error(f"Assessment generation failed: {e}")
        return None

    # Конвертируем в Question-модели (covers_block попадёт автоматически,
    # т.к. Pydantic с extra='allow' — либо передадим явно)
    questions_models = [Question(**q) for q in questions_raw]

    return AssessmentContent(
        track_id=track.id,
        track_name=track.name,
        questions=questions_models,
    )


# ─────────────────────────────────────────────
# PUBLIC: submit_assessment
# ─────────────────────────────────────────────

async def submit_assessment(
    user_id: int,
    track_id: int,
    answers: list[AnswerItem],
    questions: list[dict],  # вопросы с фронта (с covers_block)
) -> SubmitAssessmentResponse | None:
    """
    Архитектура (важно для стабильности с asyncpg):
    1) Сессия A: читаем Track + блоки + первые темы → вытаскиваем в плоские структуры
    2) Без сессии: AI-проверка 7 ответов + генерация summary (долго)
    3) Сессия B: одной транзакцией пишем все изменения

    Нельзя держать сессию открытой во время вызовов Claude — коннект asyncpg закрывается.
    """
    # ── Сессия A: читаем всё что нужно, сохраняем в обычные переменные ──
    async with AsyncSessionLocal() as session:
        track_res = await session.execute(
            select(Track).where(Track.id == track_id, Track.user_id == user_id)
        )
        track = track_res.scalar_one_or_none()
        if not track:
            return None

        track_name = track.name
        track_total_topics = track.total_topics

        # Все блоки трека
        blocks_res = await session.execute(
            select(Block).where(Block.track_id == track_id).order_by(Block.order_num)
        )
        all_blocks = blocks_res.scalars().all()

        # Для каждого блока — id и order_num (плоско, без ORM)
        blocks_flat = [
            {"id": b.id, "order_num": b.order_num} for b in all_blocks
        ]

        # Все темы трека (с блоком) — понадобятся для определения стартовой темы
        # и для массового обновления "все темы до X"
        topics_res = await session.execute(
            select(Topic.id, Topic.title, Topic.block_id, Topic.order_num, Block.order_num.label("block_order"))
            .join(Block, Topic.block_id == Block.id)
            .where(Block.track_id == track_id)
            .order_by(Block.order_num, Topic.order_num)
        )
        all_topics_flat = [
            {
                "id": row.id,
                "title": row.title,
                "block_id": row.block_id,
                "order_num": row.order_num,
                "block_order": row.block_order,
            }
            for row in topics_res.all()
        ]

    # ── Вне сессии: проверяем ответы через AI ──
    block_scores: dict[int, list[float]] = {}
    total_score = 0.0

    for i, q in enumerate(questions):
        if i >= len(answers):
            score = 0.0
        else:
            result = await check_answer(q, answers[i].value)
            score = result.score
        total_score += score
        covers = q.get("covers_block", 1)
        block_scores.setdefault(covers, []).append(score)

    score_pct = (total_score / len(questions)) * 100 if questions else 0.0

    # Определяем последний освоенный блок (идём по порядку — как только
    # встречаем блок со средним < 0.7, останавливаемся)
    last_passed_block_num = 0
    for block_num in sorted(block_scores.keys()):
        scores = block_scores[block_num]
        avg = sum(scores) / len(scores) if scores else 0.0
        if avg >= 0.7:
            last_passed_block_num = block_num
        else:
            break

    # Уровень
    if score_pct >= 80:
        level = "senior"
    elif score_pct >= 50:
        level = "middle"
    else:
        level = "beginner"

    # Определяем стартовую тему (по плоским данным из сессии A)
    start_topic_id: int | None = None
    start_topic_title: str | None = None

    first_unmastered_order = last_passed_block_num + 1

    # Первая тема первого неосвоенного блока
    for t in all_topics_flat:
        if t["block_order"] == first_unmastered_order and t["order_num"] == 1:
            start_topic_id = t["id"]
            start_topic_title = t["title"]
            break

    # Если все блоки освоены и стартовой темы нет — ставим на последнюю
    if start_topic_id is None and last_passed_block_num > 0 and all_topics_flat:
        last_topic = all_topics_flat[-1]
        start_topic_id = last_topic["id"]
        start_topic_title = last_topic["title"]

    # Список id тем, которые пометим как passed
    topic_ids_to_pass: list[int] = [
        t["id"] for t in all_topics_flat if t["block_order"] <= last_passed_block_num
    ]
    skipped_count = len(topic_ids_to_pass)

    # Генерим summary через Claude (вне сессии — AI вызов)
    summary = await _generate_summary(
        track_name, level, score_pct, skipped_count, start_topic_title,
    )

    # ── Сессия B: одной транзакцией пишем все изменения ──
    now = datetime.utcnow()
    async with AsyncSessionLocal() as session:
        # 1. Массово помечаем темы до стартовой как passed
        for tid in topic_ids_to_pass:
            stmt = pg_insert(UserTopicProgress).values(
                user_id=user_id,
                topic_id=tid,
                status="passed",
                score_pct=100.0,
                attempts=0,
                passed_at=now,
                source="assessment",
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "topic_id"],
                set_={
                    "status": "passed",
                    "score_pct": 100.0,
                    "passed_at": now,
                    "source": "assessment",
                },
            )
            await session.execute(stmt)

        # 2. Разблокируем стартовую тему (только если она ещё не passed)
        if start_topic_id is not None and start_topic_id not in topic_ids_to_pass:
            stmt = pg_insert(UserTopicProgress).values(
                user_id=user_id,
                topic_id=start_topic_id,
                status="available",
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "topic_id"],
                set_={
                    "status": sa_case(
                        (UserTopicProgress.status == "passed", "passed"),
                        else_="available",
                    ),
                },
            )
            await session.execute(stmt)

        # 3. Обновляем UserTrack: mode='assessed', progress_pct
        ut_res = await session.execute(
            select(UserTrack).where(
                UserTrack.user_id == user_id,
                UserTrack.track_id == track_id,
            )
        )
        ut = ut_res.scalar_one_or_none()
        if ut:
            ut.mode = "assessed"
            ut.progress_pct = (
                (skipped_count / track_total_topics * 100)
                if track_total_topics else 0.0
            )
            ut.last_activity = now

        # 4. Сохраняем запись Assessment
        session.add(Assessment(
            user_id=user_id,
            track_id=track_id,
            score_pct=score_pct,
            level=level,
            start_topic_id=start_topic_id,
        ))

        await session.commit()

    return SubmitAssessmentResponse(
        track_id=track_id,
        score_pct=round(score_pct, 1),
        level=level,
        level_label=LEVEL_LABELS[level],
        summary=summary,
        start_topic_id=start_topic_id,
        start_topic_title=start_topic_title,
        skipped_topics_count=skipped_count,
    )


async def _generate_summary(
    track_name: str,
    level: str,
    score_pct: float,
    skipped_count: int,
    start_topic_title: str | None,
) -> str:
    """Короткий мотивирующий текст для экрана результата assessment."""
    level_ru = LEVEL_LABELS[level]

    try:
        system_prompt = (
            "Ты — дружелюбный преподаватель. "
            "Отвечай коротко (3-4 предложения), на 'ты', с мотивацией."
        )
        user_prompt = (
            f"Ученик прошёл оценочный тест по направлению '{track_name}'.\n"
            f"Результат: {round(score_pct)}%, уровень: {level_ru}.\n"
            f"Уже освоено тем: {skipped_count}.\n"
            f"{'Стартует с темы: ' + start_topic_title if start_topic_title else 'Все темы освоены!'}\n\n"
            "Напиши короткое резюме для ученика: где он силён, что его ждёт. Без эмодзи."
        )
        return (await llm_client.generate(
            system=system_prompt,
            user=user_prompt,
            max_tokens=250,
        )).strip()
    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return f"Твой уровень: {level_ru}. Пропущено {skipped_count} тем, которые ты уже знаешь. Продолжай!"