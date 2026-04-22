"""
notion_service.py

Структура в Notion (вложенная):
  [корневая страница пользователя]
  └── 📚 Основы Python                  ← страница трека
      ├── 🔻 Блок 1: Синтаксис           ← toggle блока
      │   ├── 🔹 Типы данных             ← toggle темы
      │   │   ├── • пункт 1              ← bullet (пункт теории)
      │   │   ├── • пункт 2
      │   │   └── • пункт 3
      │   └── 🔹 Переменные
      │       └── • ...
      └── 🔻 Блок 2: Управляющие конструкции
          └── 🔹 Циклы
              └── • ...
"""

import logging
import httpx

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"


class NotionService:
    def __init__(self, token: str, root_page_id: str):
        self.token = token
        self.root_page_id = root_page_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # ─────────────────────────────────────────────
    # Низкоуровневые методы
    # ─────────────────────────────────────────────

    async def _post(self, url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=self.headers, json=payload)
            r.raise_for_status()
            return r.json()

    async def _patch(self, url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.patch(url, headers=self.headers, json=payload)
            r.raise_for_status()
            return r.json()

    async def _get(self, url: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def _delete(self, block_id: str) -> None:
        url = f"https://api.notion.com/v1/blocks/{block_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            await client.delete(url, headers=self.headers)

    # ─────────────────────────────────────────────
    # Создание страниц
    # ─────────────────────────────────────────────

    async def create_user_page(self, username: str, user_id: int) -> str:
        """Создаёт корневую страницу пользователя. Возвращает page_id."""
        payload = {
            "parent": {"page_id": self.root_page_id},
            "icon": {"type": "emoji", "emoji": "📖"},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": f"{username} (id: {user_id})"}}]
                }
            },
            "children": [
                self._heading("🎓 Мои конспекты SkillStack", level=1),
                self._paragraph("Здесь автоматически сохраняются все пройденные уроки."),
            ],
        }
        data = await self._post("https://api.notion.com/v1/pages", payload)
        return data["id"]

    async def get_or_create_skill_page(self, user_page_id: str, skill: str) -> str:
        """
        Страница трека (направления) внутри страницы пользователя.
        Ищет существующую; если нет — создаёт. Возвращает page_id страницы трека.
        """
        data = await self._get(
            f"https://api.notion.com/v1/blocks/{user_page_id}/children?page_size=100"
        )
        for block in data.get("results", []):
            if block["type"] == "child_page":
                title = block["child_page"].get("title", "")
                if title == skill:
                    return block["id"]

        # Не нашли — создаём
        payload = {
            "parent": {"page_id": user_page_id},
            "icon": {"type": "emoji", "emoji": "📚"},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": skill}}]
                }
            },
            "children": [
                self._heading(f"Конспект: {skill}", level=2),
            ],
        }
        data = await self._post("https://api.notion.com/v1/pages", payload)
        return data["id"]

    # ─────────────────────────────────────────────
    # ВЛОЖЕННАЯ ЗАПИСЬ: Блок → Тема → Пункты (v2)
    # ─────────────────────────────────────────────

    async def _list_children(self, block_id: str) -> list[dict]:
        """Все дочерние блоки указанного блока (с пагинацией)."""
        results: list[dict] = []
        url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
        while True:
            data = await self._get(url)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            url = (
                f"https://api.notion.com/v1/blocks/{block_id}/children"
                f"?page_size=100&start_cursor={cursor}"
            )
        return results

    @staticmethod
    def _toggle_plain_text(toggle_block: dict) -> str:
        """Достаёт простой текст из toggle-блока для сравнения с заголовком."""
        rich = toggle_block.get("toggle", {}).get("rich_text", [])
        return "".join(r.get("plain_text", "") for r in rich)

    async def get_or_create_block_toggle(
        self,
        track_page_id: str,
        block_title: str,
    ) -> str:
        """
        Ищет toggle блока с указанным заголовком внутри страницы трека.
        Если нет — создаёт. Возвращает block_id toggle'а.

        Заголовок toggle'а = '▸ {block_title}'. По этой подстроке ищем.
        """
        needle = f"▸ {block_title}"
        children = await self._list_children(track_page_id)

        for b in children:
            if b.get("type") != "toggle":
                continue
            text = self._toggle_plain_text(b)
            if text == needle or text.strip() == needle.strip():
                return b["id"]

        # Не нашли — создаём пустой toggle блока
        toggle_block = {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": needle},
                        "annotations": {"bold": True, "color": "green"},
                    }
                ],
                "children": [],
            },
        }
        data = await self._post(
            f"https://api.notion.com/v1/blocks/{track_page_id}/children",
            {"children": [toggle_block]},
        )
        return data["results"][0]["id"]

    async def append_topic_nested(
        self,
        track_page_id: str,
        block_title: str,
        topic_title: str,
        theory_points: list[str],
    ) -> None:
        """
        Вложенная запись урока:
          Страница трека
          └── Toggle блока (создаётся при первом уроке блока)
              └── Toggle темы
                  ├── bullet 1
                  ├── bullet 2
                  └── bullet 3

        Если toggle темы с таким же названием уже есть внутри блока — пропускаем
        (идемпотентность на случай повторного прохождения).
        """
        # 1. Находим/создаём toggle блока
        block_toggle_id = await self.get_or_create_block_toggle(track_page_id, block_title)

        # 2. Проверяем, нет ли уже такой темы
        needle_topic = f"🔹 {topic_title}"
        existing = await self._list_children(block_toggle_id)
        for b in existing:
            if b.get("type") != "toggle":
                continue
            text = self._toggle_plain_text(b)
            if text == needle_topic or text.strip() == needle_topic.strip():
                logger.info(
                    f"Notion: тема '{topic_title}' уже есть в блоке '{block_title}', пропускаем"
                )
                return

        # 3. Bullet-пункты внутри toggle темы
        bullet_children = [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": point}}]
                },
            }
            for point in theory_points
        ]

        # 4. Toggle темы с bullet'ами
        topic_toggle = {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": needle_topic},
                        "annotations": {"bold": True},
                    }
                ],
                "children": bullet_children,
            },
        }

        # 5. Добавляем topic-toggle внутрь toggle блока
        await self._post(
            f"https://api.notion.com/v1/blocks/{block_toggle_id}/children",
            {"children": [topic_toggle]},
        )
        logger.info(
            f"Notion: тема '{topic_title}' записана в блок '{block_title}'"
        )

    # ─────────────────────────────────────────────
    # Старый плоский метод (оставляем для совместимости)
    # ─────────────────────────────────────────────

    async def append_lesson(
        self,
        skill_page_id: str,
        lesson_title: str,
        theory_points: list[str],
    ) -> None:
        """
        ⚠️  DEPRECATED: используй append_topic_nested с блоком.
        Плоская запись — toggle с bullet'ами прямо в странице трека.
        """
        bullet_children = [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": point}}]
                },
            }
            for point in theory_points
        ]
        toggle_block = {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"🔹 {lesson_title}"},
                        "annotations": {"bold": True},
                    }
                ],
                "children": bullet_children,
            },
        }
        await self._post(
            f"https://api.notion.com/v1/blocks/{skill_page_id}/children",
            {"children": [toggle_block]},
        )
        logger.info(f"Notion (flat): урок '{lesson_title}' записан в {skill_page_id}")

    # ─────────────────────────────────────────────
    # Удаление страницы
    # ─────────────────────────────────────────────

    async def delete_user_page(self, page_id: str) -> None:
        """Архивирует (удаляет) страницу пользователя."""
        url = f"https://api.notion.com/v1/pages/{page_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            await client.patch(
                url,
                headers=self.headers,
                json={"archived": True},
            )
        logger.info(f"Notion: страница {page_id} удалена (архивирована)")

    # ─────────────────────────────────────────────
    # Вспомогательные блоки
    # ─────────────────────────────────────────────

    @staticmethod
    def _heading(text: str, level: int = 2) -> dict:
        h_type = f"heading_{level}"
        return {
            "object": "block",
            "type": h_type,
            h_type: {
                "rich_text": [{"type": "text", "text": {"content": text}}]
            },
        }

    @staticmethod
    def _paragraph(text: str) -> dict:
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": text}}]
            },
        }