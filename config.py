import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

# Optional: restrict bot to one Telegram user (your ID)
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

# Defaults (can be changed via Telegram)
DEFAULT_THRESHOLD = 2.0          # % deviation to trigger rebalance
DEFAULT_MIN_TRADE_USDT = 5.0     # minimum order size in USDT
QUOTE_ASSET = "USDT"
