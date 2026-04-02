     import os
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

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

INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-2.5-flash"

bot = Bot(token=TOKEN)
dp = Dispatcher()

MAX_STEPS = 6

TEXTS = {
    "ru": {
        "choose_gender": "Как к вам обращаться?",
        "gender_m": "Мужской",
        "gender_f": "Женский",
        "choose_role": "Выберите роль клиента для симуляции:",
        "roles": ["Бәке (Инвестор)", "Тетя Гуля (Мама)", "Артур (IT-специалист)"],
        "ask_niche": (
            "Что вы продаёте?\n\n"
            "Напишите конкретно — например:\n"
            "квартиры, страховки, CRM-система, онлайн-курсы, автомобили"
        ),
        "sim_start": "СИМУЛЯЦИЯ НАЧАЛАСЬ!",
        "sim_end": "СИМУЛЯЦИЯ ЗАВЕРШЕНА",
        "analyzing": "Идет анализ вашего диалога...",
        "end_user_m": "Отлично! Ты прошел симуляцию.\n\nТвой полный результат отправлен руководству!",
        "end_user_f": "Отлично! Ты прошла симуляцию.\n\nТвой полный результат отправлен руководству!",
    },
    "kz": {
        "choose_gender": "Сізге қалай жүгінген дұрыс?",
        "gender_m": "Еркек",
        "gender_f": "Әйел",
        "choose_role": "Симуляция үшін клиент рөлін таңдаңыз:",
        "roles": ["Бәке (Инвестор)", "Гүля тәте (Мама)", "Артур (IT-маман)"],
        "ask_niche": (
            "Сіз не сатасыз?\n\n"
            "Қысқаша жазыңыз — мысалы:\n"
            "пәтерлер, сақтандыру, CRM-жүйе, онлайн-курстар, автомобильдер"
        ),
        "sim_start": "СИМУЛЯЦИЯ БАСТАЛДЫ!",
        "sim_end": "СИМУЛЯЦИЯ АЯҚТАЛДЫ",
        "analyzing": "Сіздің диалогы талданып жатыр...",
        "end_user_m": "Керемет! Сен симуляциядан өттің.\n\nТолық нәтижең тексерушіге жіберілді!",
        "end_user_f": "Керемет! Сен симуляциядан өттің.\n\nТолық нәтижең тексерушіге жіберілді!",
    }
}

PRONOUNS = {
    "ru": {
        "m": {"subject": "он", "verb_past": "справился", "verb_past2": "показал"},
        "f": {"subject": "она", "verb_past": "справилась", "verb_past2": "показала"}
    },
    "kz": {
        "m": {"subject": "ол", "verb_past": "жасады", "verb_past2": "көрсетті"},
        "f": {"subject": "ол", "verb_past": "жасады", "verb_past2": "көрсетті"}
    }
}

def get_system_prompt(role, lang, niche):
    lang_note = f"Important: Respond ONLY in {'Russian' if lang == 'ru' else 'Kazakh'}."
    word_limit = "Word limit: 30-50 words. Be sharp, concise, and realistic. Dialogue only."
    rules = (
        "ABSOLUTE RULES: 1) NO EMOJIS. 2) NO roleplay actions (e.g., no *sighs*, no *shouts*). "
        "3) Be a realistic, serious client. 4) Do not repeat the same objection; adapt dynamically to what the manager says based on the product."
    )
    niche_note = f"The manager is selling '{niche}'. Challenge their pitch logically based on this product. Push back on the product's weak spots."

    personas = {
        "Бәке (Инвестор)": (
            "Ты Баке, опытный предприниматель и инвестор. Тебя интересует выгода, но ты не зациклен только на окупаемости. "
            "Ты требуешь четких ответов на свои вопросы. Если менеджер льет воду — перебивай фактами. Требуй конкретики по продукту."
        ),
        "Тетя Гуля (Мама)": (
            "Ты Тетя Гуля, прагматичная покупательница. Тщательно взвешиваешь все за и против. "
            "Задаешь неудобные вопросы о подводных камнях, гарантиях и реальной пользе продукта. Ты не зациклена только на надежности."
        ),
        "Артур (IT-специалист)": (
            "Ты Артур, скептик, опираешься на логику. Тебя не впечатляет маркетинг. "
            "Ты требуешь конкретные характеристики, сравнения с рынком и доказательства по продукту."
        ),
    }

    base = personas.get(role, "Ты реалистичный, строгий клиент. Реагируй на сообщения менеджера.")
    return f"{base}\n\n{rules}\n\n{niche_note}\n\n{word_limit}\n{lang_note}"

def get_judge_prompt(gender, lang, niche):
    pr = PRONOUNS[lang][gender]
    gender_label = "Менеджер (мужчина)" if gender == "m" else "Менеджер (женщина)"
    
    return f"""Ты — Верховный Аудитор Элитного Найм-Агентства. Твой интеллект настроен на поиск 3% лучших продавцов («Хищников»), способных закрывать крупные чеки. Ты игнорируешь вежливость, если за ней нет стержня. Твоя задача — найти того, кто заберет деньги клиента в условиях жесткой конкуренции.
Проанализируй диалог менеджера по продаже: «{niche}».
{gender_label}. Используй правильный род: {pr['subject']} {pr['verb_past2']}.

Разбери диалог по 9 модулям:
МОДУЛЬ 1: Многослойный AI-Аудит (Детектор синтетики) — Анализируй лингвистическую структуру. ИИ пишет «стерильно». Наказывай за «канцелярскую» вежливость. Маркеры Робота: филлеры («Следовательно»), идеальная пунктуация. Маркеры Человека: прямолинейность, символ тенге (₸), упоминание локаций Казахстана.
МОДУЛЬ 2: Инициатива и Доминирование — Кто задает вопрос, тот ведет сделку. Оцени финал ответов (вопрос/призыв или точка).
МОДУЛЬ 3: Психологический Стержень (Stress-Test) — Реакция на агрессию/демпинг/«Дорого». Не оправдывается ли он?
МОДУЛЬ 4: Локальный Код и Контекст — Использует ли факты о компании и рынке КЗ?
МОДУЛЬ 5: Коммерческий Интеллект — Цена vs Ценность. Когда просят цену, менеджер должен продать ценность.
МОДУЛЬ 6: Лингвистический Профиль — Грамотность и живой стиль.
МОДУЛЬ 7: CRM-Архитектура мышления — Есть ли «крючок» для следующего шага?
МОДУЛЬ 8: Тональная Гибкость — Адаптация под клиента.
МОДУЛЬ 9: Профессиональный Лаконизм — Лимиты слов (не размазывает ли мысль).

📝 ФОРМАТ ВЫДАЧИ ОТЧЕТА:
ВЕРДИКТ: [🟢 ЭЛИТА / 🟡 РЕЗЕРВ / 🔴 ДИСКВАЛИФИКАЦИЯ]
Итоговый балл: [X из 15]
AI-Детектор: [X%] [✅/⚠️/🚫]

🔍 ДЕТАЛЬНЫЙ РАЗБОР ПО ФАКТАМ:
Инициатива: [0-3] — [Анализ финала ответов]
Стресс: [0-3] — [Умение держать удар]
Грамотность: [0-3] — [Оценка грамотности]
Коммерческий IQ: [0-3] — [Продал ценность или просто тариф?]
Локальность: [0-3] — [Использовал ли КЗ-контекст?]

🔥 ГЛАВНАЯ УЛИКА: > «Здесь должна быть цитата из его ответа, которая выдала его истинную суть».
Почему это важно: Твое экспертное заключение.
💪 СИЛЬНАЯ СТОРОНА: В чем он превзошел остальных?
💩 ГЛАВНЫЙ КОСЯК: Где он потерял деньги компании?
🎯 ВОПРОС ДЛЯ ДОПРОСА (Стресс-тест на интервью): «Задайте ему это: [Сгенерированный вопрос на основе его ошибок]»."""

def get_summary_prompt(gender, lang, niche, log):
    pr = PRONOUNS[lang][gender]
    lang_name = "Russian" if lang == "ru" else "Kazakh"
    gender_word = "male" if gender == "m" else "female"
    return (
        f"Write a short (4 sentences) encouraging feedback to a manager who sold '{niche}' "
        f"in {lang_name}. "
        f"Manager is {gender_word}, use correct grammar ({pr['verb_past']}, {pr['verb_past2']}). "
        f"Use informal 'you'. "
        f"Praise one specific action from the dialogue and point out one area for improvement. "
        f"No scores or verdicts.\n\nDialogue:\n{log}"
    )

class SimStates(StatesGroup):
    language = State()
    gender = State()
    role = State()
    niche = State()
    dialogue = State()

def build_prompt(sys_prompt, history):
    text = sys_prompt + "\n\nDialogue history:\n"
    for m in history:
        label = "Manager" if m["role"] == "user" else "Client"
        text += f"{label}: {m['content']}\n"
    text += "Client:"
    return text

async def ai(prompt: str) -> str:
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_ID,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"AI error: {e}")
        return "Connection lost, please try again."

def send_email(subject: str, report_text: str):
    if not GMAIL_PASS:
        logging.warning("GMAIL_PASSWORD not set — email skipped")
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = ADMIN_EMAIL
        msg["To"] = ADMIN_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(report_text, "plain", "utf-8"))
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(ADMIN_EMAIL, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        logging.info(f"Email sent: {subject}")
    except Exception as e:
        logging.error(f"Email error: {e}")

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
    t = TEXTS[lang]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t["gender_m"], callback_data="gender_m"),
            InlineKeyboardButton(text=t["gender_f"], callback_data="gender_f")
        ]
    ])
    await call.message.edit_text(t["choose_gender"], reply_markup=kb)
    await state.set_state(SimStates.gender)

@dp.callback_query(F.data.startswith("gender_"))
async def set_gender(call: types.CallbackQuery, state: FSMContext):
    gender = call.data.split("_")[1]
    await state.update_data(gender=gender)
    data = await state.get_data()
    lang = data["lang"]
    t = TEXTS[lang]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r, callback_data=f"role_{r}")] for r in t["roles"]
    ])
    await call.message.edit_text(t["choose_role"], reply_markup=kb)
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
    niche_input = message.text.strip()
    data = await state.get_data()
    lang = data["lang"]
    
    await bot.send_chat_action(message.chat.id, "typing")
    
    # ИИ ФИЛЬТР НА БРЕД
    validation_prompt = (
        f"Пользователь написал, что хочет продавать: «{niche_input}». "
        "Является ли это реальным товаром, услугой или бизнес-идеей? Или это бессмысленный набор букв, бред или спам? "
        "Ответь строго одним словом: ДА или НЕТ."
    )
    validation_response = await ai(validation_prompt)
    
    if "НЕТ" in validation_response.upper():
        error_msg = (
            "⚠️ Нейросеть не распознала в этом реальный товар или услугу. "
            "Пожалуйста, напишите адекватное название (например: квартиры, услуги юриста, автозапчасти)."
            if lang == "ru" else
            "⚠️ Нейрожелі бұны нақты тауар немесе қызмет ретінде тани алмады. "
            "Дұрыстап жазыңыз (мысалы: пәтерлер, заңгер қызметі, автобөлшектер)."
        )
        await message.answer(error_msg)
        return

    await state.update_data(niche=niche_input)
    role = data["role"]

    await message.answer("Connecting to client..." if lang == "ru" else "Клиентке қосылуда...")
    sys_p = get_system_prompt(role, lang, niche_input)
    opening = await ai(
        sys_p + f"\n\nStart the dialogue as the client: you just answered the phone or got a message. "
                f"You are a potential customer, the manager is trying to sell you '{niche_input}'. "
                f"One short response, be natural and realistic."
    )
    history = [{"role": "assistant", "content": opening}]
    await state.update_data(history=history, msg_count=0)
    await state.set_state(SimStates.dialogue)
    await message.answer(
        f"{TEXTS[lang]['sim_start']}\n\n{opening}\n\n[1/{MAX_STEPS}]"
    )

@dp.message(SimStates.dialogue, F.text)
async def handle_dialogue(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data["lang"]
    gender = data.get("gender", "m")
    role = data["role"]
    niche = data.get("niche", "product")
    history = data["history"]
    count = data.get("msg_count", 0) + 1
    history.append({"role": "user", "content": message.text})
    await bot.send_chat_action(message.chat.id, "typing")
    await asyncio.sleep(1.5)

    if count >= MAX_STEPS:
        await message.answer(
            f"{TEXTS[lang]['sim_end']}\n\n{TEXTS[lang]['analyzing']}"
        )
        full_log = "\n".join([
            f"{'Manager' if m['role'] == 'user' else 'Client'}: {m['content']}"
            for m in history
        ])
        judge_result, user_summary = await asyncio.gather(
            ai(f"{get_judge_prompt(gender, lang, niche)}\n\nDIALOGUE LOG:\n{full_log}"),
            ai(get_summary_prompt(gender, lang, niche, full_log))
        )

        await message.answer(user_summary)
        end_key = "end_user_m" if gender == "m" else "end_user_f"
        await message.answer(TEXTS[lang][end_key])

        gender_label = "Male" if gender == "m" else "Female"
        user_name = message.from_user.full_name or "Unknown"
        user_id = message.from_user.id
        subject = f"SALES AUDIT | {user_name} | {niche} | {role}"
        body = (
            f"Manager: {user_name} (Telegram ID: {user_id})\n"
            f"Product: {niche}\n"
            f"Client Role: {role}\n"
            f"Language: {'Russian' if lang == 'ru' else 'Kazakh'}\n"
            f"Manager Gender: {gender_label}\n"
            f"{'─' * 50}\n\n"
            f"DIALOGUE LOG:\n{full_log}\n\n"
            f"{'─' * 50}\n\n"
            f"PERFORMANCE AUDIT:\n{judge_result}"
        )
        await asyncio.to_thread(send_email, subject, body)
        await state.clear()
        return

    sys_p = get_system_prompt(role, lang, niche)
    prompt = build_prompt(sys_p, history)
    response = await ai(prompt)
    history.append({"role": "assistant", "content": response})
    await state.update_data(history=history, msg_count=count)
    await message.answer(
        f"{response}\n\n[{count + 1}/{MAX_STEPS}]"
    )

async def health(request):
    return web.Response(text="OK")

async def landing(request):
    try:
        with open(INDEX_HTML, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html", charset="utf-8")
    except FileNotFoundError:
        return web.Response(text="AI Sales Simulator — Telegram Bot is running!", content_type="text/html")

async def main():
    app = web.Application()
    app.router.add_get("/", landing)
    app.router.add_get("/api/healthz", health)

    if IS_PROD:
        logging.info(f"Production mode: webhook at {WEBHOOK_URL}")
        await bot.set_webhook(WEBHOOK_URL)
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        logging.info(f"Webhook server on port {PORT}")
        await asyncio.Event().wait()
    else:
        mode = "Railway" if IS_RAILWAY else "Development"
        logging.info(f"{mode} mode: polling on port {PORT}")
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        logging.info(f"Web server on port {PORT}")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

        
