import pytest
from unittest.mock import patch


def test_settings_loads_from_env():
    with patch.dict("os.environ", {"BOT_TOKEN": "test-token", "WEBAPP_URL": "https://example.com"}):
        from pydantic_settings import BaseSettings

        class TestSettings(BaseSettings):
            bot_token: str
            webapp_url: str

        s = TestSettings()
        assert s.bot_token == "test-token"
        assert s.webapp_url == "https://example.com"
