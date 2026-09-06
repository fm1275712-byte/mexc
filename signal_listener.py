"""
Telethon listener: reads messages from configured signal bots/channels
using a USER account session (not the bot token).

Runs in a dedicated background thread with its own asyncio loop so it
does not conflict with python-telegram-bot's event loop.

Credentials MUST come from env vars only — never hardcode.
"""
import asyncio
import logging
import threading
from typing import Callable, Awaitable, Optional, Set, List

import config

logger = logging.getLogger(__name__)

_client = None
_thread: Optional[threading.Thread] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_stop_event: Optional[threading.Event] = None


def telethon_configured() -> bool:
    return bool(
        config.TELEGRAM_API_ID
        and config.TELEGRAM_API_HASH
        and (config.TELEGRAM_SESSION or config.TELEGRAM_PHONE)
    )


def _normalize_id(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _expand_ids(ids: Set[int]) -> Set[int]:
    """Expand Telegram id forms: 446.. <-> -100446.. <-> 100446.."""
    expanded = set(ids)
    for cid in list(ids):
        if not cid:
            continue
        expanded.add(cid)
        expanded.add(abs(cid))
        s = str(abs(cid))
        if s.startswith("100") and len(s) > 3:
            short = int(s[3:])
            expanded.add(short)
            expanded.add(-short)
            expanded.add(-int(s))
            expanded.add(int(s))
        else:
            expanded.add(int(f"-100{abs(cid)}"))
            expanded.add(int(f"100{abs(cid)}"))
    return expanded


async def _build_client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(config.TELEGRAM_API_ID)
    api_hash = config.TELEGRAM_API_HASH
    if config.TELEGRAM_SESSION:
        client = TelegramClient(StringSession(config.TELEGRAM_SESSION), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "TELEGRAM_SESSION is set but not authorized. "
                "Generate a fresh StringSession locally."
            )
    else:
        client = TelegramClient("/tmp/mexc_signal_session", api_id, api_hash)
        await client.start(phone=config.TELEGRAM_PHONE)
    return client


async def _run_listener(
    get_allowed_ids: Callable[[], Set[int]],
    get_allowed_usernames: Callable[[], Set[str]],
    on_signal_text: Callable[[str, str], Awaitable[None]],
):
    global _client
    from telethon import events

    _client = await _build_client()
    me = await _client.get_me()
    logger.info(
        "Telethon signal listener started as %s (id=%s)",
        me.username or me.first_name,
        me.id,
    )

    @ _client.on(events.NewMessage)
    async def handler(event):
        try:
            text = (event.raw_text or "").strip()
            if not text:
                return

            allowed_ids = _expand_ids(get_allowed_ids() or set())
            allowed_users = {
                u.lower().lstrip("@") for u in (get_allowed_usernames() or set()) if u
            }
            if not allowed_ids and not allowed_users:
                return

            candidates_ids: Set[int] = set()
            candidates_names: Set[str] = set()
            label = "unknown"

            # Sender (user or bot)
            try:
                sender = await event.get_sender()
            except Exception:
                sender = None
            if sender:
                sid = _normalize_id(getattr(sender, "id", 0))
                if sid:
                    candidates_ids.add(sid)
                suser = (getattr(sender, "username", None) or "").lower()
                if suser:
                    candidates_names.add(suser)
                    label = suser
                elif sid:
                    label = str(sid)

            # Chat itself (channel / group / private)
            chat = event.chat
            if chat is not None:
                cid = _normalize_id(getattr(chat, "id", 0) or event.chat_id)
                if cid:
                    candidates_ids.add(cid)
                cuser = (getattr(chat, "username", None) or "").lower()
                if cuser:
                    candidates_names.add(cuser)
                    if label == "unknown":
                        label = cuser

            # event.chat_id always available
            if event.chat_id:
                candidates_ids.add(_normalize_id(event.chat_id))

            candidates_ids = _expand_ids(candidates_ids)

            matched = False
            for cid in candidates_ids:
                if cid in allowed_ids:
                    matched = True
                    break
            if not matched:
                for name in candidates_names:
                    if name in allowed_users:
                        matched = True
                        label = name
                        break

            if not matched:
                logger.debug(
                    "telethon signal ignored (no match) chat=%s ids=%s names=%s",
                    event.chat_id,
                    candidates_ids,
                    candidates_names,
                )
                return

            logger.info("Telethon signal matched from %s | text[:80]=%s", label, text[:80])
            await on_signal_text(text, label)
        except Exception as e:
            logger.exception("telethon signal handler error: %s", e)

    await _client.run_until_disconnected()


def _thread_main(
    get_allowed_ids: Callable[[], Set[int]],
    get_allowed_usernames: Callable[[], Set[str]],
    on_signal_text: Callable[[str, str], Awaitable[None]],
):
    global _loop, _client
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(
            _run_listener(get_allowed_ids, get_allowed_usernames, on_signal_text)
        )
    except Exception as e:
        logger.exception("Telethon listener thread stopped: %s", e)
    finally:
        try:
            if _client:
                _loop.run_until_complete(_client.disconnect())
        except Exception:
            pass
        _loop.close()
        logger.info("Telethon listener thread exited")


def start_listener_background(
    get_allowed_ids: Callable[[], Set[int]],
    get_allowed_usernames: Callable[[], Set[str]],
    on_signal_text: Callable[[str, str], Awaitable[None]],
) -> Optional[threading.Thread]:
    """
    Start Telethon in a daemon thread. Safe to call once from PTB post_init.
    on_signal_text is an async callback; it will be awaited on the Telethon loop.
    If it needs to talk to the PTB bot, schedule work onto the PTB loop inside it.
    """
    global _thread, _stop_event
    if not telethon_configured():
        logger.info("Telethon not configured — signal listener disabled "
                     "(set TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_SESSION or TELEGRAM_PHONE)")
        return None
    if _thread and _thread.is_alive():
        logger.warning("Telethon listener already running")
        return _thread

    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=_thread_main,
        args=(get_allowed_ids, get_allowed_usernames, on_signal_text),
        name="telethon-signal-listener",
        daemon=True,
    )
    _thread.start()
    logger.info("Telethon signal listener thread started")
    return _thread


def stop_listener():
    global _client, _loop
    try:
        if _client and _loop and _loop.is_running():
            asyncio.run_coroutine_threadsafe(_client.disconnect(), _loop)
    except Exception as e:
        logger.warning("stop_listener: %s", e)
