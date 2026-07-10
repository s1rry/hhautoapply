"""
Пер-юзерные Telegram userbot'ы (Telethon) — пересылка входящих ЛС от HR.

Зачем: пользователь указывает в сопроводительном письме свой ВТОРОЙ ТГ-аккаунт
как контакт (чтобы не светить личный). Когда HR пишет на этот аккаунт, бот
пересылает сообщение владельцу в основной чат job-hunter.

Каждый пользователь подключает СВОЙ аккаунт своими api_id/api_hash
(https://my.telegram.org/auth) и входит своим кодом — мастер-доступа у сервиса
нет. StringSession хранится в его строке User (TODO: шифровать at-rest).

Логин — многошаговый (через FSM бота):
  start_login(api_id, api_hash, phone) -> "code_sent"
  submit_code(code)                    -> "ok" | "password" (нужен 2FA-пароль)
  submit_password(pw)                  -> "ok"
После "ok" сессия сохраняется и слушатель запускается.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import structlog

from app.config import settings
from app.database import async_session
from app.models.user import User

log = structlog.get_logger()


def _proxy():
    """SOCKS5-прокси для Telethon (тот же, что для бота на RU-сервере)."""
    if settings.tg_proxy and settings.tg_proxy.startswith("socks5"):
        try:
            import python_socks
            u = urlparse(settings.tg_proxy)
            return (python_socks.ProxyType.SOCKS5, u.hostname, u.port)
        except Exception:
            return None
    return None


class UserBotManager:
    """Держит активные Telethon-клиенты по user_id и незавершённые логины."""

    def __init__(self):
        self._bot = None                       # aiogram Bot — для пересылки владельцу
        self._clients: dict[int, Any] = {}     # user_id -> запущенный TelegramClient
        self._pending: dict[int, dict] = {}    # user_id -> {client, phone, phone_code_hash}

    def bind_bot(self, bot) -> None:
        self._bot = bot

    # ── Логин ───────────────────────────────────────────────────────────
    async def start_login(self, user_id: int, api_id: int, api_hash: str, phone: str) -> dict:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError:
            return {"status": "error", "error": "telethon не установлен на сервере"}

        await self._drop_pending(user_id)
        client = TelegramClient(StringSession(), api_id, api_hash, proxy=_proxy())
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            return {"status": "error", "error": str(e)[:200]}
        self._pending[user_id] = {
            "client": client, "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "api_id": api_id, "api_hash": api_hash,
        }
        return {"status": "code_sent"}

    async def submit_code(self, user_id: int, code: str) -> dict:
        p = self._pending.get(user_id)
        if not p:
            return {"status": "error", "error": "сессия входа потеряна, начни заново"}
        client = p["client"]
        try:
            from telethon.errors import SessionPasswordNeededError
        except ImportError:
            return {"status": "error", "error": "telethon не установлен"}
        try:
            await client.sign_in(phone=p["phone"], code=code, phone_code_hash=p["phone_code_hash"])
        except SessionPasswordNeededError:
            return {"status": "password"}
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}
        return await self._finish(user_id, p)

    async def submit_password(self, user_id: int, password: str) -> dict:
        p = self._pending.get(user_id)
        if not p:
            return {"status": "error", "error": "сессия входа потеряна, начни заново"}
        try:
            await p["client"].sign_in(password=password)
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}
        return await self._finish(user_id, p)

    async def _finish(self, user_id: int, p: dict) -> dict:
        client = p["client"]
        session_str = client.session.save()
        me = await client.get_me()
        uname = getattr(me, "username", None)
        # Сначала запускаем слушатель, и только при успехе помечаем активным —
        # иначе в БД был бы active=True без работающего клиента (рассинхрон).
        self._pending.pop(user_id, None)
        self._attach_and_run(user_id, client, me.id)
        self._clients[user_id] = client
        async with async_session() as session:
            u = await session.get(User, user_id)
            if u:
                u.tg_api_id = p["api_id"]
                u.tg_api_hash = p["api_hash"]
                u.tg_session = session_str
                u.tg_userbot_active = True
                await session.commit()
        return {"status": "ok", "username": uname}

    # ── Слушатель ───────────────────────────────────────────────────────
    async def start_for_user(self, user_id: int) -> bool:
        """Поднять клиента из сохранённой сессии (при старте сервиса)."""
        if user_id in self._clients:
            return True
        async with async_session() as session:
            u = await session.get(User, user_id)
            if not u or not u.tg_userbot_active or not u.tg_session:
                return False
            api_id, api_hash, sess = u.tg_api_id, u.tg_api_hash, u.tg_session
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError:
            return False
        client = TelegramClient(StringSession(sess), api_id, api_hash, proxy=_proxy())
        try:
            await client.connect()
            if not await client.is_user_authorized():
                log.warning("userbot_session_invalid", user_id=user_id)
                return False
            me = await client.get_me()
        except Exception as e:
            log.warning("userbot_start_failed", user_id=user_id, error=str(e)[:120])
            return False
        self._attach_and_run(user_id, client, me.id)
        self._clients[user_id] = client
        return True

    def _attach_and_run(self, user_id: int, client, me_id: int) -> None:
        from telethon import events

        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            try:
                if not event.is_private:
                    return
                sender = await event.get_sender()
                if not sender or getattr(sender, "bot", False) or sender.id == me_id:
                    return
                first = (getattr(sender, "first_name", "") or "").strip()
                last = (getattr(sender, "last_name", "") or "").strip()
                uname = (getattr(sender, "username", "") or "").strip()
                name = (f"{first} {last}".strip()) or uname or f"id{sender.id}"
                await self._forward(user_id, name, uname, event.raw_text or "")
            except Exception as e:
                log.warning("userbot_handler_error", user_id=user_id, error=str(e)[:120])

        asyncio.create_task(self._run(user_id, client))

    async def _run(self, user_id: int, client) -> None:
        try:
            await client.run_until_disconnected()
        except Exception as e:
            log.info("userbot_disconnected", user_id=user_id, error=str(e)[:80])
        finally:
            self._clients.pop(user_id, None)
        # Авто-reconnect: если пользователь не отключал пересылку — переподнять.
        async with async_session() as session:
            u = await session.get(User, user_id)
            still_active = bool(u and u.tg_userbot_active and u.tg_session)
        if still_active:
            await asyncio.sleep(30)
            try:
                await self.start_for_user(user_id)
            except Exception as e:
                log.warning("userbot_reconnect_failed", user_id=user_id, error=str(e)[:120])

    async def _forward(self, owner_id: int, name: str, uname: str, text: str) -> None:
        if not self._bot:
            return
        head = f"📨 <b>Новое сообщение на твой контактный ТГ</b>\n\n👤 {name}"
        if uname:
            head += f" (@{uname})"
        body = (text or "").strip()[:2000] or "(без текста)"
        try:
            await self._bot.send_message(owner_id, f"{head}\n\n{body}", parse_mode="HTML")
        except Exception as e:
            log.error("userbot_forward_error", owner_id=owner_id, error=str(e)[:120])

    # ── Управление ──────────────────────────────────────────────────────
    async def stop_for_user(self, user_id: int, forget: bool = False) -> None:
        client = self._clients.pop(user_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        await self._drop_pending(user_id)
        if forget:
            async with async_session() as session:
                u = await session.get(User, user_id)
                if u:
                    u.tg_userbot_active = False
                    u.tg_session = None
                    u.tg_api_id = None
                    u.tg_api_hash = None
                    await session.commit()

    async def _drop_pending(self, user_id: int) -> None:
        p = self._pending.pop(user_id, None)
        if p:
            try:
                await p["client"].disconnect()
            except Exception:
                pass

    async def start_all(self) -> int:
        """Поднять всех, у кого включён userbot (при старте сервиса)."""
        from sqlalchemy import select
        started = 0
        async with async_session() as session:
            ids = (await session.execute(
                select(User.id).where(User.tg_userbot_active.is_(True), User.tg_session.is_not(None))
            )).scalars().all()
        for uid in ids:
            try:
                if await self.start_for_user(uid):
                    started += 1
            except Exception as e:
                log.warning("userbot_start_all_item_failed", user_id=uid, error=str(e)[:120])
        log.info("userbot_start_all", started=started, total=len(ids))
        return started


manager = UserBotManager()
