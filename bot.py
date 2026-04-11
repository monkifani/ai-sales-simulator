import os
import asyncio
import logging
import random
import re
import json
import time
import smtplib
import hashlib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict
from google import genai
from google.genai import types as genai_types
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_EMAIL = "monkifani@gmail.com"
GMAIL_PASS = os.getenv("GMAIL_PASSWORD")

IS_PROD = os.getenv("IS_PROD") == "1"
IS_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT"))
IS_RENDER = bool(os.getenv("RENDER"))
PORT = int(os.getenv("PORT", "8080" if (IS_PROD or IS_RAILWAY or IS_RENDER) else "8009"))

REPLIT_DOMAIN = os.getenv("REPLIT_DOMAINS", "").split(",")[0].strip()
RENDER_DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH = "/api/tgwebhook"

if RENDER_DOMAIN:
    WEBHOOK_URL = f"{RENDER_DOMAIN}{WEBHOOK_PATH}"
elif REPLIT_DOMAIN:
    WEBHOOK_URL = f"https://{REPLIT_DOMAIN}{WEBHOOK_PATH}"
else:
    WEBHOOK_URL = None

client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-2.5-flash"

bot = Bot(token=TOKEN)
dp = Dispatcher()

MAX_STEPS = 6
MIN_MESSAGE_LENGTH = 3
BOT_VERSION = "2.0.0"

# ============================================================
# ПРИВЕТСТВЕННОЕ СООБЩЕНИЕ ПРИ СТАРТЕ
# ============================================================

WELCOME_INFO = {
    "ru": (
        "<b>SalesAI Simulator</b> v{version}\n\n"
        "Тренажер продаж на базе искусственного интеллекта.\n\n"
        "Как это работает:\n"
        "1. Вы выбираете тип клиента\n"
        "2. Указываете что продаете\n"
        "3. Ведете диалог как с реальным клиентом\n"
        "4. ИИ-аудитор оценивает ваши навыки\n\n"
        "Бот определяет использование ChatGPT в ваших ответах.\n"
        "Результаты сохраняются и отправляются руководству.\n\n"
        "Выберите язык:"
    ),
    "kz": (
        "<b>SalesAI Simulator</b> v{version}\n\n"
        "Жасанды интеллект негiзiндегi сату тренажеры.\n\n"
        "Қалай жумыс iстейдi:\n"
        "1. Клиент типiн тандайсыз\n"
        "2. Не сататыныңызды жазасыз\n"
        "3. Нақты клиентпен сияқты сойлесесiз\n"
        "4. ИИ-аудитор сiздiң дағдыларыңызды бағалайды\n\n"
        "Тiлдi таңдаңыз:"
    ),
}

# ============================================================
# БАЗА ДАННЫХ (in-memory, для MVP)
# ============================================================

class Database:
    def __init__(self):
        self.companies = {}
        self.users = {}
        self.sessions = []
        self.session_counter = 0

    def register_company(self, company_name: str, admin_id: int) -> str:
        raw = f"{company_name}{admin_id}{time.time()}"
        code = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
        self.companies[code] = {
            "name": company_name,
            "admin_ids": [admin_id],
            "created_at": datetime.now().isoformat(),
            "plan": "free",
            "max_users": 10,
            "max_sessions_per_user": 50,
        }
        self.users[admin_id] = {
            "name": "",
            "company_code": code,
            "role": "admin",
            "registered_at": datetime.now().isoformat(),
        }
        return code

    def join_company(self, user_id: int, user_name: str, code: str) -> bool:
        code = code.upper().strip()
        if code not in self.companies:
            return False
        current_users = sum(1 for u in self.users.values() if u.get("company_code") == code)
        if current_users >= self.companies[code]["max_users"]:
            return False
        self.users[user_id] = {
            "name": user_name,
            "company_code": code,
            "role": "manager",
            "registered_at": datetime.now().isoformat(),
        }
        return True

    def get_user(self, user_id: int):
        return self.users.get(user_id)

    def get_company(self, code: str):
        return self.companies.get(code)

    def is_admin(self, user_id: int) -> bool:
        user = self.users.get(user_id)
        return user is not None and user.get("role") == "admin"

    def save_session(self, session_data: dict) -> int:
        self.session_counter += 1
        session_data["session_id"] = self.session_counter
        session_data["completed_at"] = datetime.now().isoformat()
        self.sessions.append(session_data)
        return self.session_counter

    def get_user_sessions(self, user_id: int, limit: int = 10) -> list:
        user_sessions = [s for s in self.sessions if s.get("user_id") == user_id]
        return sorted(user_sessions, key=lambda x: x.get("completed_at", ""), reverse=True)[:limit]

    def get_company_sessions(self, company_code: str, limit: int = 50) -> list:
        company_sessions = [s for s in self.sessions if s.get("company_code") == company_code]
        return sorted(company_sessions, key=lambda x: x.get("completed_at", ""), reverse=True)[:limit]

    def get_company_leaderboard(self, company_code: str) -> list:
        company_sessions = [s for s in self.sessions if s.get("company_code") == company_code]
        stats = defaultdict(lambda: {"scores": [], "sessions": 0, "name": ""})
        for s in company_sessions:
            uid = s.get("user_id")
            score = s.get("score", 0)
            stats[uid]["scores"].append(score)
            stats[uid]["sessions"] += 1
            stats[uid]["name"] = s.get("user_name", "Unknown")
        leaderboard = []
        for uid, data in stats.items():
            avg = sum(data["scores"]) / len(data["scores"]) if data["scores"] else 0
            best = max(data["scores"]) if data["scores"] else 0
            leaderboard.append({
                "user_id": uid,
                "name": data["name"],
                "avg_score": round(avg, 1),
                "best_score": best,
                "sessions": data["sessions"],
            })
        return sorted(leaderboard, key=lambda x: x["avg_score"], reverse=True)

    def get_user_stats(self, user_id: int) -> dict:
        user_sessions = [s for s in self.sessions if s.get("user_id") == user_id]
        if not user_sessions:
            return {"sessions": 0, "avg_score": 0, "best_score": 0, "worst_score": 0, "avg_ai_detect": 0}
        scores = [s.get("score", 0) for s in user_sessions]
        ai_detects = [s.get("ai_detect_percent", 0) for s in user_sessions]
        return {
            "sessions": len(user_sessions),
            "avg_score": round(sum(scores) / len(scores), 1),
            "best_score": max(scores),
            "worst_score": min(scores),
            "avg_ai_detect": round(sum(ai_detects) / len(ai_detects), 1) if ai_detects else 0,
            "last_session": user_sessions[-1].get("completed_at", ""),
        }

db = Database()

# ============================================================
# ПАРАМЕТРЫ СОСТОЯНИЯ КЛИЕНТА
# ============================================================

def generate_client_state():
    return {
        "interest": random.randint(1, 4),
        "trust": random.randint(1, 3),
        "patience": random.randint(3, 6),
        "mood": random.choice(["tired", "neutral", "skeptical", "distracted", "irritated"]),
        "chat_style": random.choice(["curt", "distracted", "warmish", "suspicious", "lazy"]),
        "budget_sensitivity": random.choice(["high", "medium", "low"]),
    }

MOOD_LABELS = {
    "tired": "усталый",
    "neutral": "нейтральный",
    "skeptical": "скептичный",
    "distracted": "отвлеченный",
    "irritated": "слегка раздраженный",
}

STYLE_LABELS = {
    "curt": "сухой",
    "distracted": "рассеянный",
    "warmish": "чуть теплый",
    "suspicious": "подозрительный",
    "lazy": "ленивый",
}

# ============================================================
# ТЕКСТЫ ИНТЕРФЕЙСА
# ============================================================

TEXTS = {
    "ru": {
        "choose_gender": "Как к вам обращаться?",
        "gender_m": "Мужской",
        "gender_f": "Женский",
        "choose_role": "Выберите роль клиента для симуляции:",
        "roles": ["Баке (Инвестор)", "Тетя Гуля (Мама)", "Артур (IT-специалист)"],
        "ask_niche": (
            "Что вы продаете?\n\n"
            "Напишите конкретно, например:\n"
            "квартиры, страховки, CRM-система, онлайн-курсы"
        ),
        "sim_start": "СИМУЛЯЦИЯ НАЧАЛАСЬ\n\nМенеджер, ваш выход. Клиент на связи:",
        "sim_end": "СИМУЛЯЦИЯ ЗАВЕРШЕНА",
        "analyzing": "Аудитор анализирует диалог, подождите 10-15 секунд",
        "end_user_m": (
            "Симуляция завершена.\n\n"
            "Ваши результаты переданы руководству на проверку.\n"
            "Ожидайте обратной связи."
        ),
        "end_user_f": (
            "Симуляция завершена.\n\n"
            "Ваши результаты переданы руководству на проверку.\n"
            "Ожидайте обратной связи."
        ),
        "progress": "Реплика {current} из {total}",
        "too_short": "Слишком короткое сообщение. Напишите нормальное предложение.",
        "company_registered": "Компания зарегистрирована.\n\nВаш код для менеджеров: <code>{code}</code>\n\nОтправьте этот код вашим менеджерам для присоединения.",
        "joined_company": "Вы присоединились к компании: <b>{name}</b>",
        "invalid_code": "Неверный код компании или лимит участников исчерпан.",
        "ask_company_name": "Введите название вашей компании:",
        "ask_company_code": "Введите код компании от вашего руководителя:",
        "welcome_menu": (
            "Выберите действие:"
        ),
        "no_company": "Вы не привязаны к компании. Используйте /start",
        "leaderboard_title": "РЕЙТИНГ МЕНЕДЖЕРОВ\nКомпания: {company}\n\n",
        "leaderboard_row": "{medal} {pos}. {name} - {avg} баллов (лучший: {best}, сессий: {sessions})\n",
        "stats_title": (
            "ВАША СТАТИСТИКА\n\n"
            "Симуляций пройдено: {sessions}\n"
            "Средний балл: {avg}/15\n"
            "Лучший результат: {best}/15\n"
            "Худший результат: {worst}/15\n"
            "Среднее AI-детект: {ai_detect}%\n"
        ),
        "no_stats": "У вас пока нет пройденных симуляций.",
        "admin_panel_title": (
            "ПАНЕЛЬ УПРАВЛЕНИЯ\n"
            "Компания: {company}\n"
            "Код: <code>{code}</code>\n"
            "Менеджеров: {users}\n"
            "Симуляций: {sessions}\n"
        ),
    },
    "kz": {
        "choose_gender": "Сiзге қалай жугiнген дурыс?",
        "gender_m": "Еркек",
        "gender_f": "Айел",
        "choose_role": "Симуляция ушiн клиент ролiн тандаңыз:",
        "roles": ["Баке (Инвестор)", "Гуля тате (Мама)", "Артур (IT-маман)"],
        "ask_niche": "Сiз не сатасыз?\n\nҚысқаша жазыңыз, мысалы:\nпатерлер, сақтандыру, CRM-жуйе",
        "sim_start": "СИМУЛЯЦИЯ БАСТАЛДЫ\n\nМенеджер, бастаңыз. Клиент байланыста:",
        "sim_end": "СИМУЛЯЦИЯ АЯҚТАЛДЫ",
        "analyzing": "Аудитор диалогты талдап жатыр, 10-15 секунд кутiңiз",
        "end_user_m": (
            "Симуляция аяқталды.\n\n"
            "Натижелерiңiз тексерушiге жiберiлдi.\n"
            "Кейiнiрек байланысады."
        ),
        "end_user_f": (
            "Симуляция аяқталды.\n\n"
            "Натижелерiңiз тексерушiге жiберiлдi.\n"
            "Кейiнiрек байланысады."
        ),
        "progress": "Хабарлама {current}/{total}",
        "too_short": "Тым қысқа хабарлама. Толығырақ жазыңыз.",
        "company_registered": "Компания тiркелдi.\n\nМенеджерлер ушiн код: <code>{code}</code>",
        "joined_company": "Сiз компанияға қосылдыңыз: <b>{name}</b>",
        "invalid_code": "Код қате немесе лимит бiттi.",
        "ask_company_name": "Компания атын жазыңыз:",
        "ask_company_code": "Басшыңыздан алған кодты жазыңыз:",
        "welcome_menu": "Арекеттi тандаңыз:",
        "no_company": "Сiз компанияға тiркелмегенсiз. /start басыңыз.",
        "leaderboard_title": "МЕНЕДЖЕРЛЕР РЕЙТИНГI\nКомпания: {company}\n\n",
        "leaderboard_row": "{medal} {pos}. {name} - {avg} балл (уздiк: {best}, сессия: {sessions})\n",
        "stats_title": (
            "СIЗДIҢ СТАТИСТИКА\n\n"
            "Симуляциялар: {sessions}\n"
            "Орташа балл: {avg}/15\n"
            "Уздiк: {best}/15\n"
            "Нашар: {worst}/15\n"
            "Орташа AI-детект: {ai_detect}%\n"
        ),
        "no_stats": "Азiрше симуляция жоқ.",
        "admin_panel_title": (
            "БАСҚАРУ ПАНЕЛI\n"
            "Компания: {company}\n"
            "Код: <code>{code}</code>\n"
            "Менеджерлер: {users}\n"
            "Симуляциялар: {sessions}\n"
        ),
    }
}

# ============================================================
# НОВЫЕ ПРОМПТЫ: МАКСИМАЛЬНО РЕАЛИСТИЧНЫЕ
# ============================================================

def get_global_client_rules(lang):
    language_directive = "Отвечай ТОЛЬКО на русском языке." if lang == "ru" else "Отвечай ТОЛЬКО на казахском языке."

    return f"""Ты обычный человек которому написал менеджер по продажам в мессенджере.
{language_directive}

Правила как ты пишешь:
- как обычный человек в вотсапе
- не как робот, не как актер, не как персонаж
- нормальная длина сообщений, 1-2 предложения обычной длины
- не пиши слишком коротко одним словом
- не пиши длинные полотна
- пунктуация минимальная, можно без точек
- можно с маленькой буквы
- не используй длинное тире
- не используй кавычки-елочки
- не используй многоточие
- не используй эмодзи и смайлики никогда
- не используй восклицательные знаки
- не имитируй звуки (ой, хм, мм, угу, ах)
- не имитируй фоновые события (подождите, тут ребенок, сейчас занят)
- не переигрывай, не будь театральным
- не задавай больше одного вопроса за сообщение
- часто можешь вообще не задавать вопрос а просто реагировать или высказать мнение
- не устраивай допрос менеджеру
- не используй фразы "уточните пожалуйста", "расскажите подробнее", "интересно узнать"
- никаких списков и нумерации
- никаких звездочек с действиями
- будь спокойным, не эмоциональным
- у тебя есть нормальный интерес к теме, ты не враждебный
- если менеджер нормально говорит ты нормально отвечаешь
- если менеджер несет бред ты это спокойно замечаешь

Выдавай только реплику, без пояснений."""


def get_role_instruction(role, lang, niche, client_state):
    mood_desc = MOOD_LABELS.get(client_state["mood"], "нейтральный")
    style_desc = STYLE_LABELS.get(client_state["chat_style"], "обычный")

    state_block = f"""Твое состояние сейчас (не показывай):
- интерес: {client_state['interest']}/10
- доверие: {client_state['trust']}/10
- настроение: {mood_desc}
Если менеджер нормально общается - интерес растет. Если давит или льет воду - падает."""

    personas = {
        "Баке (Инвестор)": {
            "persona": f"""Ты Баке, 50 лет, бизнесмен. У тебя есть деньги и опыт.

Как ты общаешься:
- спокойно и по делу
- без грубости но и без лишней вежливости
- если непонятно, просто говоришь что непонятно
- не засыпаешь вопросами
- можешь просто высказать мнение
- обычный взрослый мужик в переписке

Менеджер пытается продать тебе: "{niche}".""",

            "examples": """Примеры твоих ответов (пиши в таком стиле, не копируй дословно):

"и сколько это стоит"
"а кто уже этим пользуется"
"ну звучит нормально, надо подумать"
"можно конкретнее без воды"
"не совсем понял как это работает"
"ладно скинь подробности"
"пока не убедил"
"а по срокам как"
"""
        },

        "Тетя Гуля (Мама)": {
            "persona": f"""Ты Гуля, 45 лет, обычная женщина, мама.

Как ты общаешься:
- просто, нормальным языком
- если непонятно говоришь что непонятно
- можешь сомневаться но спокойно
- не засыпаешь вопросами
- обычная женщина в чате

Менеджер пытается продать тебе: "{niche}".""",

            "examples": """Примеры твоих ответов (пиши в таком стиле, не копируй дословно):

"а сколько это стоит вообще"
"не очень поняла"
"ну может быть, надо подумать"
"а это точно нормально работает"
"мне пока непонятно зачем мне это"
"ну в принципе интересно да"
"дорого наверное"
"а попроще можно объяснить"
"""
        },

        "Артур (IT-специалист)": {
            "persona": f"""Ты Артур, 28 лет, программист.

Как ты общаешься:
- коротко и по делу
- без лишней вежливости но и без грубости
- если видишь бред спокойно говоришь
- можешь задать один конкретный вопрос
- обычный айтишник в чате

Менеджер пытается продать тебе: "{niche}".""",

            "examples": """Примеры твоих ответов (пиши в таком стиле, не копируй дословно):

"а по факту что это дает"
"откуда такие цифры"
"ну это все говорят, а конкретный пример есть"
"и чем это лучше аналогов"
"звучит как маркетинг"
"ну допустим, дальше что"
"а техническая часть какая"
"не вижу пока смысла если честно"
"""
        },
    }

    kz_to_ru_role_map = {
        "Гуля тате (Мама)": "Тетя Гуля (Мама)",
        "Артур (IT-маман)": "Артур (IT-специалист)",
        "Баке (Инвестор)": "Баке (Инвестор)",
    }

    role_key = kz_to_ru_role_map.get(role, role)
    role_data = personas.get(role_key, personas["Артур (IT-специалист)"])

    return f"""{role_data['persona']}

{state_block}

Перед ответом прикинь: тебе интересно или нет. Отвечай исходя из этого.

{role_data['examples']}"""

# ============================================================
# AI-ДЕТЕКТ
# ============================================================

def get_ai_detect_instruction():
    return """Ты эксперт по определению текстов написанных ИИ. Проанализируй реплики менеджера.

Признаки ИИ:
- идеальная структура и грамматика
- канцеляризмы: "хотел бы отметить", "позвольте предложить"
- шаблонные фразы: "уникальное предложение", "индивидуальный подход"
- длинные развернутые ответы
- длинное тире, кавычки-елочки
- неестественная вежливость
- списки преимуществ
- отсутствие ошибок и разговорности
- резкая смена стиля между сообщениями

Признаки живого человека:
- неидеальная пунктуация
- разговорный стиль
- короткие сообщения
- личный характер
- мелкие ошибки

Формат ответа строго:

AI_DETECT_PERCENT: [0-100]

АНАЛИЗ ПО РЕПЛИКАМ:
- Реплика 1: [начало текста] - [ЖИВОЙ/ПОДОЗРИТЕЛЬНО/ИИ] - [почему]
...

ОБЩИЙ ВЫВОД: [1-2 предложения]

КРАСНЫЕ ФЛАГИ: [фразы которые выдают ИИ, если есть]"""

# ============================================================
# ПРОМПТ СУДЬИ
# ============================================================

def get_judge_system_instruction(gender, lang, niche):
    pr = "он" if gender == "m" else "она"
    pr_v = "показал" if gender == "m" else "показала"
    pr_sold = "продавал" if gender == "m" else "продавала"
    pr_adapted = "адаптировал" if gender == "m" else "адаптировала"
    pr_pressed = "давил" if gender == "m" else "давила"
    pr_was = "был" if gender == "m" else "была"

    return f"""Ты Верховный Аудитор. Оцени навыки менеджера который {pr_sold} "{niche}" в переписке.

Клиент был реалистичным, мог отвечать коротко или расплывчато. Это нормально. Не штрафуй за это.

Перед отчетом проведи анализ в <thinking>...</thinking>.
В thinking рассуждай:
1. Понял ли менеджер клиента и {pr_adapted} ли подачу
2. Задавал вопросы или вываливал информацию
3. Как реагировал на сомнения
4. Говорил штампами или по делу
5. {pr_pressed.capitalize()} или выстраивал контакт

После thinking выдай отчет:

ВЕРДИКТ: [🟢 ЭЛИТА / 🟡 РЕЗЕРВ / 🔴 ДИСКВАЛИФИКАЦИЯ]
Итоговый балл: [X из 15]

Детальный разбор:
- Инициатива: [0-3] - [Комментарий]
- Работа с возражениями: [0-3] - [Комментарий]
- Коммерческий IQ: [0-3] - [Комментарий]
- Адаптивность: [0-3] - [Комментарий]
- Грамотность и стиль: [0-3] - [Комментарий]

ГЛАВНАЯ УЛИКА: "[Цитата менеджера]"
Почему: [Комментарий]

СИЛЬНАЯ СТОРОНА: [Конкретно]
ГЛАВНЫЙ КОСЯК: [Конкретно]

ВОПРОС НА СОБЕСЕДОВАНИИ: "[Вопрос]"

Пиши жестко и экспертно. Без длинных тире и елочек."""

# ============================================================
# ЯДРО ИИ
# ============================================================

async def generate_response(prompt_or_history, system_instruction: str = None, temperature: float = 0.7):
    try:
        config = genai_types.GenerateContentConfig(temperature=temperature)
        if system_instruction:
            config.system_instruction = system_instruction

        if isinstance(prompt_or_history, list):
            contents = []
            for msg in prompt_or_history:
                role = "user" if msg["role"] == "manager" else "model"
                contents.append(
                    genai_types.Content(
                        role=role,
                        parts=[genai_types.Part.from_text(text=msg["content"])]
                    )
                )
        else:
            contents = prompt_or_history

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_ID,
            contents=contents,
            config=config
        )

        result = response.text.strip()
        result = re.sub(r'<thinking>.*?</thinking>', '', result, flags=re.DOTALL).strip()
        result = re.sub(r'\*[^*]+\*', '', result).strip()
        result = result.replace('\u00ab', '"').replace('\u00bb', '"')
        result = result.replace('\u2014', '-').replace('\u2013', '-')
        result = result.replace('...', '').replace('\u2026', '')
        result = re.sub(
            r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
            r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0000FE00-\U0000FE0F'
            r'\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF'
            r'\U00002600-\U000026FF\U00002700-\U000027BF]+', '', result
        ).strip()
        result = result.replace('!', '')
        result = re.sub(r' +', ' ', result).strip()

        return result if result else "ну не знаю"

    except Exception as e:
        logging.error(f"AI error: {e}")
        return "не понял, повтори"

async def run_ai_detection(manager_messages: list) -> dict:
    messages_text = "\n".join([
        f"Реплика {i+1}: {msg}" for i, msg in enumerate(manager_messages)
    ])
    prompt = f"РЕПЛИКИ МЕНЕДЖЕРА:\n\n{messages_text}\n\nВыполни анализ."
    result = await generate_response(
        prompt,
        system_instruction=get_ai_detect_instruction(),
        temperature=0.3
    )
    percent_match = re.search(r'AI_DETECT_PERCENT:\s*(\d+)', result)
    percent = int(percent_match.group(1)) if percent_match else 0
    return {
        "percent": min(percent, 100),
        "full_analysis": result,
    }

def build_full_system_instruction(lang, role, niche, client_state):
    global_rules = get_global_client_rules(lang)
    role_instruction = get_role_instruction(role, lang, niche, client_state)
    return f"{global_rules}\n\n{role_instruction}"

def extract_score_from_verdict(verdict_text: str) -> int:
    match = re.search(r'Итоговый балл:\s*(\d+)', verdict_text)
    return int(match.group(1)) if match else 0

def generate_pdf_report(session_data: dict) -> bytes:
    report = []
    report.append("=" * 60)
    report.append("SALESAI SIMULATOR - ОТЧЕТ")
    report.append("=" * 60)
    report.append("")
    report.append(f"Дата: {session_data.get('completed_at', 'N/A')}")
    report.append(f"Менеджер: {session_data.get('user_name', 'N/A')}")
    report.append(f"Продукт: {session_data.get('niche', 'N/A')}")
    report.append(f"Роль клиента: {session_data.get('role', 'N/A')}")
    report.append(f"Балл: {session_data.get('score', 0)}/15")
    report.append(f"AI-детект: {session_data.get('ai_detect_percent', 0)}%")
    report.append("")
    report.append("-" * 60)
    report.append("ЛОГ ДИАЛОГА:")
    report.append("-" * 60)
    for msg in session_data.get("history", []):
        role_label = "МЕНЕДЖЕР" if msg["role"] == "manager" else "КЛИЕНТ"
        report.append(f"\n[{role_label}]: {msg['content']}")
    report.append("")
    report.append("-" * 60)
    report.append("ВЕРДИКТ АУДИТОРА:")
    report.append("-" * 60)
    report.append(session_data.get("verdict", "N/A"))
    report.append("")
    report.append("-" * 60)
    report.append("AI-ДЕТЕКТ:")
    report.append("-" * 60)
    report.append(session_data.get("ai_detect_analysis", "N/A"))
    report.append("")
    report.append("-" * 60)
    report.append("ВРЕМЯ ОТВЕТОВ:")
    report.append("-" * 60)
    for i, t in enumerate(session_data.get("response_times", [])):
        report.append(f"Реплика {i+1}: {t:.1f} сек")
    avg_time = session_data.get("avg_response_time", 0)
    report.append(f"\nСреднее: {avg_time:.1f} сек")
    report.append("")
    report.append("=" * 60)
    report.append(f"SalesAI Simulator v{BOT_VERSION}")
    report.append("=" * 60)
    return "\n".join(report).encode("utf-8")

# ============================================================
# ОТПРАВКА EMAIL — ТОЛЬКО ТЕБЕ И КОМПАНИИ
# ============================================================

async def send_results_to_admin_email(session_data: dict):
    """Отправляет полный отчет на почту monkifani@gmail.com"""
    try:
        if not GMAIL_PASS:
            logging.warning("GMAIL_PASSWORD не задан, email не отправлен")
            return

        report_bytes = generate_pdf_report(session_data)

        msg = MIMEMultipart()
        msg["From"] = "monkifani@gmail.com"
        msg["To"] = ADMIN_EMAIL
        msg["Subject"] = (
            f"[SalesAI] Отчет: {session_data.get('user_name', 'N/A')} | "
            f"Балл: {session_data.get('score', 0)}/15 | "
            f"AI: {session_data.get('ai_detect_percent', 0)}% | "
            f"{session_data.get('company_name', 'Без компании')}"
        )

        body = f"""SALESAI SIMULATOR — НОВЫЙ ОТЧЕТ

Менеджер: {session_data.get('user_name', 'N/A')}
Telegram ID: {session_data.get('user_id', 'N/A')}
Компания: {session_data.get('company_name', 'Без компании')} (код: {session_data.get('company_code', 'N/A')})
Продукт: {session_data.get('niche', 'N/A')}
Роль клиента: {session_data.get('role', 'N/A')}
Дата: {session_data.get('completed_at', 'N/A')}

РЕЗУЛЬТАТ:
Балл: {session_data.get('score', 0)}/15
AI-детект: {session_data.get('ai_detect_percent', 0)}%

ВЕРДИКТ:
{session_data.get('verdict', 'N/A')}

AI-ДЕТЕКТ АНАЛИЗ:
{session_data.get('ai_detect_analysis', 'N/A')}

ДИАЛОГ:
""" + "\n".join([
            f"[{'МЕНЕДЖЕР' if m['role'] == 'manager' else 'КЛИЕНТ'}]: {m['content']}"
            for m in session_data.get("history", [])
        ])

        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Прикрепляем txt-отчет
        attachment = MIMEText(report_bytes.decode("utf-8"), "plain", "utf-8")
        attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"report_{session_data.get('user_id', 'unknown')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        msg.attach(attachment)

        await asyncio.to_thread(_send_smtp, msg)
        logging.info(f"Email отправлен на {ADMIN_EMAIL}")

    except Exception as e:
        logging.error(f"Ошибка отправки email: {e}")


def _send_smtp(msg: MIMEMultipart):
    """Синхронная отправка через Gmail SMTP"""
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login("monkifani@gmail.com", GMAIL_PASS)
        server.send_message(msg)


async def send_results_to_company_admins(session_data: dict):
    """Отправляет полный отчет всем админам компании в Telegram"""
    try:
        company_code = session_data.get("company_code")
        if not company_code:
            return

        company = db.get_company(company_code)
        if not company:
            return

        admin_ids = company.get("admin_ids", [])
        if not admin_ids:
            return

        report_bytes = generate_pdf_report(session_data)
        report_file = BufferedInputFile(
            report_bytes,
            filename=f"report_{session_data.get('user_id', 'unknown')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )

        # Краткая сводка для сообщения
        summary = (
            f"<b>НОВЫЙ ОТЧЕТ — {company['name']}</b>\n\n"
            f"Менеджер: {session_data.get('user_name', 'N/A')}\n"
            f"Продукт: {session_data.get('niche', 'N/A')}\n"
            f"Роль клиента: {session_data.get('role', 'N/A')}\n"
            f"Дата: {session_data.get('completed_at', 'N/A')[:16]}\n\n"
            f"<b>Балл: {session_data.get('score', 0)}/15</b>\n"
            f"AI-детект: {session_data.get('ai_detect_percent', 0)}%\n\n"
            f"Полный отчет во вложении."
        )

        for admin_id in admin_ids:
            try:
                await bot.send_document(
                    chat_id=admin_id,
                    document=report_file,
                    caption=summary,
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Не удалось отправить отчет админу {admin_id}: {e}")

    except Exception as e:
        logging.error(f"Ошибка отправки отчета компании: {e}")


async def deliver_results(session_data: dict):
    """Главная функция доставки результатов — тебе на почту + админам компании в ТГ"""
    await asyncio.gather(
        send_results_to_admin_email(session_data),
        send_results_to_company_admins(session_data),
    )

# ============================================================
# FSM STATES
# ============================================================

class SimStates(StatesGroup):
    language = State()
    gender = State()
    menu = State()
    register_company = State()
    join_company = State()
    role = State()
    niche = State()
    dialogue = State()

class AdminStates(StatesGroup):
    viewing = State()

# ============================================================
# КОМАНДА /start
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    welcome_text = (
        f"<b>SalesAI Simulator</b> v{BOT_VERSION}\n\n"
        "AI-тренажер для менеджеров по продажам\n\n"
        "Как это работает:\n"
        "- Выбираете тип клиента\n"
        "- Указываете что продаете\n"
        "- Ведете диалог как с реальным клиентом\n"
        "- ИИ-аудитор оценивает ваши навыки\n"
        "- Система определяет использование ChatGPT\n"
        "- Результаты отправляются руководству\n\n"
        "Выберите язык:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Русский", callback_data="lang_ru"),
            InlineKeyboardButton(text="Қазақша", callback_data="lang_kz")
        ]
    ])
    await message.answer(welcome_text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(SimStates.language)

@dp.callback_query(F.data.startswith("lang_"))
async def set_lang(call: types.CallbackQuery, state: FSMContext):
    lang = call.data.split("_")[1]
    await state.update_data(lang=lang)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=TEXTS[lang]["gender_m"], callback_data="gender_m"),
            InlineKeyboardButton(text=TEXTS[lang]["gender_f"], callback_data="gender_f")
        ]
    ])
    await call.message.edit_text(TEXTS[lang]["choose_gender"], reply_markup=kb)
    await state.set_state(SimStates.gender)

@dp.callback_query(F.data.startswith("gender_"))
async def set_gender(call: types.CallbackQuery, state: FSMContext):
    gender = call.data.split("_")[1]
    await state.update_data(gender=gender)
    data = await state.get_data()
    lang = data["lang"]
    await show_main_menu(call.message, lang, call.from_user.id, edit=True)
    await state.set_state(SimStates.menu)

async def show_main_menu(message, lang, user_id, edit=False):
    user = db.get_user(user_id)
    buttons = []

    if user and user.get("company_code"):
        company = db.get_company(user["company_code"])
        buttons.append([InlineKeyboardButton(text="Начать симуляцию", callback_data="action_simulate")])
        buttons.append([InlineKeyboardButton(text="Моя статистика", callback_data="action_stats")])
        buttons.append([InlineKeyboardButton(text="Рейтинг команды", callback_data="action_leaderboard")])
        if db.is_admin(user_id):
            buttons.append([InlineKeyboardButton(text="Панель управления", callback_data="action_admin")])
    else:
        buttons.append([InlineKeyboardButton(text="Зарегистрировать компанию", callback_data="action_register")])
        buttons.append([InlineKeyboardButton(text="Присоединиться по коду", callback_data="action_join")])
        buttons.append([InlineKeyboardButton(text="Быстрая симуляция", callback_data="action_simulate")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = TEXTS[lang]["welcome_menu"]

    if user and user.get("company_code"):
        company = db.get_company(user["company_code"])
        if company:
            text = f"Компания: <b>{company['name']}</b>\n\n{text}"

    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

# ============================================================
# РЕГИСТРАЦИЯ КОМПАНИИ
# ============================================================

@dp.callback_query(F.data == "action_register")
async def start_register(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await call.message.edit_text(TEXTS[lang]["ask_company_name"])
    await state.set_state(SimStates.register_company)

@dp.message(SimStates.register_company, F.text)
async def do_register_company(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    company_name = message.text.strip()
    if len(company_name) < 2 or len(company_name) > 100:
        await message.answer("Название должно быть от 2 до 100 символов.")
        return
    code = db.register_company(company_name, message.from_user.id)
    await message.answer(
        TEXTS[lang]["company_registered"].format(code=code),
        parse_mode="HTML"
    )
    await show_main_menu(message, lang, message.from_user.id)
    await state.set_state(SimStates.menu)

# ============================================================
# ПРИСОЕДИНЕНИЕ К КОМПАНИИ
# ============================================================

@dp.callback_query(F.data == "action_join")
async def start_join(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await call.message.edit_text(TEXTS[lang]["ask_company_code"])
    await state.set_state(SimStates.join_company)

@dp.message(SimStates.join_company, F.text)
async def do_join_company(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    code = message.text.strip().upper()
    success = db.join_company(message.from_user.id, message.from_user.full_name, code)
    if success:
        company = db.get_company(code)
        await message.answer(
            TEXTS[lang]["joined_company"].format(name=company["name"]),
            parse_mode="HTML"
        )
    else:
        await message.answer(TEXTS[lang]["invalid_code"])
    await show_main_menu(message, lang, message.from_user.id)
    await state.set_state(SimStates.menu)

# ============================================================
# СТАТИСТИКА
# ============================================================

@dp.callback_query(F.data == "action_stats")
async def show_stats(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    stats = db.get_user_stats(call.from_user.id)
    if stats["sessions"] == 0:
        text = TEXTS[lang]["no_stats"]
    else:
        text = TEXTS[lang]["stats_title"].format(
            sessions=stats["sessions"],
            avg=stats["avg_score"],
            best=stats["best_score"],
            worst=stats["worst_score"],
            ai_detect=stats["avg_ai_detect"],
        )
        user_sessions = db.get_user_sessions(call.from_user.id, limit=10)
        if user_sessions:
            text += "\nПоследние результаты:\n"
            for s in reversed(user_sessions):
                score = s.get("score", 0)
                bar = "#" * score + "." * (15 - score)
                text += f"[{bar}] {score}/15\n"
    await call.message.edit_text(text)

# ============================================================
# РЕЙТИНГ
# ============================================================

@dp.callback_query(F.data == "action_leaderboard")
async def show_leaderboard(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    user = db.get_user(call.from_user.id)
    if not user or not user.get("company_code"):
        await call.message.edit_text(TEXTS[lang]["no_company"])
        return
    company = db.get_company(user["company_code"])
    leaderboard = db.get_company_leaderboard(user["company_code"])
    if not leaderboard:
        await call.message.edit_text("Пока нет результатов.")
        return
    medals = ["🥇", "🥈", "🥉"]
    text = TEXTS[lang]["leaderboard_title"].format(company=company["name"])
    for i, entry in enumerate(leaderboard[:10]):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += TEXTS[lang]["leaderboard_row"].format(
            medal=medal,
            pos=i+1,
            name=entry["name"],
            avg=entry["avg_score"],
            best=entry["best_score"],
            sessions=entry["sessions"],
        )
    await call.message.edit_text(text)

# ============================================================
# ПАНЕЛЬ УПРАВЛЕНИЯ (АДМИН)
# ============================================================

@dp.callback_query(F.data == "action_admin")
async def show_admin_panel(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    if not db.is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    user = db.get_user(call.from_user.id)
    company_code = user["company_code"]
    company = db.get_company(company_code)
    users_count = sum(1 for u in db.users.values() if u.get("company_code") == company_code)
    sessions_count = len(db.get_company_sessions(company_code))
    text = TEXTS[lang]["admin_panel_title"].format(
        company=company["name"],
        code=company_code,
        users=users_count,
        sessions=sessions_count,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Последние отчеты", callback_data="admin_recent")],
        [InlineKeyboardButton(text="Рейтинг команды", callback_data="action_leaderboard")],
        [InlineKeyboardButton(text="Назад", callback_data="action_back_menu")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_recent")
async def show_recent_sessions(call: types.CallbackQuery, state: FSMContext):
    if not db.is_admin(call.from_user.id):
        await call.answer("Нет доступа")
        return
    user = db.get_user(call.from_user.id)
    company_code = user["company_code"]
    sessions = db.get_company_sessions(company_code, limit=10)
    if not sessions:
        await call.message.edit_text("Нет сессий.")
        return
    text = "ПОСЛЕДНИЕ СИМУЛЯЦИИ:\n\n"
    for s in sessions:
        text += (
            f"{s.get('user_name', 'N/A')} | "
            f"{s.get('score', 0)}/15 | "
            f"AI:{s.get('ai_detect_percent', 0)}% | "
            f"{s.get('completed_at', '')[:16]}\n"
        )
    await call.message.edit_text(text)

@dp.callback_query(F.data == "action_back_menu")
async def back_to_menu(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await show_main_menu(call.message, lang, call.from_user.id, edit=True)

# ============================================================
# СИМУЛЯЦИЯ — ВЫБОР РОЛИ
# ============================================================

@dp.callback_query(F.data == "action_simulate")
async def start_simulation(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    roles = TEXTS[lang]["roles"]
    buttons = [[InlineKeyboardButton(text=r, callback_data=f"role_{r}")] for r in roles]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text(TEXTS[lang]["choose_role"], reply_markup=kb)
    await state.set_state(SimStates.role)

@dp.callback_query(SimStates.role, F.data.startswith("role_"))
async def set_role(call: types.CallbackQuery, state: FSMContext):
    role = call.data.removeprefix("role_")
    await state.update_data(role=role)
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await call.message.edit_text(TEXTS[lang]["ask_niche"])
    await state.set_state(SimStates.niche)

# ============================================================
# СИМУЛЯЦИЯ — ВВОД НИШИ
# ============================================================

@dp.message(SimStates.niche, F.text)
async def set_niche(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    niche = message.text.strip()
    if len(niche) < MIN_MESSAGE_LENGTH:
        await message.answer(TEXTS[lang]["too_short"])
        return

    client_state = generate_client_state()
    await state.update_data(
        niche=niche,
        history=[],
        step=0,
        client_state=client_state,
        response_times=[],
        start_time=time.time(),
    )

    await message.answer(TEXTS[lang]["sim_start"])

    system_instruction = build_full_system_instruction(lang, data["role"], niche, client_state)
    opening_prompt = "Менеджер только что написал тебе первое сообщение. Жди его."
    first_response = await generate_response(opening_prompt, system_instruction=system_instruction)

    history = [{"role": "client", "content": first_response}]
    await state.update_data(history=history, system_instruction=system_instruction)

    await message.answer(first_response)
    await state.set_state(SimStates.dialogue)

# ============================================================
# СИМУЛЯЦИЯ — ДИАЛОГ
# ============================================================

@dp.message(SimStates.dialogue, F.text)
async def handle_dialogue(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    step = data.get("step", 0)
    history = data.get("history", [])
    response_times = data.get("response_times", [])

    if len(message.text.strip()) < MIN_MESSAGE_LENGTH:
        await message.answer(TEXTS[lang]["too_short"])
        return

    msg_start = time.time()
    history.append({"role": "manager", "content": message.text.strip()})
    step += 1

    progress_text = TEXTS[lang]["progress"].format(current=step, total=MAX_STEPS)
    await message.answer(progress_text)

    if step >= MAX_STEPS:
        await state.update_data(history=history, step=step, response_times=response_times)
        await finish_simulation(message, state)
        return

    system_instruction = data.get("system_instruction", "")
    client_response = await generate_response(history, system_instruction=system_instruction)

    response_time = time.time() - msg_start
    response_times.append(response_time)

    history.append({"role": "client", "content": client_response})
    await state.update_data(history=history, step=step, response_times=response_times)
    await message.answer(client_response)

# ============================================================
# ЗАВЕРШЕНИЕ СИМУЛЯЦИИ
# ============================================================

async def finish_simulation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    gender = data.get("gender", "m")
    history = data.get("history", [])
    niche = data.get("niche", "")
    role = data.get("role", "")
    response_times = data.get("response_times", [])

    await message.answer(TEXTS[lang]["sim_end"])
    await message.answer(TEXTS[lang]["analyzing"])

    manager_messages = [m["content"] for m in history if m["role"] == "manager"]

    judge_system = get_judge_system_instruction(gender, lang, niche)
    dialogue_text = "\n".join([
        f"{'Менеджер' if m['role'] == 'manager' else 'Клиент'}: {m['content']}"
        for m in history
    ])

    verdict, ai_result = await asyncio.gather(
        generate_response(
            f"Вот диалог:\n\n{dialogue_text}\n\nВынеси вердикт.",
            system_instruction=judge_system,
            temperature=0.4
        ),
        run_ai_detection(manager_messages)
    )

    score = extract_score_from_verdict(verdict)
    avg_time = sum(response_times) / len(response_times) if response_times else 0

    user = db.get_user(message.from_user.id)
    company_code = user.get("company_code") if user else None
    company = db.get_company(company_code) if company_code else None

    session_data = {
        "user_id": message.from_user.id,
        "user_name": message.from_user.full_name,
        "company_code": company_code,
        "company_name": company["name"] if company else "Без компании",
        "niche": niche,
        "role": role,
        "gender": gender,
        "lang": lang,
        "history": history,
        "verdict": verdict,
        "score": score,
        "ai_detect_percent": ai_result["percent"],
        "ai_detect_analysis": ai_result["full_analysis"],
        "response_times": response_times,
        "avg_response_time": avg_time,
    }

    db.save_session(session_data)

    # Пользователю — только короткое сообщение
    end_msg = TEXTS[lang]["end_user_m"] if gender == "m" else TEXTS[lang]["end_user_f"]
    await message.answer(end_msg)

    # Полные результаты — тебе на почту + админам компании в ТГ
    await deliver_results(session_data)

    await state.clear()
    await show_main_menu(message, lang, message.from_user.id)

# ============================================================
# ЛЕНДИНГ (FastAPI)
# ============================================================

LANDING_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AuditCore AI | Отбор менеджеров по продажам</title>
    <style>
        :root { --bg-color: #050a15; --text-main: #ffffff; --text-muted: #8b9bb4; --accent-glow: #00d2ff; --accent-dark: #005ce6; }
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }
        body { background-color: var(--bg-color); color: var(--text-main); line-height: 1.6; overflow-x: hidden; }
        header { display: flex; justify-content: space-between; align-items: center; padding: 20px 5%; background: rgba(5, 10, 21, 0.8); backdrop-filter: blur(10px); position: fixed; width: 100%; top: 0; z-index: 100; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }
        .contact-btn { background: linear-gradient(90deg, var(--accent-dark), var(--accent-glow)); color: #fff; padding: 10px 24px; text-decoration: none; border-radius: 8px; font-weight: 600; transition: all 0.3s ease; box-shadow: 0 4px 15px rgba(0, 210, 255, 0.2); }
        .contact-btn:hover { box-shadow: 0 4px 25px rgba(0, 210, 255, 0.4); transform: translateY(-2px); }
        .hero { padding: 150px 5% 100px; display: flex; flex-direction: column; align-items: center; text-align: center; max-width: 1000px; margin: 0 auto; }
        .logo { width: 160px; aspect-ratio: 1 / 1; object-fit: cover; border-radius: 24px; box-shadow: 0 0 30px rgba(0, 210, 255, 0.15); border: 1px solid rgba(0, 210, 255, 0.3); margin-bottom: 30px; }
        .mvp-badge { background: rgba(0, 210, 255, 0.1); color: var(--accent-glow); padding: 6px 16px; border-radius: 20px; font-size: 0.85rem; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 20px; border: 1px solid rgba(0, 210, 255, 0.2); display: inline-block; }
        h1 { font-size: 3.5rem; margin-bottom: 20px; background: linear-gradient(90deg, #fff, var(--accent-glow)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; line-height: 1.2; }
        .subtitle { font-size: 1.2rem; color: var(--text-muted); max-width: 600px; margin-bottom: 50px; }
        .content-section { display: grid; grid-template-columns: 1fr 1fr; gap: 40px; padding: 0 5% 100px; max-width: 1200px; margin: 0 auto; }
        .card { background: rgba(255, 255, 255, 0.02); border: 1px solid rgba(255, 255, 255, 0.05); padding: 40px; border-radius: 16px; transition: transform 0.3s ease; }
        .card:hover { transform: translateY(-5px); border-color: rgba(0, 210, 255, 0.2); background: rgba(255, 255, 255, 0.03); }
        .card h3 { color: var(--accent-glow); font-size: 1.5rem; margin-bottom: 20px; }
        .card p { color: var(--text-muted); font-size: 1.05rem; }
        footer { text-align: center; padding: 40px 20px; border-top: 1px solid rgba(255, 255, 255, 0.05); color: var(--text-muted); font-size: 0.9rem; }
        @media (max-width: 768px) { h1 { font-size: 2.5rem; } .content-section { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <header>
        <div style="font-weight: 700; font-size: 1.2rem; letter-spacing: 1px;">AuditCore AI</div>
        <a href="mailto:monkifani@gmail.com" class="contact-btn">Связаться с нами</a>
    </header>
    <section class="hero">
        <img src="/1775141753778.png" alt="AuditCore AI Logo" class="logo">
        <div class="mvp-badge">MVP Status</div>
        <h1>Аудит и отбор сейлз-менеджеров</h1>
        <p class="subtitle">Интеллектуальная система оценки компетенций отдела продаж. Мы не тренируем - мы находим тех, кто действительно умеет продавать.</p>
    </section>
    <section class="content-section">
        <div class="card">
            <h3>Проблема</h3>
            <p>Стандартные собеседования и резюме не показывают реальных навыков менеджера по продажам. Компании тратят месяцы и тысячи долларов на онбординг кандидатов, которые в итоге "сливают" лидов при первых же реальных возражениях клиента.</p>
        </div>
        <div class="card">
            <h3>Решение AuditCore AI</h3>
            <p>Глубокая ИИ-симуляция рабочих ситуаций. Кандидат проходит через стресс-тесты и сложные диалоги с нашим ИИ-клиентом. Система анализирует каждое слово, отработку возражений и закрытие сделки, выдавая вам детализированный аудит компетенций еще до этапа найма.</p>
        </div>
    </section>
    <footer>
        <p>Все права защищены 2026 AuditCore AI</p>
    </footer>
</body>
</html>
"""

# ============================================================
# FASTAPI + LIFESPAN (исправлено для Render)
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"Webhook установлен: {WEBHOOK_URL}")
    else:
        logging.warning("WEBHOOK_URL не задан — webhook не установлен")
    yield
    # Shutdown
    await bot.delete_webhook()
    logging.info("Webhook удален, бот остановлен")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def render_landing():
    return HTMLResponse(content=LANDING_HTML)

@app.get("/1775141753778.png")
async def serve_logo():
    return FileResponse("1775141753778.png")

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
