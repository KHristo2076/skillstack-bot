"""
AI-проверка ответов.

Единая точка проверки для всех типов вопросов.
Используется в submit-topic, submit-block-exam, submit-assessment.

Типы вопросов:
  - multiple_choice: простое сравнение индекса
  - text_input:      нормализация + match ('exact' | 'contains' | 'any')
  - code:            AI-проверка через Claude (работает код + выполняет критерий)
  - translation:     AI-проверка через Claude (смысл, грамматика)
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from app.services.llm import llm_client

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    correct: bool
    score: float           # 0.0 .. 1.0
    feedback: str | None   # объяснение, особенно полезно для code/translation


# ─────────────────────────────────────────────
# Нормализация строки для text_input
# ─────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Нижний регистр, схлопнутые пробелы, без краевых пробелов и знаков препинания."""
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[.,;:!?\"'`«»()]+", "", s)
    return s


# ─────────────────────────────────────────────
# Основной роутер проверки
# ─────────────────────────────────────────────

async def check_answer(question: dict, user_answer: Any) -> CheckResult:
    """
    question — словарь из TopicContent.questions (поля: type, text, options, correct,
               correct_answers, match, language, criteria, ai_check)
    user_answer — int (multiple_choice) или str (остальное)
    """
    qtype = question.get("type", "multiple_choice")

    try:
        if qtype == "multiple_choice":
            return _check_multiple_choice(question, user_answer)
        if qtype == "text_input":
            return _check_text_input(question, user_answer)
        if qtype in ("code", "translation"):
            return await _check_with_ai(question, user_answer)
    except Exception as e:
        logger.error(f"check_answer error for type={qtype}: {e}")
        return CheckResult(correct=False, score=0.0, feedback="Ошибка проверки ответа")

    return CheckResult(correct=False, score=0.0, feedback=f"Неизвестный тип: {qtype}")


# ─────────────────────────────────────────────
# Multiple choice
# ─────────────────────────────────────────────

def _check_multiple_choice(q: dict, user_answer: Any) -> CheckResult:
    correct_idx = q.get("correct")
    if correct_idx is None or not isinstance(user_answer, int):
        return CheckResult(False, 0.0, "Ответ не выбран")

    is_correct = user_answer == correct_idx
    return CheckResult(
        correct=is_correct,
        score=1.0 if is_correct else 0.0,
        feedback="Верно" if is_correct else f"Правильный ответ: вариант {correct_idx + 1}",
    )


# ─────────────────────────────────────────────
# Text input
# ─────────────────────────────────────────────

def _check_text_input(q: dict, user_answer: Any) -> CheckResult:
    if not isinstance(user_answer, str) or not user_answer.strip():
        return CheckResult(False, 0.0, "Ответ пустой")

    correct_answers = q.get("correct_answers") or []
    if not correct_answers:
        return CheckResult(False, 0.0, "Нет эталонного ответа")

    match_type = q.get("match", "exact")
    user_norm = _normalize(user_answer)

    for expected in correct_answers:
        exp_norm = _normalize(expected)
        if match_type == "exact" and user_norm == exp_norm:
            return CheckResult(True, 1.0, "Верно")
        if match_type == "contains" and exp_norm in user_norm:
            return CheckResult(True, 1.0, "Верно")
        if match_type == "any":
            # частичное совпадение — хотя бы одно слово из ожидаемых
            if any(w in user_norm for w in exp_norm.split() if len(w) > 2):
                return CheckResult(True, 1.0, "Верно")

    return CheckResult(
        correct=False,
        score=0.0,
        feedback=f"Ожидается: {correct_answers[0]}",
    )


# ─────────────────────────────────────────────
# AI-проверка (code / translation)
# ─────────────────────────────────────────────

async def _check_with_ai(q: dict, user_answer: Any) -> CheckResult:
    if not isinstance(user_answer, str) or not user_answer.strip():
        return CheckResult(False, 0.0, "Ответ пустой")

    qtype = q.get("type", "code")
    question_text = q.get("text", "")
    criteria = q.get("criteria", "")

    if qtype == "code":
        language = q.get("language", "")
        system_prompt = (
            "Ты — строгий код-ревьюер. Проверяешь ответ ученика.\n"
            "Твой ответ — ТОЛЬКО валидный JSON. Без markdown, без пояснений."
        )
        user_prompt = f"""Задание: {question_text}
Язык: {language}
Критерий правильности: {criteria}

Ответ ученика:
```
{user_answer}
```

Оцени и верни JSON:
{{
  "correct": true/false,
  "score": 0.0 до 1.0 (1.0 — идеально, 0.7 — работает но есть замечания, 0.4 — частично, 0.0 — не работает),
  "feedback": "короткое объяснение на русском (1-2 предложения): что верно, что исправить"
}}"""

    else:  # translation
        correct_answers = q.get("correct_answers") or []
        examples_str = ""
        if correct_answers:
            examples_str = "\nПримеры корректных переводов:\n" + "\n".join(f"- {a}" for a in correct_answers)

        system_prompt = (
            "Ты — преподаватель иностранного языка. Проверяешь перевод ученика.\n"
            "Твой ответ — ТОЛЬКО валидный JSON. Без markdown, без пояснений."
        )
        user_prompt = f"""Задание: {question_text}
Критерий: {criteria or 'смысл передан, грамматика верна'}
{examples_str}

Перевод ученика: {user_answer}

Оцени и верни JSON:
{{
  "correct": true/false,
  "score": 0.0 до 1.0 (1.0 — смысл и грамматика верны, 0.7 — смысл верен, мелкие ошибки, 0.4 — смысл частичный, 0.0 — неверно),
  "feedback": "короткое объяснение на русском (1-2 предложения): что верно, что исправить"
}}"""

    try:
        raw = await llm_client.generate(
            system=system_prompt,
            user=user_prompt,
            max_tokens=300,
        )
        data = _parse_ai_json(raw)

        correct = bool(data.get("correct", False))
        score = float(data.get("score", 0.0))
        score = max(0.0, min(1.0, score))  # clamp
        feedback = str(data.get("feedback", ""))[:500]

        return CheckResult(correct=correct, score=score, feedback=feedback)

    except Exception as e:
        logger.error(f"AI-check ошибка: {e}")
        return CheckResult(False, 0.0, "Не удалось проверить ответ, попробуй ещё раз")


def _parse_ai_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError(f"JSON не найден: {raw[:200]}")
    return json.loads(cleaned[start:end])