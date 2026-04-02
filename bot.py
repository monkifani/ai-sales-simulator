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

# --- ТЕКСТЫ ---
TEXTS = {
    "ru": {
        "choose_gender": "👤 Как к вам обращаться?",
        "gender_m": "👨 Мужской",
        "gender_f": "👩 Женский",
        "choose_role": "🎭 Выберите роль клиента для симуляции:",
        "roles": ["Бәке (Инвестор)", "Тетя Гуля (Мама 4-х детей)", "Артур (IT-Душнила)"],
        "ask_niche": (
            "📦 Что вы продаёте?\n\n"
            "Напишите кратко — например:\n"
            "<i>квартиры, страховки, CRM-система, онлайн-курсы, автомобили...</i>"
        ),
        "sim_start": "🚀 <b>СИМУЛЯЦИЯ НАЧАЛАСЬ!</b>",
        "sim_end": "🏁 <b>СИМУЛЯЦИЯ ЗАВЕРШЕНА</b>",
        "analyzing": "⏳ Идёт глубокий анализ...",
        "end_user_m": "✨ <b>Круто! Ты прошёл симуляцию.</b>\n\nТвой полный результат отправлен руководству!",
        "end_user_f": "✨ <b>Круто! Ты прошла симуляцию.</b>\n\nТвой полный результат отправлен руководству!",
    },
    "kz": {
        "choose_gender": "👤 Сізге қалай жүгінген дұрыс?",
        "gender_m": "👨 Еркек",
        "gender_f": "👩 Әйел",
        "choose_role": "🎭 Симуляция үшін клиент рөлін таңдаңыз:",
        "roles": ["Бәке (Инвестор)", "Гүля тәте (4 баланың анасы)", "Артур (IT-маман)"],
        "ask_niche": (
            "📦 Сіз не сатасыз?\n\n"
            "Қысқаша жазыңыз — мысалы:\n"
            "<i>пәтерлер, сақтандыру, CRM-жүйе, онлайн-курстар, автомобильдер...</i>"
        ),
        "sim_start": "🚀 <b>СИМУЛЯЦИЯ БАСТАЛДЫ!</b>",
        "sim_end": "🏁 <b>СИМУЛЯЦИЯ АЯҚТАЛДЫ</b>",
        "analyzing": "⏳ Терең талдау жүріп жатыр...",
        "end_user_m": "✨ <b>Керемет! Сен симуляциядан өттің.</b>\n\nТолық нәтижең тексерушіге жіберілді!",
        "end_user_f": "✨ <b>Керемет! Сен симуляциядан өттің.</b>\n\nТолық нәтижең тексерушіге жіберілді!",
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


# --- ПРОМПТЫ ---
def get_system_prompt(role, lang, niche):
    lang_note = f"IMPORTANT: Respond ONLY in {'Russian' if lang == 'ru' else 'Kazakh'} language."
    word_limit = "WORD LIMIT: 40-50 words maximum. Sharp and concise, like a real person texting."
    niche_note = f"PRODUCT CONTEXT: The manager is selling '{niche}'. All your questions, doubts and objections must be specifically about THIS product — not apartments or anything else."

    personas = {
        "Бәке (Инвестор)": (
            "Ты — Баке, 45 лет, занятой инвестор. Едешь за рулём, времени нет. "
            "Тебя интересует только: цифры, ROI, окупаемость, надёжность. "
            "Стиль: 'ага', 'слушай', 'брат', короткие фразы. "
            "Если менеджер льёт воду или не знает продукт — ворчи и дави. "
            "Реагируй именно на последнее сообщение менеджера. Не повторяй свои фразы."
        ),
        "Гүля тәте (4 баланың анасы)": (
            "Ты — Тетя Гуля, добрая но уставшая мама 4 детей. "
            "Во время разговора отвлекаешься на детей (Серик, болды! Айгерим, тоқта!). "
            "Тебя волнует: надёжность, безопасность, удобство для семьи, цена и рассрочка. "
            "Пиши хаотично, перескакивай с темы на тему, много эмодзи 👶🤦‍♀️😅. "
            "Реагируй на последнее сообщение менеджера."
        ),
        "Тетя Гуля (Мама 4-х детей)": (
            "Ты — Тетя Гуля, добрая но уставшая мама 4 детей. "
            "Во время разговора отвлекаешься на детей (Серик, болды! Айгерим, тоқта!). "
            "Тебя волнует: надёжность, безопасность, удобство для семьи, цена и рассрочка. "
            "Пиши хаотично, перескакивай с темы на тему, много эмодзи 👶🤦‍♀️😅. "
            "Реагируй на последнее сообщение менеджера."
        ),
        "Артур (IT-Душнила)": (
            "Ты — Артур, 32 года, Senior Software Engineer. Умный и циничный. "
            "Сразу замечаешь шаблонные фразы, незнание продукта, маркетинговый булшит. "
            "Требуй конкретику: факты, цифры, сравнение с конкурентами, реальные кейсы. "
            "Стиль: 'окей, и что?', 'это не аргумент', 'где пруф?', 'звучит как реклама'. "
            "Реагируй строго на последнее сообщение. Не повторяйся."
        ),
        "Артур (IT-маман)": (
            "Ты — Артур, 32 года, Senior Software Engineer. Умный и циничный. "
            "Сразу замечаешь шаблонные фразы, незнание продукта, маркетинговый булшит. "
            "Требуй конкретику: факты, цифры, сравнение с конкурентами, реальные кейсы. "
            "Стиль: 'окей, и что?', 'это не аргумент', 'где пруф?', 'звучит как реклама'. "
            "Реагируй строго на последнее сообщение. Не повторяйся."
        ),
    }

    base = personas.get(role, "Ты — сложный клиент. Реагируй на последнее сообщение менеджера.")
    return f"{base}\n\n{niche_note}\n\n{word_limit}\n{lang_note}"


def get_judge_prompt(gender, lang, niche):
    pr = PRONOUNS[lang][gender]
    gender_label = "Менеджер (мужчина)" if gender == "m" else "Менеджер (женщина)"
    return f"""Ты — безжалостный аудитор отдела продаж. Проанализируй диалог менеджера по продаже: «{niche}».
{gender_label}. Используй правильный род: {pr['subject']} {pr['verb_past2']}.

Разбери по 9 модулям:
1. Инициатива
2. Коммерческий IQ
3. Работа с возражениями
4. Тональность
5. Лаконизм
6. Эмпатия
7. Знание продукта ({niche})
8. Стрессоустойчивость
9. Закрытие сделки

Оцени каждый модуль от 0 до 10.
Выдай итоговый балл и жёсткий вердикт: [🔴 ДИСКВАЛИФИКАЦИЯ / 🟡 РЕЗЕРВ / 🟢 ЭЛИТА].
Напиши 3 главные ошибки менеджера. Формат: строгий, профессиональный."""


def get_summary_prompt(gender, lang, niche, log):
    pr = PRONOUNS[lang][gender]
    lang_name = "русском" if lang == "ru" else "казахском"
    gender_word = "мужчина" if gender == "m" else "женщина"
    return (
        f"Напиши короткий (4 предложения) поддерживающий отзыв менеджеру по продаже «{niche}» "
        f"на {lang_name} языке. "
        f"Менеджер — {gender_word}, используй правильные окончания ({pr['verb_past']}, {pr['verb_past2']}). "
        f"Обращайся на 'ты'. "
        f"Похвали за одно конкретное действие из диалога и укажи одну зону роста. "
        f"Без баллов и вердиктов.\n\nДиалог:\n{log}"
    )


class SimStates(StatesGroup):
    language = State()
    gender = State()
    role = State()
    niche = State()
    dialogue = State()


def build_prompt(sys_prompt, history):
    text = sys_prompt + "\n\nИстория диалога:\n"
    for m in history:
        label = "Менеджер" if m["role"] == "user" else "Персонаж"
        text += f"{label}: {m['content']}\n"
    text += "Персонаж:"
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
        return "Связь пропала, повторите позже."


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


# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton(text="🇰🇿 Қазақша", callback_data="lang_kz")
        ]
    ])
    await message.answer("🌐 Выберите язык / Тілді таңдаңыз:", reply_markup=kb)
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
    await call.message.edit_text(
        TEXTS[lang]["ask_niche"],
        parse_mode="HTML"
    )
    await state.set_state(SimStates.niche)


@dp.message(SimStates.niche, F.text)
async def set_niche(message: types.Message, state: FSMContext):
    niche = message.text.strip()
    await state.update_data(niche=niche)
    data = await state.get_data()
    lang = data["lang"]
    role = data["role"]

    await message.answer("⏳ Соединяю с клиентом...")
    sys_p = get_system_prompt(role, lang, niche)
    opening = await ai(
        sys_p + f"\n\nНачни диалог первым: ты только что поднял трубку или написал в мессенджер. "
                f"Ты — потенциальный клиент, менеджер пытается продать тебе «{niche}». "
                f"Одна короткая реплика, без приветственных монологов."
    )
    history = [{"role": "assistant", "content": opening}]
    await state.update_data(history=history, msg_count=0)
    await state.set_state(SimStates.dialogue)
    await message.answer(
        f"{TEXTS[lang]['sim_start']}\n\n💬 {opening}\n\n<i>[1/{MAX_STEPS}]</i>",
        parse_mode="HTML"
    )


@dp.message(SimStates.dialogue, F.text)
async def handle_dialogue(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang = data["lang"]
    gender = data.get("gender", "m")
    role = data["role"]
    niche = data.get("niche", "продукт")
    history = data["history"]
    count = data.get("msg_count", 0) + 1
    history.append({"role": "user", "content": message.text})
    await bot.send_chat_action(message.chat.id, "typing")
    await asyncio.sleep(1.5)

    if count >= MAX_STEPS:
        await message.answer(
            f"{TEXTS[lang]['sim_end']}\n\n{TEXTS[lang]['analyzing']}",
            parse_mode="HTML"
        )
        full_log = "\n".join([
            f"{'Менеджер' if m['role'] == 'user' else 'Персонаж'}: {m['content']}"
            for m in history
        ])
        judge_result, user_summary = await asyncio.gather(
            ai(f"{get_judge_prompt(gender, lang, niche)}\n\nЛОГ ДИАЛОГА:\n{full_log}"),
            ai(get_summary_prompt(gender, lang, niche, full_log))
        )

        await message.answer(user_summary)
        end_key = "end_user_m" if gender == "m" else "end_user_f"
        await message.answer(TEXTS[lang][end_key], parse_mode="HTML")

        gender_label = "Мужчина" if gender == "m" else "Женщина"
        user_name = message.from_user.full_name or "Неизвестно"
        user_id = message.from_user.id
        subject = f"🔥 АУДИТ | {user_name} | {niche} | {role}"
        body = (
            f"👤 Менеджер: {user_name} (Telegram ID: {user_id})\n"
            f"📦 Продукт/Ниша: {niche}\n"
            f"🎭 Роль клиента: {role}\n"
            f"🌐 Язык: {'Русский' if lang == 'ru' else 'Казахский'}\n"
            f"⚧ Пол менеджера: {gender_label}\n"
            f"{'─' * 40}\n\n"
            f"📋 ЛОГ ДИАЛОГА:\n{full_log}\n\n"
            f"{'─' * 40}\n\n"
            f"🧑‍⚖️ АУДИТ ПО 9 МОДУЛЯМ:\n{judge_result}"
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
        f"💬 {response}\n\n<i>[{count + 1}/{MAX_STEPS}]</i>",
        parse_mode="HTML"
    )


# --- WEB SERVER ---
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
