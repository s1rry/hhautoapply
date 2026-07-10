"""
Бот поддержки — мост между пользователями и админом.

Пользователь пишет боту → сообщение уходит админу с пометкой id.
Админ отвечает reply на это сообщение → бот пересылает ответ пользователю.
Состояние не хранится: id пользователя берётся из текста сообщения, на
которое админ отвечает (переживает перезапуск).

Запуск: python -m app.support_bot (отдельный systemd-сервис).
"""
from __future__ import annotations

import asyncio
import re

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession

from app.config import settings

log = structlog.get_logger()

_UID_RE = re.compile(r"id (\d+)")


async def main():
    if not settings.support_bot_token:
        log.error("support_bot_no_token")
        return

    session = AiohttpSession(proxy=settings.tg_proxy) if settings.tg_proxy else None
    bot = Bot(token=settings.support_bot_token, session=session)
    dp = Dispatcher()
    admin = str(settings.tg_admin_chat_id or "")

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        if str(message.chat.id) == admin:
            await message.answer("Это бот поддержки. Отвечай reply на сообщения пользователей.")
        else:
            await message.answer(
                "🆘 Поддержка. Напиши свой вопрос — мы ответим здесь же."
            )

    # Ответ админа (reply на пересланное сообщение) → пользователю
    @dp.message(F.reply_to_message, F.chat.id == (int(admin) if admin.lstrip('-').isdigit() else 0))
    async def admin_reply(message: Message):
        src = message.reply_to_message.text or ""
        m = _UID_RE.search(src)
        if not m:
            return
        uid = int(m.group(1))
        try:
            await bot.send_message(uid, f"🆘 <b>Поддержка:</b>\n\n{message.text or ''}", parse_mode="HTML")
            await message.answer("✅ Отправлено пользователю.")
        except Exception as e:
            await message.answer(f"❌ Не удалось отправить: {e}")

    # Сообщение от пользователя → админу
    @dp.message()
    async def from_user(message: Message):
        if str(message.chat.id) == admin:
            return  # админ без reply — игнор
        u = message.from_user
        uname = f" @{u.username}" if u and u.username else ""
        name = u.full_name if u else "?"
        text = message.text or message.caption or "(без текста)"
        await bot.send_message(
            admin,
            f"💬 <b>От {name}{uname}</b> (id {message.chat.id}):\n\n{text}",
            parse_mode="HTML",
        )
        await message.answer("Отправлено в поддержку, скоро ответим 🙌")

    log.info("support_bot_started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
