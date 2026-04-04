"""Configuration file for bot constants and environment variables."""
import os

# ============================================================
# API KEYS
# ============================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GMAIL_PASS = os.getenv("GMAIL_PASSWORD")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "monkifani@gmail.com")

# ============================================================
# ENVIRONMENT DETECTION
# ============================================================
IS_PROD = os.getenv("IS_PROD") == "1"
IS_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT"))
IS_RENDER = bool(os.getenv("RENDER"))
PORT = int(os.getenv("PORT", "8080" if (IS_PROD or IS_RAILWAY or IS_RENDER) else "8009"))

# ============================================================
# WEBHOOK CONFIGURATION
# ============================================================
REPLIT_DOMAIN = os.getenv("REPLIT_DOMAINS", "").split(",")[0].strip()
RENDER_DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH = "/api/tgwebhook"

if RENDER_DOMAIN:
    WEBHOOK_URL = f"{RENDER_DOMAIN}{WEBHOOK_PATH}"
elif REPLIT_DOMAIN:
    WEBHOOK_URL = f"https://{REPLIT_DOMAIN}{WEBHOOK_PATH}"
olog:
    WEBHOOK_URL = None

# ============================================================
# AI MODEL CONFIGURATION
# ============================================================
MODEL_ID = "gemini-2.5-flash"

# ============================================================
# BOT SETTINGS
# ============================================================
MAX_STEPS = 6
MIN_MESSAGE_LENGTH = 3
BOT_VERSION = "2.0.0"
MAX_SESSIONS_PER_DAY = 10
COMPANY_CODE_LENGTH = 8

# ============================================================
# TIMEOUTS (seconds)
# ============================================================
RESPONSE_TIMEOUT = 15.0
AI_ANALYSIS_TIMEOUT = 20.0

# ============================================================
# TEMPERATURE SETTINGS FOR AI
# ============================================================
TEMPERATURE_DEFAULT = 0.7
TEMPERATURE_VALIDATION = 0.1
TEMPERATURE_DIALOGUE = 0.85
TEMPERATURE_ANALYSIS = 0.3

# ============================================================
# GENDER PRONOUNS MAPPING
# ============================================================
PRONOUNS = {
    "m": {
        "pr": "он",
        "pr_v": "показал",
        "pr_sold": "продавал",
        "pr_adapted": "адаптировал",
        "pr_pressed": "давил",
        "pr_was": "был",
    },
    "f": {
        "pr": "она",
        "pr_v": "показала",
        "pr_sold": "продавала",
        "pr_adapted": "адаптировала",
        "pr_pressed": "давила",
        "pr_was": "была",
    }
}

# ============================================================
# DATABASE CONFIGURATION
# ============================================================
DB_FILE = "database.json"
BACKUP_INTERVAL = 3600  # seconds