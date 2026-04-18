from pathlib import Path
from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    bot_token: str
    webapp_url: str
    database_url: str

    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8"}


settings = Settings()
