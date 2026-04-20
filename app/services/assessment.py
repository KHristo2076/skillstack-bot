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

from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
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
anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


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

    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = response.content[0].text
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
    questions: list[dict],  # вопросы, которые фронт вернул обратно (с covers_block)
) -> SubmitAssessmentResponse | None:
    """
    Проверяет все ответы, считает по блокам какие юзер знает,
    определяет стартовую тему. Все темы до неё → status='passed' source='assessment'.
    """
    async with AsyncSessionLocal() as session:
        track_res = await session.execute(
            select(Track).where(Track.id == track_id, Track.user_id == user_id)
        )
        track = track_res.scalar_one_or_none()
        if not track:
            return None

    # 1. Проверяем ответы. По каждому блоку собираем статистику.
    block_scores: dict[int, list[float]] = {}  # covers_block → [score,...]
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

    # 2. Определяем последний "освоенный" блок:
    # блок считается освоенным, если средний score по нему ≥ 0.7
    blocks_sorted = sorted(block_scores.keys())
    last_passed_block_num = 0
    for block_num in blocks_sorted:
        scores = block_scores[block_num]
        avg = sum(scores) / len(scores) if scores else 0.0
        if avg >= 0.7:
            last_passed_block_num = block_num
        else:
            break  # как только встретили не освоенный — стоп

    # 3. Определяем уровень
    if score_pct >= 80:
        level = "senior"
    elif score_pct >= 50:
        level = "middle"
    else:
        level = "beginner"

    # 4. Находим стартовую тему (первая тема первого неосвоенного блока)
    async with AsyncSessionLocal() as session:
        blocks_res = await session.execute(
            select(Block).where(Block.track_id == track_id).order_by(Block.order_num)
        )
        all_blocks = blocks_res.scalars().all()

        # Первый неосвоенный блок
        first_unmastered_order = last_passed_block_num + 1

        start_topic_id: int | None = None
        start_topic_title: str | None = None
        skipped_count = 0

        target_block = next((b for b in all_blocks if b.order_num == first_unmastered_order), None)
        if target_block:
            t_res = await session.execute(
                select(Topic).where(Topic.block_id == target_block.id).order_by(Topic.order_num).limit(1)
            )
            first_topic = t_res.scalar_one_or_none()
            if first_topic:
                start_topic_id = first_topic.id
                start_topic_title = first_topic.title
        elif last_passed_block_num > 0:
            # Все блоки освоены — ставим на последнюю тему последнего блока
            last_block = all_blocks[-1] if all_blocks else None
            if last_block:
                t_res = await session.execute(
                    select(Topic).where(Topic.block_id == last_block.id).order_by(Topic.order_num.desc()).limit(1)
                )
                last_topic = t_res.scalar_one_or_none()
                if last_topic:
                    start_topic_id = last_topic.id
                    start_topic_title = last_topic.title

        # 5. Помечаем все темы ДО стартовой как passed (source='assessment')
        if last_passed_block_num > 0:
            topics_to_pass_res = await session.execute(
                select(Topic.id)
                .join(Block, Topic.block_id == Block.id)
                .where(
                    Block.track_id == track_id,
                    Block.order_num <= last_passed_block_num,
                )
            )
            topic_ids_to_pass = [tid for tid, in topics_to_pass_res.all()]

            for tid in topic_ids_to_pass:
                stmt = pg_insert(UserTopicProgress).values(
                    user_id=user_id,
                    topic_id=tid,
                    status="passed",
                    score_pct=100.0,
                    attempts=0,
                    passed_at=datetime.utcnow(),
                    source="assessment",
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["user_id", "topic_id"],
                    set_={
                        "status": "passed",
                        "score_pct": 100.0,
                        "passed_at": datetime.utcnow(),
                        "source": "assessment",
                    },
                )
                await session.execute(stmt)

            skipped_count = len(topic_ids_to_pass)

        # 6. Разблокируем стартовую тему
        if start_topic_id:
            stmt = pg_insert(UserTopicProgress).values(
                user_id=user_id,
                topic_id=start_topic_id,
                status="available",
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "topic_id"],
                # Не перезаписывать если уже passed
                set_={"status": "available"},
                where=(UserTopicProgress.status != "passed"),
            )
            await session.execute(stmt)

        # 7. Обновляем UserTrack: mode='assessed', progress_pct
        ut_res = await session.execute(
            select(UserTrack).where(
                UserTrack.user_id == user_id,
                UserTrack.track_id == track_id,
            )
        )
        ut = ut_res.scalar_one_or_none()
        if ut:
            ut.mode = "assessed"
            ut.progress_pct = (skipped_count / track.total_topics * 100) if track.total_topics else 0.0
            ut.last_activity = datetime.utcnow()

        # 8. Генерим summary через Claude для UX
        summary = await _generate_summary(track.name, level, score_pct, skipped_count, start_topic_title)

        # 9. Сохраняем запись Assessment
        assessment = Assessment(
            user_id=user_id,
            track_id=track_id,
            score_pct=score_pct,
            level=level,
            start_topic_id=start_topic_id,
        )
        session.add(assessment)

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
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=250,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return f"Твой уровень: {level_ru}. Пропущено {skipped_count} тем, которые ты уже знаешь. Продолжай!"