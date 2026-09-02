import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

# Optional: restrict to one Discord user ID
ADMIN_DISCORD_ID = os.getenv("ADMIN_DISCORD_ID")

DEFAULT_THRESHOLD = 2.0
DEFAULT_MIN_TRADE_USDT = 5.0
QUOTE_ASSET = "USDT"
