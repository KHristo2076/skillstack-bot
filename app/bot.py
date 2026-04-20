# import logging
# from contextlib import asynccontextmanager
#
# from fastapi import FastAPI
# from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
# from telegram.ext import Application, CommandHandler, ContextTypes
#
# from app.config import settings
#
# logger = logging.getLogger(__name__)
#
#
# class BotService:
#     def __init__(self):
#         self.application = Application.builder().token(settings.bot_token).build()
#         self.application.add_handler(CommandHandler("start", self.start_command))
#
#     async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
#         keyboard = [
#             [InlineKeyboardButton(text="🚀 Открыть SkillStack", web_app=WebAppInfo(url=settings.webapp_url))]
#         ]
#         await update.message.reply_text(
#             text="👋 Привет! Добро пожаловать в **SkillStack** — твой личный трекер навыков.\n\n"
#                  "Нажми кнопку ниже и начни учиться уже сегодня!",
#             reply_markup=InlineKeyboardMarkup(keyboard),
#             parse_mode="Markdown",
#         )
#
#     async def initialize(self):
#         await self.application.initialize()
#
#     async def shutdown(self):
#         await self.application.shutdown()
#
#
# bot_service = BotService()
#
#
# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     await bot_service.initialize()
#     yield
#     await bot_service.shutdown()
