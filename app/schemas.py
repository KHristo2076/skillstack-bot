"""
Pydantic-модели для запросов и ответов API.

Логика именования:
- *Request — что принимаем от фронта
- *Response — что отдаём
- *Item — элемент списка в составе другого ответа
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# Tracks (направления)
# ─────────────────────────────────────────────

class CreateTrackRequest(BaseModel):
    user_id: int
    skill: str                          # "Java Senior" или кастом "Figma + UI/UX"
    username: str | None = None         # для Notion


class TopicItem(BaseModel):
    id: int
    order_num: int
    title: str
    status: Literal["locked", "available", "passed"]
    score_pct: float | None = None


class BlockItem(BaseModel):
    id: int
    order_num: int
    title: str
    topics: list[TopicItem]
    exam_passed: bool = False
    exam_score_pct: float | None = None


class TrackOverview(BaseModel):
    """Полная структура трека + прогресс пользователя. Для экрана overview."""
    id: int
    name: str
    description: str
    where_applied: str
    final_skills: list[str]
    total_topics: int
    progress_pct: float
    streak: int
    mode: Literal["learn", "assessed"] | None = None   # null = режим ещё не выбран
    blocks: list[BlockItem]


class MyTrackItem(BaseModel):
    """Краткая карточка трека для главного экрана."""
    id: int
    name: str
    progress_pct: float
    streak: int
    total_topics: int
    level_label: str   # 'новичок' / 'начинающий' / 'средний' / 'продвинутый'


class MyTracksResponse(BaseModel):
    tracks: list[MyTrackItem]


# ─────────────────────────────────────────────
# Выбор режима обучения
# ─────────────────────────────────────────────

class ChooseModeRequest(BaseModel):
    user_id: int
    track_id: int
    mode: Literal["learn", "assessed"]


# ─────────────────────────────────────────────
# Работа с темой
# ─────────────────────────────────────────────

class StartTopicRequest(BaseModel):
    user_id: int
    topic_id: int


class Question(BaseModel):
    """
    Универсальный вопрос. type определяет, какие поля заполнены:
      - multiple_choice: options[], correct (int)
      - text_input:      correct_answers[], match ('exact'|'contains'|'any')
      - code:            language, criteria (что проверяем, для AI)
      - translation:     correct_answers[], criteria
    """
    type: Literal["multiple_choice", "text_input", "code", "translation"]
    text: str
    # multiple_choice
    options: list[str] | None = None
    correct: int | None = None
    # text_input / translation
    correct_answers: list[str] | None = None
    match: Literal["exact", "contains", "any"] = "exact"
    # code
    language: str | None = None
    # для code и translation — критерии для AI-проверки
    criteria: str | None = None
    # нужна ли AI-проверка (для code и translation всегда true)
    ai_check: bool = False
    # для assessment: какой блок трека покрывает этот вопрос (1..N)
    covers_block: int | None = None


class TopicContent(BaseModel):
    """Что отдаём на открытие темы."""
    topic_id: int
    track_id: int
    block_id: int
    title: str                # название темы
    block_title: str          # в каком блоке
    order_info: str           # "Блок 1 / Тема 3"
    theory: list[str]
    questions: list[Question]


class AnswerItem(BaseModel):
    """
    Один ответ пользователя.
    value — может быть int (multiple_choice) или str (остальные).
    """
    value: Any


class SubmitTopicRequest(BaseModel):
    user_id: int
    topic_id: int
    answers: list[AnswerItem]


class QuestionResult(BaseModel):
    correct: bool
    score: float                 # 0.0 .. 1.0
    feedback: str | None = None  # от AI или простое "Верно"/"Неверно"


class SubmitTopicResponse(BaseModel):
    topic_id: int
    score_pct: float
    passed: bool                 # ≥70%
    threshold: int = 70
    correct_count: int
    total: int
    per_question: list[QuestionResult]
    progress_pct: float          # общий прогресс по треку
    next_topic_id: int | None    # id следующей темы (null если блок/трек закончились)
    block_exam_available: bool   # true если это была последняя тема блока


# ─────────────────────────────────────────────
# Block Exam
# ─────────────────────────────────────────────

class StartBlockExamRequest(BaseModel):
    user_id: int
    block_id: int


class BlockExamContent(BaseModel):
    block_id: int
    block_title: str
    questions: list[Question]


class SubmitBlockExamRequest(BaseModel):
    user_id: int
    block_id: int
    answers: list[AnswerItem]


class SubmitBlockExamResponse(BaseModel):
    block_id: int
    score_pct: float
    passed: bool                 # ≥70% → следующий блок открывается
    threshold: int = 70
    correct_count: int
    total: int
    per_question: list[QuestionResult]
    next_block_id: int | None


# ─────────────────────────────────────────────
# Assessment (оценка уровня)
# ─────────────────────────────────────────────

class StartAssessmentRequest(BaseModel):
    user_id: int
    track_id: int


class AssessmentContent(BaseModel):
    track_id: int
    track_name: str
    questions: list[Question]


class SubmitAssessmentRequest(BaseModel):
    user_id: int
    track_id: int
    answers: list[AnswerItem]
    # фронт обязан передать обратно вопросы, полученные из /start-assessment
    # (чтобы не перегенерировать и не потерять covers_block)
    questions: list[Question]


class SubmitAssessmentResponse(BaseModel):
    track_id: int
    score_pct: float
    level: Literal["beginner", "middle", "senior"]
    level_label: str                # "Начинающий" / "Средний" / "Продвинутый"
    summary: str                    # краткая оценка от AI
    start_topic_id: int | None      # откуда продолжить (null = с самого начала)
    start_topic_title: str | None
    skipped_topics_count: int       # сколько тем помечено как passed


# ─────────────────────────────────────────────
# AI-ментор (оставляем как было)
# ─────────────────────────────────────────────

class AskAIRequest(BaseModel):
    user_id: int
    skill: str
    question: str
    lesson_context: str = ""


class AskAIResponse(BaseModel):
    answer: str


# ─────────────────────────────────────────────
# Notion
# ─────────────────────────────────────────────

class NotionLinkResponse(BaseModel):
    available: bool
    url: str | None = None
    message: str | None = None