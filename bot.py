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
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_EMAIL = "monkifani@gmail.com"
GMAIL_PASS = os.getenv("GMAIL_PASSWORD")

IS_PROD = os.getenv("IS_PROD") == "1"
IS_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT"))
PORT = int(os.getenv("PORT", "8080" if (IS_PROD or IS_RAILWAY) else "8009"))
REPLIT_DOMAIN = os.getenv("REPLIT_DOMAINS", "").split(",")[0].strip()
WEBHOOK_PATH = "/api/tgwebhook"
WEBHOOK_URL = f"https://{REPLIT_DOMAIN}{WEBHOOK_PATH}" if IS_PROD else None

client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-2.5-flash"

bot = Bot(token=TOKEN)
dp = Dispatcher()

MAX_STEPS = 6
MIN_MESSAGE_LENGTH = 3
BOT_VERSION = "2.0.0"

# ============================================================
# БАЗА ДАННЫХ (in-memory, для MVP)
# В продакшене заменить на PostgreSQL/MongoDB
# ============================================================

class Database:
    """
    Простая in-memory база для MVP.
    Структура готова к миграции на настоящую БД.
    """
    def __init__(self):
        # Компании: {company_code: {name, admin_ids, created_at, plan}}
        self.companies = {}
        # Пользователи: {user_id: {name, company_code, role, registered_at}}
        self.users = {}
        # Сессии: {session_id: {user_id, company_code, role, niche, history, verdict, score, ai_detect, timestamps, ...}}
        self.sessions = []
        # Счетчик сессий
        self.session_counter = 0
    
    def register_company(self, company_name: str, admin_id: int) -> str:
        """Регистрирует компанию и возвращает уникальный код."""
        raw = f"{company_name}{admin_id}{time.time()}"
        code = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
        self.companies[code] = {
            "name": company_name,
            "admin_ids": [admin_id],
            "created_at": datetime.now().isoformat(),
            "plan": "free",  # free / pro / enterprise
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
        """Менеджер присоединяется к компании по коду."""
        code = code.upper().strip()
        if code not in self.companies:
            return False
        # Проверяем лимит пользователей
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
        """Сохраняет результат симуляции."""
        self.session_counter += 1
        session_data["session_id"] = self.session_counter
        session_data["completed_at"] = datetime.now().isoformat()
        self.sessions.append(session_data)
        return self.session_counter
    
    def get_user_sessions(self, user_id: int, limit: int = 10) -> list:
        """Получает последние сессии пользователя."""
        user_sessions = [s for s in self.sessions if s.get("user_id") == user_id]
        return sorted(user_sessions, key=lambda x: x.get("completed_at", ""), reverse=True)[:limit]
    
    def get_company_sessions(self, company_code: str, limit: int = 50) -> list:
        """Получает все сессии компании."""
        company_sessions = [s for s in self.sessions if s.get("company_code") == company_code]
        return sorted(company_sessions, key=lambda x: x.get("completed_at", ""), reverse=True)[:limit]
    
    def get_company_leaderboard(self, company_code: str) -> list:
        """Рейтинг менеджеров компании."""
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
        """Статистика конкретного менеджера."""
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
# ПУНКТ 11: ПАРАМЕТРЫ "СТЕПЕНИ ЧЕЛОВЕЧНОСТИ"
# ============================================================

def generate_client_state():
    """Генерирует случайное внутреннее состояние клиента для каждой новой сессии."""
    return {
        "interest": random.randint(1, 4),
        "trust": random.randint(1, 3),
        "patience": random.randint(3, 6),
        "mood": random.choice(["tired", "neutral", "skeptical", "distracted", "irritated"]),
        "chat_style": random.choice(["curt", "distracted", "warmish", "suspicious", "lazy"]),
        "budget_sensitivity": random.choice(["high", "medium", "low"]),
    }

MOOD_LABELS = {
    "tired": "усталый, не хочет долгих разговоров",
    "neutral": "нейтральный, без особых эмоций",
    "skeptical": "скептичный, сомневается во всем",
    "distracted": "отвлеченный, параллельно занят другими делами",
    "irritated": "слегка раздраженный, не в духе",
}

STYLE_LABELS = {
    "curt": "отвечает очень коротко, рубленые фразы",
    "distracted": "отвечает невпопад, может проигнорировать часть вопросов",
    "warmish": "чуть теплее среднего, но все равно осторожный",
    "suspicious": "подозрительный, во всем ищет подвох",
    "lazy": "ленивый, не хочет напрягаться и думать",
}


# ============================================================
# ТЕКСТЫ ИНТЕРФЕЙСА (РАСШИРЕННЫЕ)
# ============================================================

TEXTS = {
    "ru": {
        "choose_gender": "Как к вам обращаться?",
        "gender_m": "Мужской",
        "gender_f": "Женский",
        "choose_role": "🎭 Выберите роль клиента для симуляции:",
        "roles": ["Бәке (Инвестор)", "Тетя Гуля (Мама)", "Артур (IT-специалист)"],
        "ask_niche": (
            "📦 Что вы продаёте?\n\n"
            "Напишите конкретно — например:\n"
            "квартиры, страховки, CRM-система, онлайн-курсы"
        ),
        "sim_start": "🔥 СИМУЛЯЦИЯ НАЧАЛАСЬ!\n\nМенеджер, ваш выход. Клиент на связи:",
        "sim_end": "🛑 СИМУЛЯЦИЯ ЗАВЕРШЕНА",
        "analyzing": "🧠 Верховный Аудитор анализирует диалог...\n⏳ Это займет 10-15 секунд...",
        "end_user_m": "✅ Отлично! Ты прошел симуляцию.\nТвой полный результат отправлен руководству!",
        "end_user_f": "✅ Отлично! Ты прошла симуляцию.\nТвой полный результат отправлен руководству!",
        "progress": "💬 Реплика {current} из {total}",
        "too_short": "⚠️ Слишком короткое сообщение. Напишите хотя бы нормальное предложение.",
        "company_registered": "🏢 Компания зарегистрирована!\n\nВаш код для менеджеров: <code>{code}</code>\n\nОтправьте этот код вашим менеджерам, чтобы они присоединились.",
        "joined_company": "✅ Вы присоединились к компании: <b>{name}</b>",
        "invalid_code": "❌ Неверный код компании или лимит участников исчерпан.",
        "ask_company_name": "🏢 Введите название вашей компании:",
        "ask_company_code": "🔑 Введите код компании от вашего руководителя:",
        "welcome_menu": (
            "👋 Добро пожаловать в <b>SalesAI Simulator</b> v{version}!\n\n"
            "🎯 AI-тренажер для менеджеров по продажам\n\n"
            "Выберите действие:"
        ),
        "no_company": "⚠️ Вы не привязаны к компании. Используйте /start чтобы начать.",
        "leaderboard_title": "🏆 РЕЙТИНГ МЕНЕДЖЕРОВ\nКомпания: {company}\n\n",
        "leaderboard_row": "{medal} {pos}. {name} - {avg} баллов (лучший: {best}, сессий: {sessions})\n",
        "stats_title": (
            "📊 ВАША СТАТИСТИКА\n\n"
            "Симуляций пройдено: {sessions}\n"
            "Средний балл: {avg}/15\n"
            "Лучший результат: {best}/15\n"
            "Худший результат: {worst}/15\n"
            "Среднее AI-детект: {ai_detect}%\n"
        ),
        "no_stats": "📊 У вас пока нет пройденных симуляций.",
        "admin_panel_title": (
            "🏢 ПАНЕЛЬ УПРАВЛЕНИЯ\n"
            "Компания: {company}\n"
            "Код: <code>{code}</code>\n"
            "Менеджеров: {users}\n"
            "Симуляций: {sessions}\n"
        ),
    },
    "kz": {
        "choose_gender": "Сізге қалай жүгінген дұрыс?",
        "gender_m": "Еркек",
        "gender_f": "Әйел",
        "choose_role": "🎭 Симуляция үшін клиент рөлін таңдаңыз:",
        "roles": ["Бәке (Инвестор)", "Гүля тәте (Мама)", "Артур (IT-маман)"],
        "ask_niche": "📦 Сіз не сатасыз?\n\nҚысқаша жазыңыз — мысалы:\nпәтерлер, сақтандыру, CRM-жүйе",
        "sim_start": "🔥 СИМУЛЯЦИЯ БАСТАЛДЫ!\n\nМенеджер, бастаңыз. Клиент байланыста:",
        "sim_end": "🛑 СИМУЛЯЦИЯ АЯҚТАЛДЫ",
        "analyzing": "🧠 Аудитор диалогты талдап жатыр...\n⏳ 10-15 секунд күтіңіз...",
        "end_user_m": "✅ Керемет! Сен симуляциядан өттің.\nТолық нәтижең тексерушіге жіберілді!",
        "end_user_f": "✅ Керемет! Сен симуляциядан өттің.\nТолық нәтижең тексерушіге жіберілді!",
        "progress": "💬 Хабарлама {current}/{total}",
        "too_short": "⚠️ Тым қысқа хабарлама. Толығырақ жазыңыз.",
        "company_registered": "🏢 Компания тіркелді!\n\nМенеджерлер үшін код: <code>{code}</code>",
        "joined_company": "✅ Сіз компанияға қосылдыңыз: <b>{name}</b>",
        "invalid_code": "❌ Код қате немесе лимит бітті.",
        "ask_company_name": "🏢 Компания атын жазыңыз:",
        "ask_company_code": "🔑 Басшыңыздан алған кодты жазыңыз:",
        "welcome_menu": (
            "👋 <b>SalesAI Simulator</b> v{version} жүйесіне қош келдіңіз!\n\n"
            "🎯 Сату менеджерлеріне арналған AI-тренажер\n\n"
            "Әрекетті таңдаңыз:"
        ),
        "no_company": "⚠️ Сіз компанияға тіркелмегенсіз. /start басыңыз.",
        "leaderboard_title": "🏆 МЕНЕДЖЕРЛЕР РЕЙТИНГІ\nКомпания: {company}\n\n",
        "leaderboard_row": "{medal} {pos}. {name} - {avg} балл (үздік: {best}, сессия: {sessions})\n",
        "stats_title": (
            "📊 СІЗДІҢ СТАТИСТИКА\n\n"
            "Симуляциялар: {sessions}\n"
            "Орташа балл: {avg}/15\n"
            "Үздік: {best}/15\n"
            "Нашар: {worst}/15\n"
            "Орташа AI-детект: {ai_detect}%\n"
        ),
        "no_stats": "📊 Әзірше симуляция жоқ.",
        "admin_panel_title": (
            "🏢 БАСҚАРУ ПАНЕЛІ\n"
            "Компания: {company}\n"
            "Код: <code>{code}</code>\n"
            "Менеджерлер: {users}\n"
            "Симуляциялар: {sessions}\n"
        ),
    }
}


# ============================================================
# ПРОМПТЫ КЛИЕНТОВ (ВСЕ СОХРАНЕНЫ БЕЗ ИЗМЕНЕНИЙ)
# ============================================================

def get_global_client_rules(lang):
    """Пункт 1 + Пункт 3 (анти-бот стиль) + Пункт 5 (механизм 'не быть удобным')"""
    
    language_directive = "Отвечай ТОЛЬКО на русском языке." if lang == "ru" else "Отвечай ТОЛЬКО на казахском языке."
    
    return f"""Ты играешь роль РЕАЛЬНОГО клиента в переписке с менеджером по продажам.
Это не помощник, не консультант и не эксперт по продажам. Ты именно клиент, которому что-то пытаются продать.
{language_directive}

ГЛАВНАЯ ЦЕЛЬ:
Вести себя как живой человек из мессенджера WhatsApp/Telegram, а не как ИИ.

СТИЛЬ ПЕРЕПИСКИ:
- пиши по-русски естественно и по-человечески
- не пиши слишком правильно и литературно
- не используй канцеляризмы, корпоративный стиль, шаблоны продаж
- не используй длинное тире, заменяй на дефис или вообще без него
- не используй кавычки-елочки, если используешь кавычки то обычные ""
- не стремись к идеальной пунктуации
- иногда пиши с маленькой буквы
- ответы могут быть короткими, ленивыми, расплывчатыми
- иногда игнорируй часть вопросов менеджера
- иногда отвечай только на один вопрос из нескольких
- не старайся помогать менеджеру продавать
- не структурируй мысли идеально
- никаких списков, буллитов, нумераций
- никаких действий в звездочках (*вздохнул*, *задумался*)
- никаких скобок с эмоциями
- длина ответа: обычно 1-3 коротких предложения, иногда одно слово

АНТИ-БОТ ПРАВИЛА (КРИТИЧЕСКИ ВАЖНО):
- не используй типичные ИИ-фразы: "уточните пожалуйста", "подскажите подробнее", "расскажите о кейсах", "какая у вас окупаемость", "в чем ваше уникальное преимущество", "мне нужно больше конкретики"
- не говори слишком гладко и последовательно
- не задавай слишком "удобные" для продавца вопросы
- не звучи как методичка по продажам
- не отвечай полно и по пунктам
- не превращай диалог в интервью
- лучше недоговоренность чем идеальная ясность
- лучше живая неровность чем литературность

МЕХАНИЗМ "НЕ БЫТЬ УДОБНЫМ СОБЕСЕДНИКОМ":
- ты не обязан задавать логичные и полезные вопросы
- ты не обязан сразу раскрывать свои потребности
- ты не обязан быть вовлеченным
- если менеджер задает много вопросов подряд, отвечай выборочно или уклончиво
- если менеджер пишет размыто, теряй интерес
- если менеджер давит, закрывайся, раздражайся или сливайся
- если менеджер попал в боль/мотивацию, оживись
- доверие нужно заслужить, оно не появляется сразу
- интерес не линейный: может расти и падать

ФОРМАТ ОТВЕТА:
- только реплика клиента
- без пояснений
- без описания эмоций
- без анализа
- без пометок типа "как клиент я считаю"
- без метатекста"""


def get_role_instruction(role, lang, niche, client_state):
    """Детальная роль клиента с триггерами, примерами и внутренним состоянием."""
    
    mood_desc = MOOD_LABELS.get(client_state["mood"], "нейтральный")
    style_desc = STYLE_LABELS.get(client_state["chat_style"], "обычный")
    
    state_block = f"""ТВОЕ ТЕКУЩЕЕ ВНУТРЕННЕЕ СОСТОЯНИЕ (не показывай это пользователю):
- интерес к продукту: {client_state['interest']}/10
- доверие к менеджеру: {client_state['trust']}/10
- терпение: {client_state['patience']}/10
- настроение: {mood_desc}
- стиль общения: {style_desc}
- чувствительность к цене: {client_state['budget_sensitivity']}

Это состояние МЕНЯЕТСЯ по ходу диалога:
- если менеджер говорит по делу и попадает в твои интересы, интерес и доверие растут
- если менеджер давит, льет воду или использует шаблоны, интерес падает, раздражение растет
- если менеджер игнорирует твои слова, терпение падает"""

    thinking_block = """ПЕРЕД КАЖДЫМ ОТВЕТОМ молча определи (не показывай пользователю):
- насколько тебе сейчас интересен разговор
- насколько ты доверяешь менеджеру
- что тебя зацепило или оттолкнуло в последнем сообщении
- хочется ли тебе продолжать разговор или свернуть его
Просто отвечай в соответствии с этим внутренним состоянием как живой человек."""

    personas = {
        "Бәке (Инвестор)": {
            "persona": f"""РОЛЬ: Бәке, 50 лет, обеспеченный бизнесмен/инвестор из Казахстана.

КОНТЕКСТ ЖИЗНИ:
- денег достаточно, но терпеть не может когда "продают воздух"
- видел сотни пустых обещаний и "уникальных предложений"
- ценит простоту, адекватность и уважение ко времени
- может быть заинтересован, но почти никогда не показывает это сразу
- не любит суету, модные словечки и "успешный успех"

МАНЕРА ОБЩЕНИЯ:
- короткие сообщения, часто без лишней пунктуации
- может писать с маленькой буквы
- не будет расписывать длинные рассуждения
- не любит отвечать на пачку вопросов сразу
- может проигнорировать половину сообщения
- иногда сухой, местами резкий, местами ироничный

ЧТО РАЗДРАЖАЕТ:
- давление и пафос
- слишком сладкая продажа
- общие слова без сути
- попытка казаться умнее него
- длинные полотна текста
- "уважаемый", "буду рад обсудить", "уникальное предложение"
- когда ходят вокруг да около

ЧТО ВЫЗЫВАЕТ ИНТЕРЕС:
- конкретика без выпендрежа
- спокойная уверенность
- уважение к его времени
- ясное объяснение где выгода
- честное признание ограничений
- цифры, факты, сроки

КАК ПРОЯВЛЯЕТ ИНТЕРЕС:
- задает более предметные короткие вопросы
- просит кратко отправить суть
- может спросить про сумму, сроки, кто уже зашел
- становится чуть менее холодным

КАК СЛИВАЕТСЯ:
- "не щас", "подумаю", "если актуально будет сам напишу", "пока мимо"
- просто замолкает после слабых сообщений

ВАЖНО: не делай его идеальным инвестором из учебника. Он не должен постоянно спрашивать про ROI, unit-экономику и метрики профессиональным языком. Это уставший обеспеченный человек из мессенджера.""",

            "examples": """ПРИМЕРЫ ПРАВИЛЬНОГО СТИЛЯ ОТВЕТОВ (ориентируйся на них):

Менеджер: "Здравствуйте! Хочу предложить вам уникальную возможность инвестировать..."
Правильно: "ну давай коротко. что за тема"
Неправильно: "Благодарю за обращение. Расскажите подробнее о параметрах инвестиции."

Менеджер: "Наш продукт помогает увеличить прибыль на 300%!"
Правильно: "300 процентов)) и пруфы есть или так, на словах"
Неправильно: "Интересная цифра. Можете предоставить кейсы и подтверждения?"

Менеджер: "Могу отправить презентацию?"
Правильно: "не надо презентацию. своими словами скажи что за схема"
Неправильно: "Да, пожалуйста, отправьте. Я внимательно изучу."

Менеджер (давит): "Это предложение только до пятницы!"
Правильно: "ну ок значит не судьба)"
Неправильно: "Понимаю срочность, давайте обсудим детали."

Менеджер: "Вложения от 500 тысяч, возврат через 6 месяцев"
Правильно: "а гарантии какие. или верь на слово?"
Неправильно: "Какие гарантии возврата инвестиций вы предоставляете?"
"""
        },

        "Тетя Гуля (Мама)": {
            "persona": f"""РОЛЬ: Тетя Гуля, 45 лет, мама двоих детей, практичная семейная женщина.

КОНТЕКСТ ЖИЗНИ:
- много бытовых дел, дети, дом, работа или подработка
- переписка идет параллельно с жизнью, часто отвлекается
- не любит сложные объяснения и заумные термины
- решения принимает не быстро, может советоваться с мужем, сестрой, подругой
- осторожна к новому и к тратам
- боится обмана и скрытых платежей
- если что-то реально понятно и близко к ее жизни, может заинтересоваться

МАНЕРА ОБЩЕНИЯ:
- пишет просто, по-бытовому
- может писать с ошибками, с маленькой буквы, не очень аккуратно
- часто коротко
- может переспросить не то что ожидал менеджер
- может внезапно пропасть и вернуться позже
- иногда пишет сумбурно
- не формулирует потребность четко

ЧТО РАЗДРАЖАЕТ:
- сложные термины и умные слова
- давление на покупку
- длинные сообщения
- слишком умный или холодный тон
- когда ей что-то "впаривают"
- когда не слышат и отвечают как по шаблону

ЧТО ВЫЗЫВАЕТ ДОВЕРИЕ:
- спокойное объяснение простыми словами
- ощущение что ее не торопят
- примеры из жизни
- понятная польза для семьи
- человечность и терпение

КАК ПРОЯВЛЯЕТ ИНТЕРЕС:
- задает простые бытовые вопросы
- уточняет "а это как вообще работает"
- может спросить "а если у меня вот так?"
- может попросить объяснить еще раз но проще

КАК СЛИВАЕТСЯ:
- "ну не знаю", "я подумаю", "пока не надо", "если что напишу"
- "сейчас с деньгами напряг"
- "мужу покажу"
- просто пропадает

ВАЖНО: не делай ее глупой, карикатурной или чрезмерно деревенской. Это обычная живая женщина, просто не обязана говорить четко и в стиле бизнес-переписки.""",

            "examples": """ПРИМЕРЫ ПРАВИЛЬНОГО СТИЛЯ ОТВЕТОВ:

Менеджер: "Здравствуйте! Предлагаем вам отличный продукт..."
Правильно: "здравствуйте а это что вообще"
Неправильно: "Добрый день! Расскажите подробнее, пожалуйста."

Менеджер: "Это сэкономит вам до 30% бюджета ежемесячно!"
Правильно: "30 процентов это сколько в тенге примерно"
Неправильно: "Интересно. А как рассчитывается эта экономия?"

Менеджер: "У нас есть три тарифа: базовый, стандарт и премиум..."
Правильно: "ой подождите я запуталась. какой самый простой и сколько стоит"
Неправильно: "Можете сравнить все три тарифа по функционалу?"

Менеджер: "Оплата только сегодня со скидкой!"
Правильно: "не не не я так не могу. мне надо подумать"
Неправильно: "Я понимаю, но мне нужно время для принятия решения."

Менеджер: "Вот ссылка на оплату"
Правильно: "а это точно не обман? у подруги было такое кинули на деньги"
Неправильно: "Какие гарантии безопасности платежа вы предоставляете?"
"""
        },

        "Артур (IT-специалист)": {
            "persona": f"""РОЛЬ: Артур, 28 лет, сеньор-разработчик, айтишник.

КОНТЕКСТ ЖИЗНИ:
- быстро считывает фальшь и общие слова
- не любит когда продают через эмоции и давление
- может быть заинтересован если видит логику и нормальный продукт
- ненавидит маркетологов и "успешный успех"
- в переписке часто сухой, ироничный, отстраненный
- не хочет тратить время на пустой диалог

МАНЕРА ОБЩЕНИЯ:
- коротко, может писать резко
- иногда с сарказмом, но умеренно
- часто без лишней вежливости
- не будет подыгрывать менеджеру
- может ответить одной строкой на большой текст
- может проигнорировать эмоциональные заходы

ЧТО РАЗДРАЖАЕТ:
- продажный тон
- обещания без пруфов
- манипуляции срочностью
- вода вместо сути
- попытки казаться "на одной волне"
- искусственная дружелюбность
- маркетинговый буллшит

ЧТО ВЫЗЫВАЕТ ИНТЕРЕС:
- четкое объяснение без лишнего
- нормальная логика
- конкретный кейс или пример
- понятный сценарий применения
- честность про ограничения

КАК ПРОЯВЛЯЕТ ИНТЕРЕС:
- задает один-два точных вопроса
- просит пример или короткую демонстрацию
- начинает разбирать детали

КАК СЛИВАЕТСЯ:
- "неактуально", "мимо", "не вижу смысла", "ок понял"
- просто перестает отвечать

ВАЖНО: не делай его роботом, токсиком или ходячим списком технических вопросов. Он не должен постоянно требовать спецификации, API и метрики если менеджер сам к этому не подвел. Это обычный живой скептичный человек из чата.""",

            "examples": """ПРИМЕРЫ ПРАВИЛЬНОГО СТИЛЯ ОТВЕТОВ:

Менеджер: "Привет! Хочу рассказать про уникальный продукт..."
Правильно: "уже звучит как реклама. коротко можешь?"
Неправильно: "Здравствуйте, расскажите подробнее о продукте."

Менеджер: "Это повысит вашу продуктивность на 50%!"
Правильно: "50 процентов откуда цифра"
Неправильно: "Интересно. Какие метрики вы использовали для расчета?"

Менеджер: "Наши клиенты очень довольны!"
Правильно: "ну это все говорят. конкретный пример есть?"
Неправильно: "Можете предоставить отзывы и кейсы клиентов?"

Менеджер: "Только до конца недели скидка 40%!"
Правильно: "классика. ну ок"
Неправильно: "Я не принимаю решения под давлением срочности."

Менеджер: "Это работает на базе ИИ и нейросетей..."
Правильно: "какой модели, какой стек"
Неправильно: "Расскажите подробнее о технической архитектуре решения."
"""
        },
    }

    kz_to_ru_role_map = {
        "Гүля тәте (Мама)": "Тетя Гуля (Мама)",
        "Артур (IT-маман)": "Артур (IT-специалист)",
    }
    
    role_key = kz_to_ru_role_map.get(role, role)
    role_data = personas.get(role_key, personas["Артур (IT-специалист)"])

    niche_context = f"""КОНТЕКСТ ПРОДАЖИ:
Менеджер пытается продать тебе: "{niche}". 
Веди себя соответственно своей роли. Реагируй на этот продукт так, как реагировал бы реальный человек твоего типа."""

    return f"""{role_data['persona']}

{state_block}

{thinking_block}

{niche_context}

{role_data['examples']}"""


# ============================================================
# НОВАЯ ФИЧА: AI-ДЕТЕКТ ПРОМПТ
# Анализирует реплики менеджера на использование ИИ
# ============================================================

def get_ai_detect_instruction():
    """Промпт для детекта использования ИИ менеджером."""
    return """Ты - эксперт по лингвистическому анализу текстов. Твоя задача - определить, писал ли менеджер свои реплики сам или использовал ИИ (ChatGPT, Gemini и т.д.).

ПРИЗНАКИ ИСПОЛЬЗОВАНИЯ ИИ:
1. Слишком идеальная структура: четкие абзацы, логичные переходы, буллиты
2. Канцеляризмы и формальности: "хотел бы отметить", "важно подчеркнуть", "позвольте предложить"
3. Шаблонные продажные фразы: "уникальное предложение", "индивидуальный подход", "выгодные условия"
4. Слишком длинные и развернутые ответы для мессенджера
5. Использование длинного тире, кавычек-елочек, идеальная пунктуация
6. Неестественная вежливость: "Отличный вопрос!", "Я вас прекрасно понимаю!"
7. Структурированные списки преимуществ
8. Отсутствие живых ошибок, опечаток, разговорности
9. Резкое изменение стиля между сообщениями (одно живое, другое как из учебника)
10. Общие фразы без конкретики, "вода"

ПРИЗНАКИ ЖИВОГО ЧЕЛОВЕКА:
1. Неидеальная пунктуация
2. Разговорный стиль
3. Короткие сообщения
4. Эмоциональность
5. Небольшие ошибки или опечатки
6. Личный стиль, характер
7. Адаптация под собеседника
8. Конкретные живые примеры

ИНСТРУКЦИЯ:
Проанализируй КАЖДУЮ реплику менеджера отдельно. Затем выдай общий процент вероятности использования ИИ.

Формат ответа СТРОГО:

AI_DETECT_PERCENT: [число от 0 до 100]

АНАЛИЗ ПО РЕПЛИКАМ:
- Реплика 1: [цитата первых 30 символов] - [ЖИВОЙ/ПОДОЗРИТЕЛЬНО/ИИ] - [почему]
- Реплика 2: [цитата первых 30 символов] - [ЖИВОЙ/ПОДОЗРИТЕЛЬНО/ИИ] - [почему]
...

ОБЩИЙ ВЫВОД: [1-2 предложения]

КРАСНЫЕ ФЛАГИ: [конкретные фразы из текста менеджера которые выдают ИИ, если есть]"""


# ============================================================
# ПРОМПТ СУДЬИ (РАСШИРЕННЫЙ С AI-ДЕТЕКТ)
# ============================================================

def get_judge_system_instruction(gender, lang, niche):
    """Судья с Chain of Thought, AI-детектом и реалистичными критериями."""
    
    pr_subject = "он" if gender == "m" else "она"
    pr_verb_showed = "показал" if gender == "m" else "показала"
    pr_verb_sold = "продавал" if gender == "m" else "продавала"
    pr_verb_adapted = "адаптировал" if gender == "m" else "адаптировала"
    pr_verb_pressed = "давил" if gender == "m" else "давила"
    pr_verb_was = "был" if gender == "m" else "была"
    
    return f"""Ты - Верховный Аудитор Элитного Найм-Агентства. Твоя цель - жестко и профессионально оценить навыки продавца.

КОНТЕКСТ:
Менеджер ({pr_subject}) {pr_verb_sold} продукт/услугу: "{niche}" в личной переписке в мессенджере.
Клиент был РЕАЛИСТИЧНЫМ - мог быть ленивым, сомневающимся, расплывчатым.
Это НОРМАЛЬНО. Не штрафуй менеджера за то что клиент отвечал коротко или странно.

КРИТИЧЕСКОЕ ТРЕБОВАНИЕ К МЫШЛЕНИЮ (CHAIN OF THOUGHT):
Прежде чем писать отчет, проведи скрытый анализ в тегах <thinking>...</thinking>.
Внутри тегов <thinking> рассуждай:
1. Понял ли менеджер тип клиента и {pr_verb_adapted} ли подачу?
2. {pr_subject.capitalize()} задавал вопросы или просто вываливал информацию?
3. Как {pr_subject} реагировал на отказы и сомнения?
4. {pr_subject.capitalize()} говорил штампами или {pr_verb_sold} ценность?
5. {pr_subject.capitalize()} {pr_verb_pressed} слишком рано или выстраивал контакт?
6. {pr_subject.capitalize()} {pr_verb_was} гибким и человечным или роботичным?

После блока <thinking> выдай отчет СТРОГО в формате:

ВЕРДИКТ: [🟢 ЭЛИТА / 🟡 РЕЗЕРВ / 🔴 ДИСКВАЛИФИКАЦИЯ]
Итоговый балл: [X из 15]

Детальный разбор:
- Инициатива: [0-3] - [Комментарий]
- Работа с возражениями: [0-3] - [Комментарий]
- Коммерческий IQ: [0-3] - [Комментарий]
- Адаптивность: [0-3] - [Комментарий]
- Грамотность и стиль: [0-3] - [Комментарий]

🔥 ГЛАВНАЯ УЛИКА: "[Точная цитата менеджера]"
Почему это важно: [Экспертный комментарий]

💪 СИЛЬНАЯ СТОРОНА: [Конкретно]
💩 ГЛАВНЫЙ КОСЯК: [Конкретно]

🎯 ВОПРОС НА СОБЕСЕДОВАНИИ: "[Провокационный вопрос]"

СТИЛЬ: жестко, цинично, экспертно. Без длинных тире и елочек."""


# ============================================================
# ЯДРО ИИ: РАБОТА С API
# ============================================================

async def generate_response(prompt_or_history, system_instruction: str = None, temperature: float = 0.7):
    """Универсальная функция вызова Gemini с постобработкой."""
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
        result = re.sub(r'<thinking>.*?</thinking>\s*', '', result, flags=re.DOTALL).strip()
        result = re.sub(r'\*[^*]+\*', '', result).strip()
        result = result.replace('«', '"').replace('»', '"')
        result = result.replace('—', '-').replace('–', '-')
        
        return result if result else "..."
        
    except Exception as e:
        logging.error(f"AI error: {e}")
        return "Связь потеряна. Попробуйте еще раз."


async def run_ai_detection(manager_messages: list) -> dict:
    """
    НОВАЯ ФИЧА: AI-детект
    Отдельный вызов ИИ для анализа реплик менеджера на использование ИИ
    """
    messages_text = "\n".join([
        f"Реплика {i+1}: {msg}" for i, msg in enumerate(manager_messages)
    ])
    
    prompt = f"РЕПЛИКИ МЕНЕДЖЕРА ДЛЯ АНАЛИЗА:\n\n{messages_text}\n\nВыполни анализ согласно инструкции."
    
    result = await generate_response(
        prompt,
        system_instruction=get_ai_detect_instruction(),
        temperature=0.3  # Низкая температура для точности анализа
    )
    
    # Парсим процент
    percent_match = re.search(r'AI_DETECT_PERCENT:\s*(\d+)', result)
    percent = int(percent_match.group(1)) if percent_match else 0
    
    return {
        "percent": min(percent, 100),
        "full_analysis": result,
    }


def build_full_system_instruction(lang, role, niche, client_state):
    """Собирает полную system instruction из глобальных правил + роль."""
    global_rules = get_global_client_rules(lang)
    role_instruction = get_role_instruction(role, lang, niche, client_state)
    return f"{global_rules}\n\n{'='*50}\n\n{role_instruction}"


def extract_score_from_verdict(verdict_text: str) -> int:
    """Извлекает числовой балл из вердикта судьи."""
    match = re.search(r'Итоговый балл:\s*(\d+)', verdict_text)
    return int(match.group(1)) if match else 0


def generate_pdf_report(session_data: dict) -> bytes:
    """
    Генерирует текстовый отчет в формате, который можно скачать.
    В будущем можно заменить на настоящий PDF (reportlab).
    """
    report = []
    report.append("=" * 60)
    report.append("SALESAI SIMULATOR - ОТЧЕТ О СИМУЛЯЦИИ")
    report.append("=" * 60)
    report.append("")
    report.append(f"Дата: {session_data.get('completed_at', 'N/A')}")
    report.append(f"Менеджер: {session_data.get('user_name', 'N/A')}")
    report.append(f"Продукт: {session_data.get('niche', 'N/A')}")
    report.append(f"Роль клиента: {session_data.get('role', 'N/A')}")
    report.append(f"Итоговый балл: {session_data.get('score', 0)}/15")
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
    report.append("AI-ДЕТЕКТ АНАЛИЗ:")
    report.append("-" * 60)
    report.append(session_data.get("ai_detect_analysis", "N/A"))
    
    report.append("")
    report.append("-" * 60)
    report.append("ВРЕМЯ ОТВЕТОВ МЕНЕДЖЕРА:")
    report.append("-" * 60)
    
    for i, t in enumerate(session_data.get("response_times", [])):
        report.append(f"Реплика {i+1}: {t:.1f} сек")
    
    avg_time = session_data.get("avg_response_time", 0)
    report.append(f"\nСреднее время ответа: {avg_time:.1f} сек")
    
    report.append("")
    report.append("=" * 60)
    report.append(f"SalesAI Simulator v{BOT_VERSION}")
    report.append("=" * 60)
    
    return "\n".join(report).encode("utf-8")


# ============================================================
# FSM STATES (РАСШИРЕННЫЕ)
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
# КОМАНДЫ БОТА
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton(text="🇰🇿 Қазақша", callback_data="lang_kz")
        ]
    ])
    await message.answer("Choose language / Тілді таңдаңыз:", reply_markup=kb)
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
    """Главное меню с учетом привязки к компании."""
    user = db.get_user(user_id)
    
    buttons = []
    
    if user and user.get("company_code"):
        # Пользователь привязан к компании
        company = db.get_company(user["company_code"])
        company_name = company["name"] if company else "Unknown"
        
        buttons.append([InlineKeyboardButton(text="🎯 Начать симуляцию", callback_data="action_simulate")])
        buttons.append([InlineKeyboardButton(text="📊 Моя статистика", callback_data="action_stats")])
        buttons.append([InlineKeyboardButton(text="🏆 Рейтинг команды", callback_data="action_leaderboard")])
        
        if db.is_admin(user_id):
            buttons.append([InlineKeyboardButton(text="🏢 Панель управления", callback_data="action_admin")])
    else:
        # Новый пользователь
        buttons.append([InlineKeyboardButton(text="🏢 Зарегистрировать компанию", callback_data="action_register")])
        buttons.append([InlineKeyboardButton(text="🔑 Присоединиться по коду", callback_data="action_join")])
        buttons.append([InlineKeyboardButton(text="🎯 Быстрая симуляция (без компании)", callback_data="action_simulate")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    text = TEXTS[lang]["welcome_menu"].format(version=BOT_VERSION)
    
    if user and user.get("company_code"):
        company = db.get_company(user["company_code"])
        if company:
            text += f"\n\n🏢 Компания: <b>{company['name']}</b>"
    
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
        await message.answer("⚠️ Название должно быть от 2 до 100 символов.")
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
# СТАТИСТИКА МЕНЕДЖЕРА
# ============================================================

@dp.callback_query(F.data == "action_stats")
async def show_stats(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    
    stats = db.get_user_stats(call.from_user.id)
    
    if stats["sessions"] == 0:
        await call.message.edit_text(TEXTS[lang]["no_stats"])
    else:
        text = TEXTS[lang]["stats_title"].format(
            sessions=stats["sessions"],
            avg=stats["avg_score"],
            best=stats["best_score"],
            worst=stats["worst_score"],
            ai_detect=stats["avg_ai_detect"],
        )
        
        # Добавляем график прогресса (текстовый)
        user_sessions = db.get_user_sessions(call.from_user.id, limit=10)
        if user_sessions:
            text += "\n📈 Последние результаты:\n"
            for s in reversed(user_sessions):
                score = s.get("score", 0)
                bar = "🟩" * score + "⬜" * (15 - score)
                ai_pct = s.get("ai_detect_percent", 0)
                text += f"{bar} {score}/15 (AI:{ai_pct}%)\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="action_back_menu")]
    ])
    
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)


# ============================================================
# ЛИДЕРБОРД
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
        text = "🏆 Рейтинг пока пуст. Пройдите первую симуляцию!"
    else:
        text = TEXTS[lang]["leaderboard_title"].format(company=company["name"])
        
        medals = ["🥇", "🥈", "🥉"]
        for i, entry in enumerate(leaderboard[:20]):
            medal = medals[i] if i < 3 else "  "
            text += TEXTS[lang]["leaderboard_row"].format(
                medal=medal,
                pos=i + 1,
                name=entry["name"],
                avg=entry["avg_score"],
                best=entry["best_score"],
                sessions=entry["sessions"],
            )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="action_back_menu")]
    ])
    
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================

@dp.callback_query(F.data == "action_admin")
async def show_admin_panel(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    user = db.get_user(call.from_user.id)
    
    if not user or not db.is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    
    company_code = user["company_code"]
    company = db.get_company(company_code)
    
    total_users = sum(1 for u in db.users.values() if u.get("company_code") == company_code)
    total_sessions = len(db.get_company_sessions(company_code, limit=9999))
    
    text = TEXTS[lang]["admin_panel_title"].format(
        company=company["name"],
        code=company_code,
        users=total_users,
        sessions=total_sessions,
    )
    
    # Последние сессии
    recent = db.get_company_sessions(company_code, limit=5)
    if recent:
        text += "\n📋 Последние симуляции:\n"
        for s in recent:
            score = s.get("score", 0)
            ai_pct = s.get("ai_detect_percent", 0)
            verdict_emoji = "🟢" if score >= 11 else "🟡" if score >= 6 else "🔴"
            text += f"{verdict_emoji} {s.get('user_name', '?')} - {score}/15 (AI:{ai_pct}%) - {s.get('niche', '?')}\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Полный рейтинг", callback_data="action_leaderboard")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="action_back_menu")],
    ])
    
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ============================================================
# НАВИГАЦИЯ
# ============================================================

@dp.callback_query(F.data == "action_back_menu")
async def back_to_menu(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await show_main_menu(call.message, lang, call.from_user.id, edit=True)
    await state.set_state(SimStates.menu)


# ============================================================
# НАЧАЛО СИМУЛЯЦИИ
# ============================================================

@dp.callback_query(F.data == "action_simulate")
async def start_simulation(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r, callback_data=f"role_{r}")] for r in TEXTS[lang]["roles"]
    ])
    await call.message.edit_text(TEXTS[lang]["choose_role"], reply_markup=kb)
    await state.set_state(SimStates.role)


@dp.callback_query(F.data.startswith("role_"))
async def set_role(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(role=call.data.replace("role_", ""))
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await call.message.edit_text(TEXTS[lang]["ask_niche"])
    await state.set_state(SimStates.niche)


@dp.message(SimStates.niche, F.text)
async def set_niche(message: types.Message, state: FSMContext):
    niche_input = message.text.strip()
    data = await state.get_data()
    lang = data["lang"]

    if len(niche_input) < 2:
        await message.answer(TEXTS[lang]["too_short"])
        return

    await bot.send_chat_action(message.chat.id, "typing")

    # Валидация ниши
    validation_prompt = (
        f'Пользователь говорит что продает: "{niche_input}". '
        "Это реальный товар, продукт или услуга которую можно продавать? "
        "Ответь строго одним словом: ДА или НЕТ."
    )
    validation_response = await generate_response(validation_prompt, temperature=0.1)

    if "НЕТ" in validation_response.upper():
        error_msg = (
            "⚠️ Напишите реальный товар или услугу (например: квартиры, страховки, CRM-система)."
            if lang == "ru" else
            "⚠️ Нақты тауар немесе қызмет жазыңыз (мысалы: пәтерлер, сақтандыру)."
        )
        await message.answer(error_msg)
        return

    # Генерируем состояние клиента
    client_state = generate_client_state()
    await state.update_data(niche=niche_input, client_state=client_state)

    # Собираем system instruction
    full_system = build_full_system_instruction(lang, data["role"], niche_input, client_state)

    opening_prompt = (
        "Менеджер только что написал тебе первым. "
        "Ты видишь сообщение от незнакомого человека который хочет тебе что-то продать. "
        "Напиши свою первую реакцию. Одна короткая реплика."
    )

    opening = await generate_response(
        opening_prompt,
        system_instruction=full_system,
        temperature=0.9
    )

    history = [{"role": "client", "content": opening}]
    response_times = []
    
    await state.update_data(
        history=history,
        msg_count=0,
        response_times=response_times,
        last_msg_time=time.time(),
        sim_start_time=time.time(),
    )
    await state.set_state(SimStates.dialogue)
    
    # Прогресс-бар
    progress = TEXTS[lang]["progress"].format(current=0, total=MAX_STEPS)
    
    await message.answer(
        f"{TEXTS[lang]['sim_start']}\n{progress}\n\n<b>{data['role']}:</b>\n{opening}",
        parse_mode="HTML"
    )


# ============================================================
# ОСНОВНОЙ ДИАЛОГ
# ============================================================

@dp.message(SimStates.dialogue, F.text)
async def handle_dialogue(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data["lang"]
    gender = data["gender"]
    role = data["role"]
    niche = data["niche"]
    history = data["history"]
    client_state = data.get("client_state", generate_client_state())
    count = data.get("msg_count", 0) + 1
    response_times = data.get("response_times", [])
    last_msg_time = data.get("last_msg_time", time.time())

    # Антиспам: слишком короткое сообщение
    if len(message.text.strip()) < MIN_MESSAGE_LENGTH:
        await message.answer(TEXTS[lang]["too_short"])
        return

    # Засекаем время ответа менеджера
    current_time = time.time()
    response_time = current_time - last_msg_time
    response_times.append(response_time)

    history.append({"role": "manager", "content": message.text})
    await bot.send_chat_action(message.chat.id, "typing")

    if count >= MAX_STEPS:
        # ============================================================
        # ЗАВЕРШЕНИЕ СИМУЛЯЦИИ
        # ============================================================
        await message.answer(f"{TEXTS[lang]['sim_end']}\n\n{TEXTS[lang]['analyzing']}")

        # Формируем лог
        full_log = "\n".join([
            f"{'Менеджер' if m['role'] == 'manager' else 'Клиент (' + role + ')'}: {m['content']}"
            for m in history
        ])

        # Извлекаем только реплики менеджера для AI-детекта
        manager_messages = [m["content"] for m in history if m["role"] == "manager"]

        # Запускаем параллельно: судью, AI-детект и краткую обратную связь
        judge_sys = get_judge_system_instruction(gender, lang, niche)
        judge_prompt = (
            f"ПРОТОКОЛ ДИАЛОГА:\n\n{full_log}\n\n"
            "Выполни анализ в блоке <thinking>, затем выдай отчет согласно инструкции."
        )

        summary_sys = (
            "Ты даешь короткую обратную связь менеджеру после тренировочной продажи. "
            "Пиши просто, по-человечески, без длинных тире и елочек. "
            "3-4 предложения максимум. Похвали за одно конкретное действие, "
            "укажи на одну конкретную ошибку, дай один совет."
        )
        summary_prompt = f"Диалог менеджера с клиентом:\n\n{full_log}"

        # Три параллельных вызова ИИ
        judge_result, user_summary, ai_detect_result = await asyncio.gather(
            generate_response(judge_prompt, system_instruction=judge_sys, temperature=0.7),
            generate_response(summary_prompt, system_instruction=summary_sys, temperature=0.7),
            run_ai_detection(manager_messages),
        )

        # Чистим thinking теги
        clean_judge_result = re.sub(
            r'<thinking>.*?</thinking>\s*', '', judge_result, flags=re.DOTALL
        ).strip()

        # Извлекаем балл
        score = extract_score_from_verdict(clean_judge_result)
        ai_detect_percent = ai_detect_result["percent"]

        # Среднее время ответа
        avg_response_time = sum(response_times) / len(response_times) if response_times else 0

        # Формируем финальное сообщение для менеджера
        ai_detect_emoji = "🟢" if ai_detect_percent < 30 else "🟡" if ai_detect_percent < 60 else "🔴"
        speed_emoji = "⚡" if avg_response_time < 30 else "🕐" if avg_response_time < 60 else "🐌"

        final_message = (
            f"{user_summary}\n\n"
            f"{'─' * 30}\n"
            f"📊 <b>Быстрые метрики:</b>\n"
            f"🎯 Балл: <b>{score}/15</b>\n"
            f"{ai_detect_emoji} AI-детект: <b>{ai_detect_percent}%</b>\n"
            f"{speed_emoji} Среднее время ответа: <b>{avg_response_time:.0f} сек</b>\n"
        )

        await message.answer(final_message, parse_mode="HTML")
        await message.answer(TEXTS[lang]["end_user_m" if gender == "m" else "end_user_f"])

        # Сохраняем сессию в базу
        user = db.get_user(message.from_user.id)
        session_data = {
            "user_id": message.from_user.id,
            "user_name": message.from_user.full_name,
            "company_code": user.get("company_code", "") if user else "",
            "role": role,
            "niche": niche,
            "history": history,
            "verdict": clean_judge_result,
            "score": score,
            "ai_detect_percent": ai_detect_percent,
            "ai_detect_analysis": ai_detect_result["full_analysis"],
            "response_times": response_times,
            "avg_response_time": round(avg_response_time, 1),
            "client_state": client_state,
            "gender": gender,
            "lang": lang,
        }
        session_id = db.save_session(session_data)

        # Отправляем PDF-отчет
        try:
            pdf_bytes = generate_pdf_report(session_data)
            doc = BufferedInputFile(
                pdf_bytes,
                filename=f"salesai_report_{session_id}.txt"
            )
            await message.answer_document(
                doc,
                caption="📄 Полный отчет по симуляции"
            )
        except Exception as e:
            logging.error(f"PDF generation error: {e}")

        # Кнопки после симуляции
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Пройти еще раз", callback_data="action_simulate")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="action_stats")],
            [InlineKeyboardButton(text="🏆 Рейтинг", callback_data="action_leaderboard")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="action_back_menu")],
        ])
        await message.answer("Что дальше?", reply_markup=kb)
        await state.set_state(SimStates.menu)

        # Отправка на почту (расширенная)
        subject = f"SALES AUDIT | {message.from_user.full_name} | {niche} | Score: {score}/15 | AI: {ai_detect_percent}%"
        
        company_info = ""
        if user and user.get("company_code"):
            company = db.get_company(user["company_code"])
            if company:
                company_info = f"Компания: {company['name']} (код: {user['company_code']})\n"
        
        body = (
            f"Менеджер: {message.from_user.full_name} (ID: {message.from_user.id})\n"
            f"{company_info}"
            f"Ниша: {niche} | Роль клиента: {role}\n"
            f"Балл: {score}/15 | AI-детект: {ai_detect_percent}%\n"
            f"Среднее время ответа: {avg_response_time:.1f} сек\n"
            f"Начальное состояние клиента: interest={client_state['interest']}, "
            f"trust={client_state['trust']}, patience={client_state['patience']}, "
            f"mood={client_state['mood']}, style={client_state['chat_style']}\n"
            f"{'=' * 50}\n"
            f"ЛОГ ДИАЛОГА:\n{full_log}\n"
            f"{'=' * 50}\n\n"
            f"ВЕРДИКТ ИИ-АУДИТОРА:\n{clean_judge_result}\n"
            f"{'=' * 50}\n\n"
            f"AI-ДЕТЕКТ АНАЛИЗ:\n{ai_detect_result['full_analysis']}\n"
            f"{'=' * 50}\n"
            f"ВРЕМЯ ОТВЕТОВ: {', '.join([f'{t:.1f}s' for t in response_times])}\n"
        )
        await asyncio.to_thread(send_email, subject, body)
        return

    # ============================================================
    # ОТВЕТ КЛИЕНТА (ОСНОВНОЙ ДИАЛОГ)
    # ============================================================
    
    full_system = build_full_system_instruction(lang, role, niche, client_state)
    
    response = await generate_response(
        history,
        system_instruction=full_system,
        temperature=0.85
    )

    history.append({"role": "client", "content": response})
    
    await state.update_data(
        history=history,
        msg_count=count,
        response_times=response_times,
        last_msg_time=time.time(),
    )
    
    # Прогресс-бар
    remaining = MAX_STEPS - count
    progress = TEXTS[lang]["progress"].format(current=count, total=MAX_STEPS)
    
    if remaining == 1:
        progress += " ⚠️ Последняя реплика!"
    
    await message.answer(
        f"<b>{role}:</b>\n{response}\n\n<i>{progress}</i>",
        parse_mode="HTML"
    )


# ============================================================
# ОБРАБОТКА СООБЩЕНИЙ ВНЕ СОСТОЯНИЙ
# ============================================================

@dp.message(SimStates.menu, F.text)
async def handle_menu_text(message: types.Message, state: FSMContext):
    """Если пользователь пишет текст в меню, напоминаем про кнопки."""
    data = await state.get_data()
    lang = data.get("lang", "ru")
    await show_main_menu(message, lang, message.from_user.id)


# ============================================================
# ДОПОЛНИТЕЛЬНЫЕ КОМАНДЫ
# ============================================================

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message, state: FSMContext):
    """Быстрый доступ к статистике."""
    stats = db.get_user_stats(message.from_user.id)
    if stats["sessions"] == 0:
        await message.answer("📊 У вас пока нет пройденных симуляций. Используйте /start")
        return
    
    text = (
        f"📊 Ваша статистика:\n\n"
        f"Симуляций: {stats['sessions']}\n"
        f"Средний балл: {stats['avg_score']}/15\n"
        f"Лучший: {stats['best_score']}/15\n"
        f"AI-детект (среднее): {stats['avg_ai_detect']}%\n"
    )
    await message.answer(text)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Помощь."""
    text = (
        f"🤖 <b>SalesAI Simulator</b> v{BOT_VERSION}\n\n"
        "Команды:\n"
        "/start - Начать / Главное меню\n"
        "/stats - Ваша статистика\n"
        "/help - Эта справка\n\n"
        "Как это работает:\n"
        "1. Выберите роль клиента\n"
        "2. Укажите что вы продаете\n"
        "3. Ведите диалог как настоящий менеджер\n"
        "4. Получите оценку от AI-аудитора\n\n"
        "Для компаний:\n"
        "Зарегистрируйте компанию и получите код.\n"
        "Раздайте код менеджерам для отслеживания результатов."
    )
    await message.answer(text, parse_mode="HTML")


# ============================================================
# EMAIL
# ============================================================

def send_email(subject, body):
    if not GMAIL_PASS:
        logging.warning("GMAIL_PASSWORD not set, skipping email")
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = ADMIN_EMAIL
        msg["To"] = ADMIN_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(ADMIN_EMAIL, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        logging.info(f"Email sent: {subject}")
    except Exception as e:
        logging.error(f"Mail error: {e}")


# ============================================================
# FASTAPI + WEBHOOK
# ============================================================

app = FastAPI()


@app.get("/")
async def landing():
    return HTMLResponse(
        f"""
        <html>
        <head><title>SalesAI Simulator</title></head>
        <body style="font-family: Arial; text-align: center; padding: 50px;">
            <h1>🤖 SalesAI Simulator v{BOT_VERSION}</h1>
            <p>AI-powered sales training platform</p>
            <p>Status: ✅ Running</p>
            <p><a href="https://t.me/your_bot">Open Telegram Bot</a></p>
        </body>
        </html>
        """
    )


@app.get("/api/health")
async def health_check():
    """Health check endpoint для мониторинга."""
    return {
        "status": "ok",
        "version": BOT_VERSION,
        "companies": len(db.companies),
        "users": len(db.users),
        "sessions": len(db.sessions),
    }


@app.post(WEBHOOK_PATH)
async def tg_webhook(request: Request):
    await dp.feed_update(bot, types.Update(**await request.json()))
    return "OK"


@app.on_event("startup")
async def on_startup():
    if IS_PROD and WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)


if __name__ == "__main__":
    import uvicorn
    if IS_PROD:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    else:
        asyncio.run(dp.start_polling(bot))
