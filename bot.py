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

import google.generativeai as genai

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === КОНФИГУРАЦИЯ ===
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}" if RENDER_URL else None

if not TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не задан!")
if not GEMINI_API_KEY:
    raise ValueError("❌ GEMINI_API_KEY не задан!")

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Пути для БД
DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# === БАЗА ДАННЫХ ===
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                history TEXT,
                step INTEGER,
                last_ts REAL
            )
        """)
        await db.commit()
    logger.info("✅ База данных инициализирована")

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
SYSTEM_PROMPT = """Ты клиент в мессенджере. Роль: Инвестор Баке, 50 лет.
Пиши как живой человек: коротко, 1-2 предложения, без эмодзи, без списков, без звездочек.
Будь скептичным, задавай вопросы по делу. Не сдавайся сразу.
Выдавай только текст сообщения, без пояснений."""

model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_PROMPT
)

async def ai_generate(prompt: str) -> str:
    try:
        response = await model.generate_content_async(prompt)
        text = response.text.strip()
        # Очистка от артефактов
        text = re.sub(r'\*.*?\*', '', text)
        text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        return text if text else "..."
    except Exception as e:
        logger.exception(f"AI Error: {e}")
        return f"❌ Ошибка AI: {str(e)}"

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
        [InlineKeyboardButton(text="🚀 Начать симуляцию", callback_data="start_sim")],
        [InlineKeyboardButton(text="🧪 Проверить AI ключ", callback_data="test_ai")]
    ])
    await m.answer("🤖 <b>SalesAI Demo</b>\n\nНажми кнопку, чтобы начать диалог с AI-клиентом.", reply_markup=kb, parse_mode="HTML")
    await state.set_state(SimState.menu)

@dp.callback_query(F.data == "test_ai")
async def test_ai(c: types.CallbackQuery):
    await c.answer("Проверка...")
    result = await ai_generate("Привет, скажи 'OK' если работаешь.")
    if "Ошибка" in result:
        await c.message.answer(f"❌ AI не работает:\n<code>{result}</code>", parse_mode="HTML")
    else:
        await c.message.answer(f"✅ AI работает!\nОтвет: {result}")

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
    first_msg = await ai_generate(f"Менеджер предлагает: {niche}. Напиши первое сообщение.")
    
    if first_msg.startswith("❌ Ошибка"):
        await m.answer(first_msg)
        await state.clear()
        return
    
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
    
    # Формируем промпт
    dialogue_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])
    reply = await ai_generate(dialogue_text)
    
    if reply.startswith("❌ Ошибка"):
        await m.answer(reply)
        await clear_session(m.from_user.id)
        await state.clear()
        return
    
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
        try:
            await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
            logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")
        except Exception as e:
            logger.error(f"❌ Ошибка webhook: {e}")
    else:
        logger.warning("⚠️ WEBHOOK_URL не задан. Проверь RENDER_EXTERNAL_URL.")
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False}

@app.get("/health")
async def health():
    return {"status": "ok", "db": str(DB_PATH)}
