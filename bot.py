import os
import asyncio
import logging
import random
import re
import json
import time
import smtplib
import hashlib
import io
from datetime import datetime
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
from fpdf import FPDF

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
logging.basicConfig(level=logging.INFO)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_EMAIL = "monkifani@gmail.com"
GMAIL_PASS = os.getenv("GMAIL_PASSWORD")
ADMIN_TG_ID = os.getenv("ADMIN_TG_ID") # Твой ID для получения отчетов

IS_PROD = os.getenv("IS_PROD") == "1"
IS_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT"))
IS_RENDER = bool(os.getenv("RENDER"))
PORT = int(os.getenv("PORT", "8080" if (IS_PROD or IS_RAILWAY or IS_RENDER) else "8009"))

WEBHOOK_PATH = "/api/tgwebhook"
RENDER_DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "")
REPLIT_DOMAIN = os.getenv("REPLIT_DOMAINS", "").split(",")[0].strip()

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
BOT_VERSION = "2.1.0"

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def clean_thinking_tags(text: str) -> str:
    return re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL).strip()

def generate_pdf_report(session_data):
    pdf = FPDF()
    pdf.add_page()
    
    # Пытаемся подключить кириллицу (нужен файлDejaVuSans.ttf в корне)
    font_path = "DejaVuSans.ttf"
    if os.path.exists(font_path):
        pdf.add_font("DejaVu", "", font_path, uni=True)
        pdf.set_font("DejaVu", size=12)
        font_name = "DejaVu"
    else:
        pdf.set_font("Arial", size=12)
        font_name = "Arial"

    pdf.set_font(font_name, size=16)
    pdf.cell(200, 10, txt="AuditCore AI - Отчет о симуляции", ln=True, align='C')
    pdf.ln(10)

    pdf.set_font(font_name, size=12)
    pdf.cell(200, 10, txt=f"Менеджер: {session_data.get('user_name')}", ln=True)
    pdf.cell(200, 10, txt=f"Ниша: {session_data.get('niche')} | Роль: {session_data.get('role')}", ln=True)
    pdf.cell(200, 10, txt=f"Оценка: {session_data.get('score')}/15", ln=True)
    pdf.cell(200, 10, txt=f"AI-Детект: {session_data.get('ai_detect_percent')}%", ln=True)
    pdf.ln(10)

    pdf.multi_cell(0, 10, txt=f"ВЕРДИКТ СУДЬИ:\n{session_data.get('verdict')}")
    
    # Возвращаем байты
    return pdf.output(dest='S').encode('latin1')

def send_email(subject, body, pdf_bytes=None, filename="report.pdf"):
    if not GMAIL_PASS:
        logging.warning("GMAIL_PASSWORD не установлен")
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = ADMIN_EMAIL
        msg["To"] = ADMIN_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        
        if pdf_bytes:
            part = MIMEApplication(pdf_bytes, Name=filename)
            part['Content-Disposition'] = f'attachment; filename="{filename}"'
            msg.attach(part)
            
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(ADMIN_EMAIL, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        logging.info(f"Email отправлен: {subject}")
    except Exception as e:
        logging.error(f"Mail error: {e}")

# (Далее идут твои классы Database, SimStates и словари TEXTS из начала твоего кода...)
# Для экономии места я пропущу дублирование словарей, но в твоем файле они должны быть здесь.

# [ВСТАВИТЬ ТВОИ DATABASE, TEXTS, GENERATE_CLIENT_STATE, PROMPTS ЗДЕСЬ]

# ============================================================
# ГЕНЕРАЦИЯ ОТВЕТОВ
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
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part.from_text(text=msg["content"])]))
        else:
            contents = prompt_or_history

        response = await asyncio.to_thread(client.models.generate_content, model=MODEL_ID, contents=contents, config=config)
        result = clean_thinking_tags(response.text.strip())
        # Очистка спецсимволов
        result = re.sub(r'[*_`]', '', result).strip()
        return result if result else "..."
    except Exception as e:
        logging.error(f"AI error: {e}")
        return "Произошла ошибка связи с ИИ."

# ============================================================
# ХЕНДЛЕРЫ
# ============================================================

@dp.message(SimStates.dialogue, F.text)
async def handle_dialogue(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lang, role, niche = data["lang"], data["role"], data["niche"]
    history = data["history"]
    count = data.get("msg_count", 0) + 1
    response_times = data.get("response_times", [])
    last_msg_time = data.get("last_msg_time", time.time())

    if len(message.text.strip()) < MIN_MESSAGE_LENGTH:
        await message.answer(TEXTS[lang]["too_short"])
        return

    response_times.append(time.time() - last_msg_time)
    history.append({"role": "manager", "content": message.text})
    await bot.send_chat_action(message.chat.id, "typing")

    if count >= MAX_STEPS:
        await message.answer(TEXTS[lang]["analyzing"])
        
        # Запуск аналитики
        full_log = "\n".join([f"{m['role']}: {m['content']}" for m in history])
        manager_msgs = [m["content"] for m in history if m["role"] == "manager"]
        
        judge_task = generate_response(f"АНАЛИЗ:\n{full_log}", system_instruction=get_judge_system_instruction(data['gender'], lang, niche))
        detect_task = run_ai_detection(manager_msgs)
        
        judge_res, detect_res = await asyncio.gather(judge_task, detect_task)
        score = extract_score_from_verdict(judge_res)
        
        session_data = {
            "user_id": message.from_user.id,
            "user_name": message.from_user.full_name,
            "role": role, "niche": niche, "history": history,
            "verdict": judge_res, "score": score,
            "ai_detect_percent": detect_res["percent"],
            "ai_detect_analysis": detect_res["full_analysis"],
            "response_times": response_times,
            "avg_response_time": sum(response_times)/len(response_times)
        }
        
        session_id = db.save_session(session_data)
        
        # Генерация и отправка PDF
        pdf_bytes = generate_pdf_report(session_data)
        doc = BufferedInputFile(pdf_bytes, filename=f"Report_{session_id}.pdf")
        await message.answer_document(doc, caption=f"Твой результат: {score}/15")
        
        # Отчет админу
        if ADMIN_TG_ID:
            try:
                await bot.send_document(ADMIN_TG_ID, doc, caption=f"Новый аудит: {message.from_user.full_name} ({score}/15)")
            except: pass
            
        await asyncio.to_thread(send_email, f"Audit {message.from_user.full_name}", judge_res, pdf_bytes)
        await state.set_state(SimStates.menu)
        return

    # Продолжение диалога
    sys_inst = build_full_system_instruction(lang, role, niche, data['client_state'])
    ai_resp = await generate_response(history, system_instruction=sys_inst)
    history.append({"role": "client", "content": ai_resp})
    
    await state.update_data(history=history, msg_count=count, last_msg_time=time.time(), response_times=response_times)
    await message.answer(f"<b>{role}:</b>\n{ai_resp}", parse_mode="HTML")

# ============================================================
# ЗАПУСК
# ============================================================

app = FastAPI()

@app.post(WEBHOOK_PATH)
async def tg_webhook(request: Request):
    await dp.feed_update(bot, types.Update(**await request.json()))
    return "OK"

if __name__ == "__main__":
    import uvicorn
    if WEBHOOK_URL:
        asyncio.run(bot.set_webhook(WEBHOOK_URL))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
