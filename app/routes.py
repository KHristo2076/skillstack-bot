@router.post("/start-lesson")
async def start_lesson(data: dict):
    """Возвращает контент урока"""
    skill = data.get("skill")

    # Пока статичный контент. Позже можно сделать AI-генерацию
    lesson = {
        "skill": skill,
        "title": f"Урок по {skill}",
        "theory": [
            "📌 Основы: что такое ...",
            "🔥 Ключевой принцип №1",
            "💡 Практический пример",
        ],
        "questions": [
            {
                "text": "Какой из вариантов правильный?",
                "options": ["A", "B", "C", "D"],
                "correct": 0
            },
            {
                "text": "Что важнее всего в ...?",
                "options": ["Скорость", "Качество", "Постоянство", "Всё вместе"],
                "correct": 3
            }
        ]
    }
    return lesson