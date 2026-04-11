"""
Telegram Bot - All in one file
Требования: pip install aiogram
"""

import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode

# ═══════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # <-- Вставьте токен от @BotFather
ADMIN_IDS = [123456789]  # <-- Вставьте свой Telegram ID

# ═══════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# "БАЗА ДАННЫХ" (в памяти)
# ═══════════════════════════════════════════

# Хранилище пользователей
users_db: dict[int, dict] = {}
# Хранилище заметок
notes_db: dict[int, list[dict]] = {}

def get_user(user_id: int) -> dict:
    """Получить или создать пользователя."""
    if user_id not in users_db:
        users_db[user_id] = {
            "user_id": user_id,
            "registered_at": datetime.now().isoformat(),
            "message_count": 0,
            "language": "ru",
        }
    return users_db[user_id]

def add_note(user_id: int, text: str) -> int:
    """Добавить заметку, вернуть её номер."""
    if user_id not in notes_db:
        notes_db[user_id] = []
    note = {
        "id": len(notes_db[user_id]) + 1,
        "text": text,
        "created_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "done": False,
    }
    notes_db[user_id].append(note)
    return note["id"]

def get_notes(user_id: int) -> list[dict]:
    """Получить все заметки пользователя."""
    return notes_db.get(user_id, [])

def delete_note(user_id: int, note_id: int) -> bool:
    """Удалить заметку по ID."""
    if user_id in notes_db:
        for i, note in enumerate(notes_db[user_id]):
            if note["id"] == note_id:
                notes_db[user_id].pop(i)
                return True
    return False

def toggle_note(user_id: int, note_id: int) -> bool:
    """Переключить статус заметки."""
    if user_id in notes_db:
        for note in notes_db[user_id]:
            if note["id"] == note_id:
                note["done"] = not note["done"]
                return True
    return False

# ═══════════════════════════════════════════
# FSM — СОСТОЯНИЯ
# ═══════════════════════════════════════════

class NoteStates(StatesGroup):
    waiting_for_note_text = State()

class FeedbackStates(StatesGroup):
    waiting_for_feedback = State()

class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    confirm = State()

# ═══════════════════════════════════════════
# КЛАВИАТУРЫ
# ═══════════════════════════════════════════

def main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Заметки"), KeyboardButton(text="📊 Профиль")],
            [KeyboardButton(text="💬 Обратная связь"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )

def notes_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    """Инлайн-клавиатура для заметок."""
    notes = get_notes(user_id)
    if not notes:
        return None

    buttons = []
    for note in notes:
        status = "✅" if note["done"] else "⬜"
        short_text = note["text"][:30] + ("..." if len(note["text"]) > 30 else "")
        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {short_text}",
                callback_data=f"toggle_{note['id']}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"delete_{note['id']}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="add_note"),
        InlineKeyboardButton(text="🗑 Удалить всё", callback_data="delete_all_notes"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"confirm_{action}"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"cancel_{action}"),
            ]
        ]
    )

def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_notes")]
        ]
    )

# ═══════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ БОТА
# ═══════════════════════════════════════════

bot = Bot(token=BOT_TOKEN, default=types.DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ═══════════════════════════════════════════
# МИДЛВАРЬ (счётчик сообщений)
# ═══════════════════════════════════════════

@router.message.outer_middleware()
async def counting_middleware(handler, event: Message, data: dict):
    user = get_user(event.from_user.id)
    user["message_count"] += 1
    return await handler(event, data)

# ═══════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start."""
    await state.clear()
    user = get_user(message.from_user.id)
    name = message.from_user.full_name

    text = (
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"Я — многофункциональный бот. Вот что я умею:\n\n"
        f"📝 <b>Заметки</b> — создавай, отмечай и удаляй заметки\n"
        f"📊 <b>Профиль</b> — информация о твоём аккаунте\n"
        f"💬 <b>Обратная связь</b> — написать администратору\n"
        f"ℹ️ <b>Помощь</b> — список всех команд\n\n"
        f"Выбери действие на клавиатуре ⬇️"
    )
    await message.answer(text, reply_markup=main_keyboard())

@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message):
    """Команда /help."""
    text = (
        "📖 <b>Список команд:</b>\n\n"
        "/start — Перезапустить бота\n"
        "/help — Показать помощь\n"
        "/notes — Мои заметки\n"
        "/add <i>текст</i> — Быстро добавить заметку\n"
        "/profile — Мой профиль\n"
        "/feedback — Написать разработчику\n"
        "/cancel — Отменить текущее действие\n"
    )

    if message.from_user.id in ADMIN_IDS:
        text += (
            "\n👑 <b>Админ-команды:</b>\n\n"
            "/stats — Статистика бота\n"
            "/broadcast — Рассылка всем пользователям\n"
            "/users — Список пользователей\n"
        )

    await message.answer(text)

@router.message(Command("cancel"))
@router.message(F.text.casefold() == "отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена текущего действия."""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("🤷 Нечего отменять.", reply_markup=main_keyboard())
        return
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_keyboard())

# ═══════════════════════════════════════════
# ЗАМЕТКИ
# ═══════════════════════════════════════════

@router.message(Command("notes"))
@router.message(F.text == "📝 Заметки")
async def cmd_notes(message: Message):
    """Показать заметки."""
    user_id = message.from_user.id
    notes = get_notes(user_id)
    kb = notes_keyboard(user_id)

    if not notes:
        text = (
            "📝 <b>У тебя пока нет заметок.</b>\n\n"
            "Нажми кнопку ниже или отправь:\n"
            "<code>/add Текст заметки</code>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить заметку", callback_data="add_note")]
            ]
        )
    else:
        done_count = sum(1 for n in notes if n["done"])
        text = (
            f"📝 <b>Твои заметки</b> ({done_count}/{len(notes)} выполнено):\n\n"
            f"Нажми на заметку, чтобы отметить ✅/⬜\n"
            f"Нажми 🗑 чтобы удалить"
        )

    await message.answer(text, reply_markup=kb)

@router.message(Command("add"))
async def cmd_add_note(message: Message, state: FSMContext):
    """Быстрое добавление заметки через /add текст."""
    text = message.text.removeprefix("/add").strip()
    if not text:
        await state.set_state(NoteStates.waiting_for_note_text)
        await message.answer(
            "📝 Напиши текст заметки:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="❌ Отмена")]],
                resize_keyboard=True,
            ),
        )
        return

    note_id = add_note(message.from_user.id, text)
    await message.answer(
        f"✅ Заметка #{note_id} добавлена!\n\n📄 <i>{text}</i>",
        reply_markup=main_keyboard(),
    )

@router.callback_query(F.data == "add_note")
async def cb_add_note(callback: CallbackQuery, state: FSMContext):
    """Добавить заметку (через кнопку)."""
    await state.set_state(NoteStates.waiting_for_note_text)
    await callback.message.answer("📝 Напиши текст заметки (или «Отмена»):")
    await callback.answer()

@router.message(NoteStates.waiting_for_note_text)
async def process_note_text(message: Message, state: FSMContext):
    """Обработка текста заметки."""
    if message.text and message.text.lower() in ("отмена", "❌ отмена"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_keyboard())
        return

    text = message.text or "[медиа]"
    note_id = add_note(message.from_user.id, text)
    await state.clear()
    await message.answer(
        f"✅ Заметка #{note_id} добавлена!\n\n📄 <i>{text}</i>",
        reply_markup=main_keyboard(),
    )

@router.callback_query(F.data.startswith("toggle_"))
async def cb_toggle_note(callback: CallbackQuery):
    """Переключить статус заметки."""
    note_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    if toggle_note(user_id, note_id):
        notes = get_notes(user_id)
        done_count = sum(1 for n in notes if n["done"])
        text = f"📝 <b>Твои заметки</b> ({done_count}/{len(notes)} выполнено):\n\n"
        text += "Нажми на заметку, чтобы отметить ✅/⬜\nНажми 🗑 чтобы удалить"
        await callback.message.edit_text(text, reply_markup=notes_keyboard(user_id))
    else:
        await callback.answer("❌ Заметка не найдена")
    await callback.answer()

@router.callback_query(F.data.startswith("delete_") & ~F.data.startswith("delete_all"))
async def cb_delete_note(callback: CallbackQuery):
    """Удалить одну заметку."""
    note_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    if delete_note(user_id, note_id):
        notes = get_notes(user_id)
        if notes:
            done_count = sum(1 for n in notes if n["done"])
            text = f"📝 <b>Твои заметки</b> ({done_count}/{len(notes)} выполнено):\n\n"
            text += "Нажми на заметку, чтобы отметить ✅/⬜\nНажми 🗑 чтобы удалить"
            await callback.message.edit_text(text, reply_markup=notes_keyboard(user_id))
        else:
            await callback.message.edit_text(
                "📝 Все заметки удалены!",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="➕ Добавить", callback_data="add_note")]
                    ]
                ),
            )
        await callback.answer("🗑 Удалено!")
    else:
        await callback.answer("❌ Заметка не найдена")

@router.callback_query(F.data == "delete_all_notes")
async def cb_delete_all_notes(callback: CallbackQuery):
    """Подтверждение удаления всех заметок."""
    await callback.message.edit_text(
        "⚠️ <b>Удалить ВСЕ заметки?</b>\nЭто действие нельзя отменить!",
        reply_markup=confirm_keyboard("delete_all"),
    )
    await callback.answer()

@router.callback_query(F.data == "confirm_delete_all")
async def cb_confirm_delete_all(callback: CallbackQuery):
    """Подтверждение удаления всех заметок."""
    user_id = callback.from_user.id
    notes_db[user_id] = []
    await callback.message.edit_text(
        "✅ Все заметки удалены!",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить", callback_data="add_note")]
            ]
        ),
    )
    await callback.answer()

@router.callback_query(F.data == "cancel_delete_all")
@router.callback_query(F.data == "back_to_notes")
async def cb_cancel_or_back(callback: CallbackQuery):
    """Отмена / назад к заметкам."""
    user_id = callback.from_user.id
    notes = get_notes(user_id)
    if notes:
        done_count = sum(1 for n in notes if n["done"])
        text = f"📝 <b>Твои заметки</b> ({done_count}/{len(notes)} выполнено):\n\n"
        text += "Нажми на заметку, чтобы отметить ✅/⬜\nНажми 🗑 чтобы удалить"
        await callback.message.edit_text(text, reply_markup=notes_keyboard(user_id))
    else:
        await callback.message.edit_text("📝 У тебя нет заметок.")
    await callback.answer()

# ═══════════════════════════════════════════
# ПРОФИЛЬ
# ═══════════════════════════════════════════

@router.message(Command("profile"))
@router.message(F.text == "📊 Профиль")
async def cmd_profile(message: Message):
    """Профиль пользователя."""
    user = get_user(message.from_user.id)
    tg = message.from_user
    notes = get_notes(message.from_user.id)
    done_notes = sum(1 for n in notes if n["done"])

    is_admin = "👑 Администратор" if tg.id in ADMIN_IDS else "👤 Пользователь"

    text = (
        f"📊 <b>Твой профиль</b>\n\n"
        f"🆔 ID: <code>{tg.id}</code>\n"
        f"👤 Имя: {tg.full_name}\n"
        f"📛 Username: @{tg.username or 'не указан'}\n"
        f"🏷 Роль: {is_admin}\n"
        f"📅 Зарегистрирован: {user['registered_at'][:10]}\n"
        f"💬 Сообщений: {user['message_count']}\n"
        f"📝 Заметок: {len(notes)} (выполнено: {done_notes})\n"
    )
    await message.answer(text)

# ═══════════════════════════════════════════
# ОБРАТНАЯ СВЯЗЬ
# ═══════════════════════════════════════════

@router.message(Command("feedback"))
@router.message(F.text == "💬 Обратная связь")
async def cmd_feedback(message: Message, state: FSMContext):
    """Начать обратную связь."""
    await state.set_state(FeedbackStates.waiting_for_feedback)
    await message.answer(
        "💬 Напиши своё сообщение, и я передам его администратору.\n"
        
