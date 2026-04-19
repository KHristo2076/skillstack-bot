"""
notion_service.py

Структура в Notion:
  [корневая страница пользователя]
  └── 📚 Java Senior          ← страница-блок (навык)
      ├── 🔹 Spring            ← toggle heading (тема урока)
      │   ├── Бины             ← bullet (пункт теории)
      │   └── IoC контейнер    ← bullet
      └── 🔹 Collections
          └── ArrayList vs LinkedList
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
        Ищет дочернюю страницу навыка. Если нет — создаёт.
        Возвращает page_id страницы навыка.
        """
        # Получаем дочерние блоки корневой страницы пользователя
        data = await self._get(
            f"https://api.notion.com/v1/blocks/{user_page_id}/children?page_size=100"
        )
        for block in data.get("results", []):
            if block["type"] == "child_page":
                title = block["child_page"].get("title", "")
                if title == skill:
                    return block["id"]

        # Не нашли — создаём страницу навыка
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
    # Запись урока
    # ─────────────────────────────────────────────

    async def append_lesson(
        self,
        skill_page_id: str,
        lesson_title: str,
        theory_points: list[str],
    ) -> None:
        """
        Добавляет урок в страницу навыка в виде:
          🔹 <lesson_title>   ← toggle
              • пункт 1
              • пункт 2
              • пункт 3
        """
        # Bullet-пункты внутри toggle
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
        logger.info(f"Notion: урок '{lesson_title}' записан в страницу {skill_page_id}")

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