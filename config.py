import os
from dotenv import load_dotenv

load_dotenv()

# MEXC (مطلوب)
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

# هوية المستخدم في قاعدة البيانات (رقم ثابت — مش تليجرام)
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "1"))

DEFAULT_THRESHOLD = 2.0
DEFAULT_MIN_TRADE_USDT = 5.0
QUOTE_ASSET = "USDT"

# Web dashboard
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET") or "change-me"
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", os.getenv("PORT", "8080")))
