"""
Curriculum service: генерация плана направления через Claude.

Флоу:
1. Юзер вводит название ("Java Senior" или "Figma + UI/UX")
2. generate_curriculum() дёргает Claude → получает план (blocks + topics)
3. create_track_for_user() сохраняет в БД: Track + Blocks + Topics
4. После создания первая тема автоматически получает status='available'
"""

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import (
    AsyncSessionLocal, Block, Topic, Track, UserTopicProgress, UserTrack,
)
from app.schemas import BlockItem, MyTrackItem, TopicItem, TrackOverview
from app.services.llm import llm_client

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Парсинг JSON от Claude
# ─────────────────────────────────────────────

def _safe_parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError(f"JSON не найден. Raw: {raw[:200]}")
    return json.loads(cleaned[start:end])


def _validate_curriculum(data: dict) -> bool:
    required = ["description", "where_applied", "final_skills", "blocks"]
    if not all(k in data for k in required):
        return False
    if not isinstance(data["blocks"], list) or len(data["blocks"]) < 3:
        return False
    for block in data["blocks"]:
        if "title" not in block or "topics" not in block:
            return False
        if not isinstance(block["topics"], list) or len(block["topics"]) < 2:
            return False
    return True


# ─────────────────────────────────────────────
# Уровень по общему прогрессу (для иконки в списке)
# ─────────────────────────────────────────────

def level_label_by_progress(progress_pct: float) -> str:
    if progress_pct < 5:
        return "новичок"
    if progress_pct < 30:
        return "начинающий"
    if progress_pct < 65:
        return "средний"
    return "продвинутый"


# ─────────────────────────────────────────────
# Генерация curriculum через Claude
# ─────────────────────────────────────────────

async def generate_curriculum(skill: str) -> dict:
    """
    Просим Claude построить план направления.
    Возвращает словарь с полями: description, where_applied, final_skills, blocks.
    """
    system_prompt = (
        "Ты — методист-эксперт, составляющий образовательные программы. "
        "Твой ответ — ТОЛЬКО валидный JSON на русском языке, без markdown, без пояснений."
    )

    user_prompt = f"""Составь план обучения по направлению: "{skill}"

Требования:
- 4-8 блоков, расположенных от базового к продвинутому
- В каждом блоке 4-10 тем (микроуроков), темы идут от простого к сложному
- Названия блоков и тем — короткие, конкретные (не "Основы X", а "Типы данных в Java")
- description: 2-3 предложения о сути направления
- where_applied: 2-3 предложения где это применяется в реальности
- final_skills: 5-7 конкретных навыков, которыми овладеет ученик. Формулируй во 2-м лице будущего времени на "ты". Примеры правильного стиля: "научишься писать чистый код", "сможешь проектировать архитектуру", "поймёшь как работают коллекции", "освоишь async/await". Запрещено 3-е лицо ("пишет", "понимает", "работает") — это грубая ошибка.

Верни строго этот JSON:
{{
  "description": "...",
  "where_applied": "...",
  "final_skills": ["навык 1", "навык 2", "навык 3", "навык 4", "навык 5"],
  "blocks": [
    {{
      "title": "Название блока",
      "topics": ["Тема 1", "Тема 2", "Тема 3", "Тема 4"]
    }}
  ]
}}"""

    raw = await llm_client.generate(
        system=system_prompt,
        user=user_prompt,
        max_tokens=3000,
    )
    data = _safe_parse_json(raw)
    if not _validate_curriculum(data):
        raise ValueError(f"Невалидная структура curriculum: {list(data.keys())}")
    return data


# ─────────────────────────────────────────────
# Создание трека в БД
# ─────────────────────────────────────────────

async def create_track_for_user(user_id: int, skill: str) -> Track:
    """
    Полный флоу: генерим curriculum → сохраняем Track + Blocks + Topics →
    создаём UserTrack + разблокируем первую тему.
    Возвращает созданный Track.
    """
    # 1. Проверяем, не создавал ли юзер уже такой трек
    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(Track).where(Track.user_id == user_id, Track.name == skill)
        )
        existing_track = existing.scalar_one_or_none()
        if existing_track:
            logger.info(f"Трек '{skill}' уже существует для user {user_id}")
            return existing_track

    # 2. Генерим curriculum
    curriculum = await generate_curriculum(skill)

    # 3. Считаем общее число тем
    total_topics = sum(len(b["topics"]) for b in curriculum["blocks"])

    # 4. Сохраняем всё в одной транзакции
    async with AsyncSessionLocal() as session:
        track = Track(
            user_id=user_id,
            name=skill,
            description=curriculum["description"],
            where_applied=curriculum["where_applied"],
            final_skills=curriculum["final_skills"],
            curriculum_json=curriculum,
            total_topics=total_topics,
        )
        session.add(track)
        await session.flush()  # получаем track.id

        first_topic_id: int | None = None

        for block_idx, block_data in enumerate(curriculum["blocks"], start=1):
            block = Block(
                track_id=track.id,
                order_num=block_idx,
                title=block_data["title"],
            )
            session.add(block)
            await session.flush()

            for topic_idx, topic_title in enumerate(block_data["topics"], start=1):
                topic = Topic(
                    block_id=block.id,
                    order_num=topic_idx,
                    title=topic_title,
                    content_json=None,
                    notion_written=False,
                )
                session.add(topic)
                await session.flush()

                if first_topic_id is None:
                    first_topic_id = topic.id

        # 5. Создаём UserTrack
        user_track = UserTrack(
            user_id=user_id,
            track_id=track.id,
            progress_pct=0.0,
            streak=0,
            mode="learn",  # дефолтный режим, меняется при выборе
        )
        session.add(user_track)

        # 6. Разблокируем первую тему
        if first_topic_id:
            session.add(UserTopicProgress(
                user_id=user_id,
                topic_id=first_topic_id,
                status="available",
            ))

        await session.commit()
        await session.refresh(track)

    logger.info(f"Трек '{skill}' создан для user {user_id}: {len(curriculum['blocks'])} блоков, {total_topics} тем")
    return track


# ─────────────────────────────────────────────
# Overview трека для фронта
# ─────────────────────────────────────────────

async def get_track_overview(user_id: int, track_id: int) -> TrackOverview | None:
    """
    Собирает полный curriculum + прогресс юзера для экрана overview.
    """
    async with AsyncSessionLocal() as session:
        # Трек с блоками и темами
        track_result = await session.execute(
            select(Track)
            .options(selectinload(Track.blocks).selectinload(Block.topics))
            .where(Track.id == track_id, Track.user_id == user_id)
        )
        track = track_result.scalar_one_or_none()
        if not track:
            return None

        # UserTrack для прогресса + mode
        ut_result = await session.execute(
            select(UserTrack).where(
                UserTrack.user_id == user_id,
                UserTrack.track_id == track_id,
            )
        )
        user_track = ut_result.scalar_one_or_none()

        # Прогресс по всем темам юзера в этом треке
        topic_ids = [t.id for b in track.blocks for t in b.topics]
        progress_map: dict[int, UserTopicProgress] = {}
        if topic_ids:
            prog_result = await session.execute(
                select(UserTopicProgress).where(
                    UserTopicProgress.user_id == user_id,
                    UserTopicProgress.topic_id.in_(topic_ids),
                )
            )
            for p in prog_result.scalars().all():
                progress_map[p.topic_id] = p

    # Собираем блоки (сортируем по order_num, т.к. SQLAlchemy не даёт гарантии порядка)
    blocks_sorted = sorted(track.blocks, key=lambda b: b.order_num)
    block_items: list[BlockItem] = []
    for block in blocks_sorted:
        topics_sorted = sorted(block.topics, key=lambda t: t.order_num)
        topic_items: list[TopicItem] = []
        for topic in topics_sorted:
            prog = progress_map.get(topic.id)
            topic_items.append(TopicItem(
                id=topic.id,
                order_num=topic.order_num,
                title=topic.title,
                status=prog.status if prog else "locked",
                score_pct=prog.score_pct if prog else None,
            ))
        block_items.append(BlockItem(
            id=block.id,
            order_num=block.order_num,
            title=block.title,
            topics=topic_items,
        ))

    return TrackOverview(
        id=track.id,
        name=track.name,
        description=track.description,
        where_applied=track.where_applied,
        final_skills=track.final_skills,
        total_topics=track.total_topics,
        progress_pct=user_track.progress_pct if user_track else 0.0,
        streak=user_track.streak if user_track else 0,
        mode=user_track.mode if user_track else None,
        blocks=block_items,
    )


# ─────────────────────────────────────────────
# Список треков юзера для главного экрана
# ─────────────────────────────────────────────

async def get_user_tracks(user_id: int) -> list[MyTrackItem]:
    """Карточки всех треков юзера."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserTrack, Track)
            .join(Track, UserTrack.track_id == Track.id)
            .where(UserTrack.user_id == user_id)
            .order_by(UserTrack.last_activity.desc())
        )
        rows = result.all()

    return [
        MyTrackItem(
            id=track.id,
            name=track.name,
            progress_pct=user_track.progress_pct,
            streak=user_track.streak,
            total_topics=track.total_topics,
            level_label=level_label_by_progress(user_track.progress_pct),
        )
        for user_track, track in rows
    ]


# ─────────────────────────────────────────────
# Выбор режима обучения
# ─────────────────────────────────────────────

async def set_track_mode(user_id: int, track_id: int, mode: str) -> bool:
    """Устанавливает mode в UserTrack. Возвращает True если обновлено."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserTrack).where(
                UserTrack.user_id == user_id,
                UserTrack.track_id == track_id,
            )
        )
        ut = result.scalar_one_or_none()
        if not ut:
            return False
        ut.mode = mode
        await session.commit()
    return True