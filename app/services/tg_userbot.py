"""
Telegram user-bot — listens for DMs on the user's second account and
forwards them to the main job-hunter bot for notification & AI reply.
"""
import asyncio
from typing import Callable, Awaitable

import structlog

from app.config import settings

log = structlog.get_logger()


class TGUserBot:
    def __init__(self, on_message: Callable[[dict], Awaitable[None]]):
        self.client = None
        self.on_message = on_message
        self._task: asyncio.Task | None = None

    async def start(self):
        if not (settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session_string):
            log.info("tg_userbot_skip", reason="credentials not set")
            return False

        try:
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession
        except ImportError:
            log.warning("tg_userbot_skip", reason="telethon not installed")
            return False

        # Optional SOCKS5 proxy (reuse WARP if set)
        proxy = None
        if settings.tg_proxy and settings.tg_proxy.startswith("socks5"):
            # parse socks5://host:port
            try:
                import python_socks
                from urllib.parse import urlparse
                u = urlparse(settings.tg_proxy)
                proxy = (python_socks.ProxyType.SOCKS5, u.hostname, u.port)
            except Exception:
                proxy = None

        self.client = TelegramClient(
            StringSession(settings.telegram_session_string),
            settings.telegram_api_id,
            settings.telegram_api_hash,
            proxy=proxy,
        )

        # Retry connection — Telethon over WARP SOCKS sometimes fails on first try
        last_err = None
        for attempt in range(6):
            try:
                await self.client.start()
                last_err = None
                break
            except Exception as e:
                last_err = e
                log.info("tg_userbot_start_retry", attempt=attempt + 1, err=str(e)[:80])
                await asyncio.sleep(10)
        if last_err:
            log.error("tg_userbot_start_error", error=str(last_err))
            return False

        me = await self.client.get_me()
        log.info("tg_userbot_started", username=getattr(me, "username", None), id=me.id)

        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event):
            try:
                log.info(
                    "tg_userbot_incoming",
                    is_private=event.is_private,
                    chat_id=getattr(event, "chat_id", None),
                    preview=(event.raw_text or "")[:50],
                )
                # Only personal DMs from individuals (not channels/groups/bots)
                if not event.is_private:
                    log.info("tg_userbot_skip_not_private")
                    return
                sender = await event.get_sender()
                if not sender:
                    log.info("tg_userbot_skip_no_sender")
                    return
                if getattr(sender, "bot", False):
                    log.info("tg_userbot_skip_bot", sender_id=sender.id)
                    return
                # Skip self
                if sender.id == me.id:
                    log.info("tg_userbot_skip_self")
                    return

                first = (sender.first_name or "").strip()
                last = (sender.last_name or "").strip()
                username = (sender.username or "").strip()
                full_name = (f"{first} {last}".strip()) or username or f"id{sender.id}"

                msg = {
                    "platform": "telegram",
                    "sender": full_name,
                    "sender_username": username,
                    "company": "",
                    "title": "",
                    "text": (event.raw_text or "")[:2000],
                    # Unique per message — avoids dedup collapsing all DMs
                    # from the same sender into one
                    "thread_id": f"tg_{sender.id}_{event.id}",
                    "chat_id": sender.id,
                    "has_unread": True,
                }
                await self.on_message(msg)
            except Exception as e:
                log.warning("tg_userbot_handler_error", error=str(e))

        self._task = asyncio.create_task(self.client.run_until_disconnected())
        return True

    async def send_reply(self, chat_id: int, text: str) -> bool:
        if not self.client:
            return False
        try:
            await self.client.send_message(chat_id, text)
            return True
        except Exception as e:
            log.error("tg_userbot_send_error", error=str(e), chat_id=chat_id)
            return False

    async def stop(self):
        if self.client:
            await self.client.disconnect()


tg_userbot: TGUserBot | None = None


def init_userbot(on_message: Callable[[dict], Awaitable[None]]) -> TGUserBot:
    global tg_userbot
    tg_userbot = TGUserBot(on_message)
    return tg_userbot
