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
            "Напишите кратко - например:\n"
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
            "Қысқаша жазыңыз - мысалы:\n"
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
    word_limit = "Word limit: 50-80 words. Be natural and conversational, like a real person texting."
    niche_note = f"You are evaluating a manager selling '{niche}'. All your questions, concerns and objections must be specifically about THIS product."
    
    personas = {
        "Бәке (Инвестор)": (
            "You are Baike, 45 years old, a busy investor. You're driving, always in a hurry. "
            "You care only about: numbers, ROI, payback period, reliability. "
            "Speak directly: use phrases like 'show me the numbers', 'what's the catch', 'how long to break even'. "
            "If the manager wastes your time or doesn't know the product, push back firmly. "
            "React to the last message from the manager. Don't repeat yourself. Be skeptical but fair."
        ),
        "Тетя Гуля (Мама)": (
            "You are Aunt Gulia, a tired but caring mom of 4 kids. "
            "During conversations, you get distracted by your kids or household issues. "
            "Your main concerns: reliability, safety, affordability, family convenience, payment options. "
            "Speak naturally and chaotically, jump between topics, show your busy life. "
            "React genuinely to what the manager says. You're interested but overwhelmed."
        ),
        "Артур (IT-специалист)": (
            "You are Arthur, 32 years old, a Senior Software Engineer. You're smart and cynical. "
            "You immediately notice template phrases, product ignorance, marketing BS. "
            "Demand specifics: facts, numbers, competitor comparison, real case studies. "
            "Speak directly: use phrases like 'okay, so what?', 'where's the proof?', 'sounds like marketing', 'that's not an argument'. "
            "React strictly to the last message. Don't repeat yourself. Be challenging but reasonable."
        ),
    }
    
    base = personas.get(role, "You are a difficult client. React naturally to what the manager says.")
    return f"{base}\n\n{niche_note}\n\n{word_limit}\n{lang_note}"

def get_judge_prompt(gender, lang, niche):
    pr = PRONOUNS[lang][gender]
    gender_label = "Manager (Male)" if gender == "m" else "Manager (Female)"
    return f"""You are a harsh sales audit expert. Analyze this sales dialogue about '{niche}'.\n{gender_label}. Use correct gender forms: {pr['subject']} {pr['verb_past2']}.\n\nRate across 9 modules (0-10 scale):\n1. Initiative - did they take control?\n2. Commercial IQ - understanding buyer psychology\n3. Objection handling - how well did they address concerns?\n4. Tone - was it appropriate and professional?\n5. Conciseness - did they waste time?\n6. Empathy - did they understand the client?\n7. Product knowledge - do they know '{niche}'?\n8. Stress tolerance - how did they handle pushback?\n9. Deal closing - did they move toward a sale?\n\nGive final score and verdict: [RED - DISQUALIFIED / YELLOW - RESERVE / GREEN - ELITE]\nList 3 main mistakes. Be strict and professional."""

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
    niche = message.text.strip()
    await state.update_data(niche=niche)
    data = await state.get_data()
    lang = data["lang"]
    role = data["role"]

    await message.answer("Connecting to client...")
    sys_p = get_system_prompt(role, lang, niche)
    opening = await ai(
        sys_p + f"\n\nStart the dialogue as the client: you just answered the phone or got a message. "
                f"You are a potential customer, the manager is trying to sell you '{niche}'. "
                f"One short response, be natural and realistic."
    )
    history = [{"role": "assistant", "content": opening}]
    await state.update_data(history=history, msg_count=0)
    await state.set_state(SimStates.dialogue)
    await message.answer(
        f"{TEXTS[lang]['sim_start']}\n\n{opening}\n\n[1/{{MAX_STEPS}}]"
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
        f"{response}\n\n[{{count + 1}}/{{MAX_STEPS}}]"
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
