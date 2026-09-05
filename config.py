import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

# Optional: Telethon user session to READ messages from signal bots
# Never commit real values — set only in Railway Variables
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")  # from my.telegram.org
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE")  # e.g. +2011...
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION")  # StringSession if already generated

DEFAULT_THRESHOLD = 2.0
DEFAULT_MIN_TRADE_USDT = 5.0
QUOTE_ASSET = "USDT"
