import os
import asyncio
import logging
import re
import time
import json
import aiosqlite
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai
from google.genai import types as genai_types

# === КОНФИГУРАЦИЯ ===
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}" if RENDER_URL else None

if not TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ Задай TELEGRAM_TOKEN и GEMINI_API_KEY в переменных окружения!")

# Пути для БД (важно для Render Disk)
DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# === БАЗА ДАННЫХ (SQLite) ===
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER,
                history TEXT,
                step INTEGER,
                last_ts REAL,
                PRIMARY KEY (user_id)
            )
        """)
        await db.commit()

async def get_session(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT history, step, last_ts FROM sessions WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return {"history": json.loads(row[0]), "step": row[1], "last_ts": row[2]}
    return None

async def save_session(user_id: int, history: list, step: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO sessions (user_id, history, step, last_ts)
            VALUES (?, ?, ?, ?)
        """, (user_id, json.dumps(history, ensure_ascii=False), step, time.time()))
        await db.commit()

async def clear_session(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await db.commit()

# === AI ЛОГИКА ===
async def ai_generate(prompt: str, system: str = None) -> str:
    try:
        config = genai_types.GenerateContentConfig(temperature=0.7)
        if system:
            config.system_instruction = system
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-1.5-flash",
            contents=prompt,
            config=config
        )
        text = response.text.strip()
        text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
        text = re.sub(r'\*.*?\*', '', text)
        return text if text else "..."
    except Exception as e:
        logging.error(f"AI Error: {e}")
        return "Ошибка AI."

CLIENT_SYSTEM = """Ты клиент в мессенджере. Роль: Инвестор Баке.
Пиши как живой человек: коротко, 1-2 предложения, без эмодзи, без списков.
Будь скептичным, задавай вопросы по делу.
Выдавай только текст сообщения."""

# === FSM ===
class SimState(StatesGroup):
    menu = State()
    niche = State()
    dialogue = State()

# === ХЕНДЛЕРЫ ===
@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    await clear_session(m.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать симуляцию", callback_data="start_sim")]
    ])
    await m.answer("🤖 <b>SalesAI Demo</b>\n\nНажми кнопку, чтобы начать диалог с AI-клиентом.", reply_markup=kb, parse_mode="HTML")
    await state.set_state(SimState.menu)

@dp.callback_query(F.data == "start_sim")
async def start_sim(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("💼 Что вы продаете? Напишите нишу (например: CRM-система, квартиры, курсы):")
    await state.set_state(SimState.niche)

@dp.message(SimState.niche)
async def set_niche(m: types.Message, state: FSMContext):
    niche = m.text.strip()
    if len(niche) < 3:
        await m.answer("Слишком коротко. Напишите нормально.")
        return
    
    await m.answer("🟢 <b>Симуляция началась!</b>\n\nКлиент на связи. Жду ваше первое сообщение.", parse_mode="HTML")
    
    # Первое сообщение от клиента
    first_msg = await ai_generate(f"Менеджер предлагает: {niche}. Напиши первое сообщение.", system=CLIENT_SYSTEM)
    history = [{"role": "client", "content": first_msg}]
    await save_session(m.from_user.id, history, 0)
    
    await m.answer(first_msg)
    await state.set_state(SimState.dialogue)

@dp.message(SimState.dialogue)
async def handle_dialogue(m: types.Message, state: FSMContext):
    text = m.text.strip()
    if len(text) < 3:
        await m.answer("Пишите полноценные предложения.")
        return
    
    session = await get_session(m.from_user.id)
    if not session:
        await m.answer("Сессия потеряна. Нажмите /start")
        await state.clear()
        return
    
    history = session["history"]
    step = session["step"] + 1
    history.append({"role": "manager", "content": text})
    
    # Индикатор "печатает..."
    await bot.send_chat_action(chat_id=m.chat.id, action="typing")
    
    # Ответ AI
    prompt_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])
    reply = await ai_generate(prompt_text, system=CLIENT_SYSTEM)
    
    history.append({"role": "client", "content": reply})
    await save_session(m.from_user.id, history, step)
    
    await m.answer(reply)
    
    # Завершение через 6 реплик
    if step >= 6:
        await m.answer("🏁 <b>Симуляция завершена.</b>\n\nНажмите /start для новой попытки.", parse_mode="HTML")
        await clear_session(m.from_user.id)
        await state.clear()

# === FASTAPI ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        logging.info(f"Webhook set: {WEBHOOK_URL}")
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    data = await request.json()
    update = types.Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok"}
