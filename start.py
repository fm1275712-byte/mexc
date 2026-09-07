"""
تشغيل مشترك: بوت تليجرام + لوحة التحكم على نفس خدمة Railway.
Railway يضبط PORT تلقائياً — الداشبورد يستمع عليه.
"""
import os
import threading
import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("start")


def run_bot():
    try:
        import bot
        bot.main()
    except Exception as e:
        logger.exception("Telegram bot crashed: %s", e)


def run_dashboard():
    import uvicorn
    port = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8080")))
    logger.info("Dashboard listening on 0.0.0.0:%s", port)
    uvicorn.run("dashboard:app", host="0.0.0.0", port=port, reload=False, log_level="info")


if __name__ == "__main__":
    # البوت في thread خلفي — الداشبورد على الـ PORT العام
    t = threading.Thread(target=run_bot, name="telegram-bot", daemon=True)
    t.start()
    logger.info("Telegram bot thread started")
    run_dashboard()
