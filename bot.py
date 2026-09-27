"""
Опционален Telegram бот (aiogram 3.x) с команди за преглед на заявки.

Известията за НОВИ заявки се пращат директно от app.py (notify_telegram) —
това работи веднага щом сложиш TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env,
без да е нужно този бот да работи 24/7.

Този файл е за ДОПЪЛНИТЕЛНА функционалност — ако искаш да питаш бота
"покажи последните заявки" направо от Telegram. Стартира се отделно:

    python bot.py

Изисква:  pip install aiogram==3.7.0
"""
import asyncio
import os
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "automediabg.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def fetch_leads(limit=5):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, phone, car, interest, message, created_at "
        "FROM lead ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def fetch_stats():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM lead").fetchone()[0]
    products = conn.execute("SELECT COUNT(*) FROM product").fetchone()[0]
    conn.close()
    return total, products


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "AutoMediaBG бот е активен.\n\n"
        "/leads — последните 5 заявки\n"
        "/stats — общ брой заявки и продукти"
    )


@dp.message(Command("leads"))
async def cmd_leads(message: Message):
    rows = fetch_leads()
    if not rows:
        await message.answer("Все още няма заявки.")
        return
    lines = []
    for r in rows:
        lines.append(
            f"👤 {r['name']} · {r['phone']}\n"
            f"🚗 {r['car'] or '—'}  ·  {r['interest'] or '—'}\n"
            f"🕒 {r['created_at']}\n"
        )
    await message.answer("\n".join(lines))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    total, products = fetch_stats()
    await message.answer(f"📥 Заявки общо: {total}\n🛒 Продукти в каталога: {products}")


async def main():
    if not BOT_TOKEN:
        print("Липсва TELEGRAM_BOT_TOKEN в .env — ботът не може да стартира.")
        return
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
