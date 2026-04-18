import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport


@pytest.fixture
def mock_bot_service():
    with patch("app.routes.bot_service") as mock_service:
        mock_service.application.bot = MagicMock()
        mock_service.application.process_update = AsyncMock()
        yield mock_service


@pytest.fixture
def app(mock_bot_service):
    from fastapi import FastAPI
    from app.routes import router

    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.mark.asyncio
async def test_health(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "running" in response.json()["status"]


@pytest.mark.asyncio
async def test_webhook_ok(app, mock_bot_service):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhook", json={"update_id": 1})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_error(app, mock_bot_service):
    mock_bot_service.application.process_update = AsyncMock(side_effect=Exception("boom"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhook", json={"update_id": 1})
    assert response.status_code == 200
    assert response.json() == {"status": "error"}


@pytest.mark.asyncio
async def test_save_skill_new(app):
    from app.routes import user_skills
    user_skills.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/save-skill", json={"user_id": 1, "skill": "Python", "action": "add"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "Python" in data["skills"]


@pytest.mark.asyncio
async def test_save_skill_no_duplicates(app):
    from app.routes import user_skills
    user_skills.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/save-skill", json={"user_id": 2, "skill": "Go", "action": "add"})
        response = await client.post("/save-skill", json={"user_id": 2, "skill": "Go", "action": "add"})
    assert response.json()["skills"].count("Go") == 1
