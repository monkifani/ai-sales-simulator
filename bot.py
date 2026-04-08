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
from email.mime.application import MIMEApplication
from collections import defaultdict
from google import genai
from google.genai import types as genai_types
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_EMAIL = "monkifani@gmail.com"
GMAIL_PASS = os.getenv("GMAIL_PASSWORD") # Пароль приложения Gmail

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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ОТПРАВКА ПОЧТЫ)
# ============================================================

def send_email_report(receiver_email, subject, body, pdf_content, filename):
    """Отправка отчета на почту"""
    if not GMAIL_PASS:
        logging.error("GMAIL_PASSWORD not set")
        return False
    
    try:
        msg = MIMEMultipart()
        msg['From'] = "AuditCore AI <noreply@gmail.com>"
        msg['To'] = receiver_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'plain'))

        part = MIMEApplication(pdf_content, Name=filename)
        part['Content-Disposition'] = f'attachment; filename="{filename}"'
        msg.attach(part)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(ADMIN_EMAIL, GMAIL_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        logging.error(f"Email error: {e}")
        return False

def clean_thinking_tags(text):
    return re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL).strip()

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
        "end_user_m": "Вы завершили симуляцию. Ваши результаты отправлены руководству для ознакомления.",
        "end_user_f": "Вы завершили симуляцию. Ваши результаты отправлены руководству для ознакомления.",
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
        "end_user_m": "Сiз симуляцияны аяқтадыңыз. Натижелерiңiз басшылыққа жiберiлдi.",
        "end_user_f": "Сiз симуляцияны аяқтадыңыз. Натижелерiңiз басшылыққа жiберiлдi.",
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
        result = clean_thinking_tags(result)
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
# КОМАНДА /start С ПРИВЕТСТВИЕМ
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
# СТАТИСТИКА И РЕЙТИНГ
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
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "action_leaderboard")
async def show_leaderboard(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    user = db.get_user(call.from_user.id)
    if not user or not user.get("company_code"):
        await call.message.answer(TEXTS[lang]["no_company"])
        return
    company = db.get_company(user["company_code"])
    lb = db.get_company_leaderboard(user["company_code"])
    text = TEXTS[lang]["leaderboard_title"].format(company=company["name"])
    for i, row in enumerate(lb[:10]):
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else "👤"
        text += TEXTS[lang]["leaderboard_row"].format(
            medal=medal, pos=i+1, name=row["name"], avg=row["avg_score"],
            best=row["best_score"], sessions=row["sessions"]
        )
    await call.message.answer(text)
    await call.answer()

# ============================================================
# ЛОГИКА СИМУЛЯЦИИ
# ============================================================

@dp.callback_query(F.data == "action_simulate")
async def start_sim(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=role, callback_data=f"role_{role}")]
        for role in TEXTS[lang]["roles"]
    ])
    await call.message.edit_text(TEXTS[lang]["choose_role"], reply_markup=kb)
    await state.set_state(SimStates.role)

@dp.callback_query(F.data.startswith("role_"))
async def set_role(call: types.CallbackQuery, state: FSMContext):
    role = call.data.replace("role_", "")
    await state.update_data(role=role)
    data = await state.get_data()
    lang = data["lang"]
    await call.message.edit_text(TEXTS[lang]["ask_niche"])
    await state.set_state(SimStates.niche)

@dp.message(SimStates.niche, F.text)
async def set_niche(message: types.Message, state: FSMContext):
    niche = message.text.strip()
    await state.update_data(niche=niche, history=[], response_times=[], start_time=time.time())
    data = await state.get_data()
    lang = data["lang"]
    role = data["role"]
    client_state = generate_client_state()
    await state.update_data(client_state=client_state)
    
    sys_instr = build_full_system_instruction(lang, role, niche, client_state)
    first_msg = await generate_response("Привет", system_instruction=sys_instr)
    
    history = [{"role": "client", "content": first_msg}]
    await state.update_data(history=history, last_ai_time=time.time())
    
    await message.answer(TEXTS[lang]["sim_start"])
    await message.answer(f"<b>{role}:</b> {first_msg}", parse_mode="HTML")
    await state.set_state(SimStates.dialogue)

@dp.message(SimStates.dialogue, F.text)
async def handle_dialogue(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data["lang"]
    history = data["history"]
    
    if len(message.text) < MIN_MESSAGE_LENGTH:
        await message.answer(TEXTS[lang]["too_short"])
        return

    # Замер времени ответа менеджера
    resp_time = time.time() - data.get("last_ai_time", time.time())
    response_times = data.get("response_times", [])
    response_times.append(resp_time)
    
    history.append({"role": "manager", "content": message.text})
    
    if len([m for m in history if m["role"] == "manager"]) >= MAX_STEPS:
        await finish_simulation(message, state)
        return

    # Ответ клиента
    sys_instr = build_full_system_instruction(lang, data["role"], data["niche"], data["client_state"])
    client_reply = await generate_response(history, system_instruction=sys_instr)
    
    history.append({"role": "client", "content": client_reply})
    await state.update_data(history=history, response_times=response_times, last_ai_time=time.time())
    
    progress = len([m for m in history if m["role"] == "manager"])
    prog_text = TEXTS[lang]["progress"].format(current=progress, total=MAX_STEPS)
    
    await message.answer(f"<i>{prog_text}</i>\n\n<b>{data['role']}:</b> {client_reply}", parse_mode="HTML")

async def finish_simulation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data["lang"]
    gender = data.get("gender", "m")
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    
    await message.answer(TEXTS[lang]["sim_end"])
    await message.answer(TEXTS[lang]["analyzing"])
    
    # 1. AI Детект
    manager_msgs = [m["content"] for m in data["history"] if m["role"] == "manager"]
    ai_detect = await run_ai_detection(manager_msgs)
    
    # 2. Судейство
    judge_instr = get_judge_system_instruction(gender, lang, data["niche"])
    dialogue_str = "\n".join([f"{m['role']}: {m['content']}" for m in data["history"]])
    verdict = await generate_response(f"ПРОАНАЛИЗИРУЙ ДИАЛОГ:\n\n{dialogue_str}", system_instruction=judge_instr, temperature=0.3)
    
    score = extract_score_from_verdict(verdict)
    
    # 3. Сбор данных
    user_info = db.get_user(user_id)
    company_code = user_info.get("company_code") if user_info else None
    
    session_result = {
        "user_id": user_id,
        "user_name": user_name,
        "company_code": company_code,
        "niche": data["niche"],
        "role": data["role"],
        "history": data["history"],
        "score": score,
        "verdict": verdict,
        "ai_detect_percent": ai_detect["percent"],
        "ai_detect_analysis": ai_detect["full_analysis"],
        "response_times": data["response_times"],
        "avg_response_time": sum(data["response_times"])/len(data["response_times"]) if data["response_times"] else 0
    }
    
    db.save_session(session_result)
    pdf_data = generate_pdf_report(session_result)
    
    # 4. ОТПРАВКА РЕЗУЛЬТАТОВ (ОСНОВНАЯ ПРАВКА)
    
    # А) Пользователю - только сообщение о завершении
    end_key = "end_user_m" if gender == "m" else "end_user_f"
    await message.answer(TEXTS[lang][end_key])

    # Б) Тебе на почту (monkifani@gmail.com)
    email_body = f"Новая симуляция от {user_name}.\nКомпания: {company_code or 'Нет'}\nБалл: {score}/15\nAI-детект: {ai_detect['percent']}%"
    subject = f"AuditCore Report: {user_name} ({score}/15)"
    send_email_report(ADMIN_EMAIL, subject, email_body, pdf_data, f"report_{user_id}.txt")

    # В) Компании (админу в ТГ)
    if company_code:
        company = db.get_company(company_code)
        if company and company.get("admin_ids"):
            admin_msg = (
                f"🔔 <b>Новый аудит менеджера!</b>\n\n"
                f"Менеджер: {user_name}\n"
                f"Ниша: {data['niche']}\n"
                f"Результат: {score}/15\n"
                f"AI-детект: {ai_detect['percent']}%\n\n"
                f"<b>Разбор:</b>\n{verdict}"
            )
            for admin_id in company["admin_ids"]:
                try:
                    await bot.send_message(admin_id, admin_msg, parse_mode="HTML")
                    await bot.send_document(
                        admin_id, 
                        BufferedInputFile(pdf_data, filename=f"Audit_{user_name}_{datetime.now().strftime('%d%m')}.txt"),
                        caption="Полный PDF-отчет симуляции"
                    )
                except Exception as e:
                    logging.error(f"Failed to send to admin {admin_id}: {e}")

    await show_main_menu(message, lang, user_id)
    await state.set_state(SimStates.menu)

# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================

@dp.callback_query(F.data == "action_admin")
async def show_admin_panel(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    if not db.is_admin(user_id):
        await call.answer("Доступ запрещен", show_alert=True)
        return
    
    user = db.get_user(user_id)
    lang = (await state.get_data()).get("lang", "ru")
    company = db.get_company(user["company_code"])
    sessions = db.get_company_sessions(user["company_code"])
    users_count = sum(1 for u in db.users.values() if u.get("company_code") == user["company_code"])
    
    text = TEXTS[lang]["admin_panel_title"].format(
        company=company["name"],
        code=user["company_code"],
        users=users_count,
        sessions=len(sessions)
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад в меню", callback_data="back_to_menu")]
    ])
    
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await show_main_menu(call.message, data.get("lang", "ru"), call.from_user.id, edit=True)

# ============================================================
# WEBHOOK & SERVER
# ============================================================

app = FastAPI()

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
async def index():
    return HTMLResponse("<h1>AuditCore AI Bot is Running</h1>")

async def main():
    if WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        logging.info(f"Webhook set to: {WEBHOOK_URL}")
    else:
        logging.info("Starting polling...")
        await bot.delete_webhook()
        await dp.start_polling(bot)

if __name__ == "__main__":
    import uvicorn
    if IS_PROD or IS_RAILWAY or IS_RENDER:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    else:
        asyncio.run(main())
