import os
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import types as genai_types
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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

INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

client = genai.Client(api_key=GEMINI_API_KEY)
# Используем flash, но с мощным контекстом он будет работать как Pro
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
            "квартиры, страховки, CRM-система, онлайн-курсы"
        ),
        "sim_start": "🔥 СИМУЛЯЦИЯ НАЧАЛАСЬ!\n\nМенеджер, ваш выход. Клиент на связи:",
        "sim_end": "🛑 СИМУЛЯЦИЯ ЗАВЕРШЕНА",
        "analyzing": "🧠 Верховный Аудитор анализирует диалог...",
        "end_user_m": "Отлично! Ты прошел симуляцию.\n\nТвой полный результат отправлен руководству!",
        "end_user_f": "Отлично! Ты прошла симуляцию.\n\nТвой полный результат отправлен руководству!",
    },
    "kz": {
        "choose_gender": "Сізге қалай жүгінген дұрыс?",
        "gender_m": "Еркек",
        "gender_f": "Әйел",
        "choose_role": "Симуляция үшін клиент рөлін таңдаңыз:",
        "roles": ["Бәке (Инвестор)", "Гүля тәте (Мама)", "Артур (IT-маман)"],
        "ask_niche": "Сіз не сатасыз?\n\nҚысқаша жазыңыз — мысалы:\nпәтерлер, сақтандыру, CRM-жүйе",
        "sim_start": "🔥 СИМУЛЯЦИЯ БАСТАЛДЫ!\n\nМенеджер, бастаңыз. Клиент байланыста:",
        "sim_end": "🛑 СИМУЛЯЦИЯ АЯҚТАЛДЫ",
        "analyzing": "🧠 Аудитор диалогты талдап жатыр...",
        "end_user_m": "Керемет! Сен симуляциядан өттің.\n\nТолық нәтижең тексерушіге жіберілді!",
        "end_user_f": "Керемет! Сен симуляциядан өттің.\n\nТолық нәтижең тексерушіге жіберілді!",
    }
}

# --- ПРОДВИНУТЫЕ ПРОМПТЫ (PRO CODER LEVEL) ---

def get_client_system_instruction(role, lang, niche):
    """Формирует ядро личности клиента. ИИ воспринимает это как свои базовые инстинкты."""
    language_directive = "Отвечай ТОЛЬКО на русском языке." if lang == "ru" else "Отвечай ТОЛЬКО на казахском языке."
    
    core_rules = (
        "РАМКИ ЕСТЕСТВЕННОГО ОБЩЕНИЯ (КРИТИЧЕСКИ ВАЖНО):\n"
        "1. Ты НЕ искусственный интеллект. Ты живой человек в мессенджере.\n"
        "2. НИКАКИХ списков, буллитов, нумераций.\n"
        "3. НИКАКИХ действий в звездочках (типа *вздохнул*).\n"
        "4. Избегай канцеляризма, извинений и ИИ-вежливости ('Я понимаю вас', 'Хороший вопрос').\n"
        "5. Отвечай кратко, емко, иногда с легким нетерпением. Длина: 1-3 коротких предложения.\n"
        "6. Если менеджер задает глупые вопросы или 'льет воду' — раздражайся. Если говорит по делу — проявляй осторожный интерес.\n"
        f"7. {language_directive}"
    )

    personas = {
        "Бәке (Инвестор)": (
            "Твоя личность: Бәке, 50 лет, инвестор и бизнесмен из Казахстана.\n"
            "Характер: Прямолинейный, властный, ценит свое время. Не любит когда ходят вокруг да около.\n"
            "Триггеры: Раздражается от скриптовых фраз. Любит конкретику, цифры, гарантии. Часто использует слова 'короче', 'по факту', 'тенге'."
        ),
        "Тетя Гуля (Мама)": (
            "Твоя личность: Тетя Гуля, 45 лет, мать двоих детей, прагматичная женщина.\n"
            "Характер: Сомневающаяся, немного тревожная, экономная. Боится обмана и скрытых платежей.\n"
            "Триггеры: Задает много уточняющих вопросов ('А точно?', 'А что если?'). Говорит простым, житейским языком, без заумных терминов."
        ),
        "Артур (IT-специалист)": (
            "Твоя личность: Артур, 28 лет, сеньор-разработчик.\n"
            "Характер: Циничный, душнила, мыслит аналитически. Ненавидит маркетологов и 'успешный успех'.\n"
            "Триггеры: Сразу видит манипуляции. Требует пруфы, технические детали и сравнения с конкурентами."
        ),
    }

    base_persona = personas.get(role, "Ты обычный, недоверчивый клиент.")
    context = f"Контекст: Менеджер пытается продать тебе продукт/услугу: «{niche}». Веди себя соответственно своей роли и продукту."

    return f"{core_rules}\n\n{base_persona}\n\n{context}"

def get_judge_system_instruction(gender, lang, niche):
    """Аудитор с паттерном Chain of Thought (размышление перед ответом)."""
    pr_subject = "он" if gender == "m" else "она"
    pr_verb = "показал" if gender == "m" else "показала"
    
    return f"""Ты — Верховный Аудитор Элитного Найм-Агентства. Твоя цель — отсеивать слабых продавцов и находить 'Хищников'.
Твоя задача — жестко, цинично и профессионально проанализировать диалог менеджера (продает: «{niche}»).
Менеджер — человек ({pr_subject} {pr_verb}).

КРИТИЧЕСКОЕ ТРЕБОВАНИЕ К МЫШЛЕНИЮ (CHAIN OF THOUGHT):
Прежде чем писать отчет, ты ОБЯЗАН провести скрытый анализ в тегах <thinking>...</thinking>.
Внутри тегов <thinking> рассуждай:
1. Кто вел диалог? Задавал ли менеджер вопросы в конце своих реплик?
2. Как менеджер реагировал на отказы? Слился или отработал?
3. Говорил ли менеджер штампами или продавал ценность?

После блока <thinking> выдай отчет СТРОГО в следующем формате (без markdown-кода, просто текст):

ВЕРДИКТ: [🟢 ЭЛИТА / 🟡 РЕЗЕРВ / 🔴 ДИСКВАЛИФИКАЦИЯ]
Итоговый балл: [X из 15]

Детальный разбор:
• Инициатива: [0-3] — [Краткий комментарий]
• Стресс: [0-3] — [Краткий комментарий]
• Коммерческий IQ: [0-3] — [Краткий комментарий]
• Локальность (КЗ контекст): [0-3] — [Краткий комментарий]
• Грамотность: [0-3] — [Краткий комментарий]

🔥 ГЛАВНАЯ УЛИКА: «[Точная цитата менеджера из диалога, где он ошибся или блеснул]»
Почему это важно: [Твой жесткий экспертный комментарий]

💪 СИЛЬНАЯ СТОРОНА: [В чем менеджер хорош]
💩 ГЛАВНЫЙ КОСЯК: [Где менеджер потерял деньги/клиента]

🎯 ВОПРОС НА СОБЕСЕДОВАНИИ: «[Провокационный вопрос менеджеру на основе его ошибок]»"""

# --- ЯДРО ИИ: РАБОТА С API ---

async def generate_response(prompt_or_history, system_instruction: str = None, temperature: float = 0.7):
    """
    Универсальная функция вызова Gemini. 
    Если передана строка — обрабатываем как обычный запрос.
    Если передан список словарей — обрабатываем как историю диалога.
    """
    try:
        config = genai_types.GenerateContentConfig(
            temperature=temperature,
        )
        if system_instruction:
            config.system_instruction = system_instruction

        # Если передаем историю диалога в нативном формате Gemini
        if isinstance(prompt_or_history, list):
            contents = []
            for msg in prompt_or_history:
                # В Gemini роли: 'user' и 'model'
                role = "user" if msg["role"] == "manager" else "model"
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part.from_text(text=msg["content"])]))
        else:
            contents = prompt_or_history

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_ID,
            contents=contents,
            config=config
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"AI error: {e}")
        return "Связь потеряна. Попробуйте еще раз."

# --- ЛОГИКА БОТА ---

class SimStates(StatesGroup):
    language = State()
    gender = State()
    role = State()
    niche = State()
    dialogue = State()

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"), InlineKeyboardButton(text="🇰🇿 Қазақша", callback_data="lang_kz")]
    ])
    await message.answer("Choose language / Тілді таңдаңыз:", reply_markup=kb)
    await state.set_state(SimStates.language)

@dp.callback_query(F.data.startswith("lang_"))
async def set_lang(call: types.CallbackQuery, state: FSMContext):
    lang = call.data.split("_")[1]
    await state.update_data(lang=lang)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TEXTS[lang]["gender_m"], callback_data="gender_m"), InlineKeyboardButton(text=TEXTS[lang]["gender_f"], callback_data="gender_f")]
    ])
    await call.message.edit_text(TEXTS[lang]["choose_gender"], reply_markup=kb)
    await state.set_state(SimStates.gender)

@dp.callback_query(F.data.startswith("gender_"))
async def set_gender(call: types.CallbackQuery, state: FSMContext):
    gender = call.data.split("_")[1]
    await state.update_data(gender=gender)
    lang = (await state.get_data())["lang"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r, callback_data=f"role_{r}")] for r in TEXTS[lang]["roles"]
    ])
    await call.message.edit_text(TEXTS[lang]["choose_role"], reply_markup=kb)
    await state.set_state(SimStates.role)

@dp.callback_query(F.data.startswith("role_"))
async def set_role(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(role=call.data.replace("role_", ""))
    lang = (await state.get_data())["lang"]
    await call.message.edit_text(TEXTS[lang]["ask_niche"])
    await state.set_state(SimStates.niche)

@dp.message(SimStates.niche, F.text)
async def set_niche(message: types.Message, state: FSMContext):
    niche_input = message.text.strip()
    data = await state.get_data()
    lang = data["lang"]
    
    await bot.send_chat_action(message.chat.id, "typing")
    
    # Валидация ниши
    validation_prompt = f"Пользователь продает: «{niche_input}». Это реальный товар/услуга? Ответь строго: ДА или НЕТ."
    validation_response = await generate_response(validation_prompt, temperature=0.1)
    
    if "НЕТ" in validation_response.upper():
        error_msg = "⚠️ Напишите адекватное название (например: квартиры, услуги юриста)." if lang == "ru" else "⚠️ Дұрыстап жазыңыз (мысалы: пәтерлер)."
        await message.answer(error_msg)
        return

    await state.update_data(niche=niche_input)
    
    # Генерация первой реплики клиента
    sys_inst = get_client_system_instruction(data["role"], lang, niche_input)
    opening_prompt = "Начни диалог. Ты только что поднял трубку или открыл сообщение. Напиши 1 короткую реплику."
    
    opening = await generate_response(opening_prompt, system_instruction=sys_inst, temperature=0.8)
    
    # История хранит 'manager' (пользователь) и 'client' (ИИ)
    history = [{"role": "client", "content": opening}]
    await state.update_data(history=history, msg_count=0)
    await state.set_state(SimStates.dialogue)
    await message.answer(f"{TEXTS[lang]['sim_start']}\n\n<b>{data['role']}:</b>\n{opening}", parse_mode="HTML")

@dp.message(SimStates.dialogue, F.text)
async def handle_dialogue(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang, gender, role, niche, history, count = data["lang"], data["gender"], data["role"], data["niche"], data["history"], data.get("msg_count", 0) + 1
    
    history.append({"role": "manager", "content": message.text})
    await bot.send_chat_action(message.chat.id, "typing")

    if count >= MAX_STEPS:
        await message.answer(f"{TEXTS[lang]['sim_end']}\n\n{TEXTS[lang]['analyzing']}")
        
        # Формируем лог для Аудитора
        full_log = "\n".join([f"{'Менеджер' if m['role'] == 'manager' else 'Клиент'}: {m['content']}" for m in history])
        
        # Аудитор с Chain of Thought
        judge_sys = get_judge_system_instruction(gender, lang, niche)
        judge_prompt = f"ПРОТОКОЛ ДИАЛОГА:\n{full_log}\n\nВыполни анализ и выдай отчет согласно инструкции."
        
        # Резюме для юзера
        summary_prompt = f"Напиши 3 ободряющих предложения обратной связи для менеджера. Без оценок. Хвали за одно действие, укажи на одну ошибку. Диалог:\n{full_log}"
        
        judge_result, user_summary = await asyncio.gather(
            generate_response(judge_prompt, system_instruction=judge_sys, temperature=0.7),
            generate_response(summary_prompt, temperature=0.7)
        )

        # Вырезаем теги <thinking> из отчета для почты, чтобы руководство видело только чистый отчет (по желанию, можно оставить)
        import re
        clean_judge_result = re.sub(r'<thinking>.*?</thinking>\s*', '', judge_result, flags=re.DOTALL)

        await message.answer(user_summary)
        await message.answer(TEXTS[lang]["end_user_m" if gender == "m" else "end_user_f"])

        # Отправка на почту
        subject = f"SALES AUDIT | {message.from_user.full_name} | {niche}"
        body = (
            f"Менеджер: {message.from_user.full_name} (ID: {message.from_user.id})\n"
            f"Ниша: {niche} | Роль клиента: {role}\n"
            f"{'='*40}\nЛОГ ДИАЛОГА:\n{full_log}\n{'='*40}\n\n"
            f"ВЕРДИКТ ИИ-АУДИТОРА:\n{clean_judge_result}"
        )
        await asyncio.to_thread(send_email, subject, body)
        await state.clear()
        return

    # Ответ клиента
    sys_inst = get_client_system_instruction(role, lang, niche)
    response = await generate_response(history, system_instruction=sys_inst, temperature=0.85) # Выше температура = живее диалог
    
    history.append({"role": "client", "content": response})
    await state.update_data(history=history, msg_count=count)
    await message.answer(f"<b>{role}:</b>\n{response}", parse_mode="HTML")

def send_email(subject, body):
    if not GMAIL_PASS: return
    try:
        msg = MIMEMultipart()
        msg["From"] = msg["To"] = ADMIN_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(ADMIN_EMAIL, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
    except Exception as e: logging.error(f"Mail error: {e}")

app = FastAPI()
@app.get("/")
async def landing(): return HTMLResponse("Bot is running!")
@app.post(WEBHOOK_PATH)
async def tg_webhook(request: Request):
    await dp.feed_update(bot, types.Update(**await request.json()))
    return "OK"

@app.on_event("startup")
async def on_startup():
    if IS_PROD and WEBHOOK_URL: await bot.set_webhook(WEBHOOK_URL)

if __name__ == "__main__":
    import uvicorn
    if IS_PROD: uvicorn.run(app, host="0.0.0.0", port=PORT)
    else: asyncio.run(dp.start_polling(bot))
        
