"""
Optional Telethon listener: reads messages from configured signal bots
using a USER account session (not the bot token).

Credentials MUST come from env vars only — never hardcode.
"""
import asyncio
import logging
from typing import Callable, Awaitable, Optional, Set

import config

logger = logging.getLogger(__name__)

_client = None
_task = None


def telethon_configured() -> bool:
    return bool(
        config.TELEGRAM_API_ID
        and config.TELEGRAM_API_HASH
        and (config.TELEGRAM_SESSION or config.TELEGRAM_PHONE)
    )


async def _build_client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(config.TELEGRAM_API_ID)
    api_hash = config.TELEGRAM_API_HASH
    if config.TELEGRAM_SESSION:
        client = TelegramClient(StringSession(config.TELEGRAM_SESSION), api_id, api_hash)
    else:
        # session file in /tmp — for Railway prefer StringSession after first auth
        client = TelegramClient("/tmp/mexc_signal_session", api_id, api_hash)
    await client.start(phone=config.TELEGRAM_PHONE if not config.TELEGRAM_SESSION else None)
    return client


async def start_listener(
    get_allowed_ids: Callable[[], Set[int]],
    get_allowed_usernames: Callable[[], Set[str]],
    on_signal_text: Callable[[str, str], Awaitable[None]],
):
    """
    on_signal_text(text, source_label) called when a matching bot posts.
    """
    global _client
    if not telethon_configured():
        logger.info("Telethon not configured — signal listener disabled")
        return

    try:
        from telethon import events
    except ImportError:
        logger.error("telethon not installed")
        return

    _client = await _build_client()
    me = await _client.get_me()
    logger.info("Telethon signal listener started as %s", me.username or me.id)

    @ _client.on(events.NewMessage)
    async def handler(event):
        try:
            sender = await event.get_sender()
            if not sender:
                return
            sid = int(getattr(sender, "id", 0) or 0)
            suser = (getattr(sender, "username", None) or "").lower()
            allowed_ids = get_allowed_ids() or set()
            allowed_users = {u.lower().lstrip("@") for u in (get_allowed_usernames() or set())}
            if sid not in allowed_ids and suser not in allowed_users:
                return
            text = event.raw_text or ""
            if not text.strip():
                return
            label = suser or str(sid)
            await on_signal_text(text, label)
        except Exception as e:
            logger.exception("signal handler error: %s", e)

    await _client.run_until_disconnected()


def start_listener_background(get_allowed_ids, get_allowed_usernames, on_signal_text):
    global _task
    if not telethon_configured():
        return None

    loop = asyncio.get_event_loop()

    async def runner():
        try:
            await start_listener(get_allowed_ids, get_allowed_usernames, on_signal_text)
        except Exception as e:
            logger.exception("Telethon listener stopped: %s", e)

    _task = loop.create_task(runner())
    return _task
