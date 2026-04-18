import logging
import os
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")  # потом заменим

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SkillStack Bot")

# Инициализация Telegram Application
application = Application.builder().token(TOKEN).build()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(text="🚀 Открыть SkillStack", web_app=WebAppInfo(url=WEBAPP_URL))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        text="👋 Привет! Добро пожаловать в **SkillStack** — твой личный трекер навыков.\n\n"
             "Нажми кнопку ниже и начни учиться уже сегодня!",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


application.add_handler(CommandHandler("start", start_command))


# Webhook для Render (продакшен)
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
    return {"status": "✅ SkillStack Bot is running!"}


# Локальный запуск (polling)
if __name__ == "__main__":
    print("🚀 Bot started in polling mode (local)")
    application.run_polling()