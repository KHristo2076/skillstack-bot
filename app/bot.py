import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import settings

logger = logging.getLogger(__name__)


class BotService:
    def __init__(self):
        self.application = Application.builder().token(settings.bot_token).build()
        self.application.add_handler(CommandHandler("start", self.start_command))
        self._initialized = False

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton(text="🚀 Открыть SkillStack", web_app=WebAppInfo(url=settings.webapp_url))]
        ]
        await update.message.reply_text(
            text="👋 Привет! Добро пожаловать в **SkillStack** — твой личный трекер навыков.\n\n"
                 "Нажми кнопку ниже и начни учиться уже сегодня!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def initialize(self):
        """Инициализация бота. Ошибки логируются, но не блокируют старт приложения.
        Это важно для dev-среды, где Telegram API может быть временно недоступен
        (нет интернета, VPN, блокировки)."""
        try:
            await self.application.initialize()
            self._initialized = True
            logger.info("✅ Telegram bot initialized")
        except Exception as e:
            logger.error(f"⚠️  Bot init failed (Telegram API unreachable?): {e}")
            logger.error("⚠️  App will run WITHOUT Telegram integration — /webhook won't work")
            self._initialized = False

    async def shutdown(self):
        if self._initialized:
            try:
                await self.application.shutdown()
            except Exception as e:
                logger.error(f"Bot shutdown error: {e}")


bot_service = BotService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_service.initialize()
    yield
    await bot_service.shutdown()