import logging
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv
import os
import asyncio

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = "https://your-miniapp-url.com"  # ← позже заменим на реальный

# Настройка логирования
logging.basicConfig(level=logging.INFO)

app = FastAPI()

# Telegram Application
application = Application.builder().token(TOKEN).build()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(
            text="🚀 Открыть SkillStack",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        text="👋 Привет! Добро пожаловать в **SkillStack** — твой личный трекер навыков.\n\n"
             "Нажми кнопку ниже, чтобы начать учиться 7–15 минут в день и видеть реальный прогресс!",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


# Регистрируем хендлер
application.add_handler(CommandHandler("start", start_command))


@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return {"status": "error"}


@app.get("/")
async def health():
    return {"status": "SkillStack Bot is running!"}


if __name__ == "__main__":
    # Запуск FastAPI + Telegram polling (для локального теста)
    print("🚀 Bot started!")
    application.run_polling()