import asyncio

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from app.config import settings
from app.database import engine
from app.models.base import Base
from app.bot.handlers import router, set_scheduler
from app.workers.scheduler import WorkerScheduler

log = structlog.get_logger()

HAS_PLAYWRIGHT = False
try:
    from app.utils.browser import browser_manager
    HAS_PLAYWRIGHT = True
except ImportError:
    pass


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database_initialized")


async def notify_telegram(bot: Bot, text: str):
    try:
        await bot.send_message(
            chat_id=settings.tg_admin_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error("telegram_notify_error", error=str(e))


async def main():
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
    )

    log.info("starting_job_hunter")

    await init_db()

    session = None
    if settings.tg_api_server:
        session = AiohttpSession(api=settings.tg_api_server)
        log.info("using_custom_tg_api", api=settings.tg_api_server)
    elif settings.tg_proxy:
        from aiohttp import BasicAuth
        session = AiohttpSession(proxy=settings.tg_proxy)
        log.info("using_tg_proxy", proxy=settings.tg_proxy)
    bot = Bot(token=settings.tg_bot_token, session=session)
    dp = Dispatcher()
    dp.include_router(router)

    playwright_ok = HAS_PLAYWRIGHT
    if playwright_ok:
        try:
            await browser_manager.start()
            log.info("playwright_started")
        except Exception as e:
            playwright_ok = False
            log.warning("playwright_start_failed", error=str(e), mode="api_only")
    else:
        log.info("playwright_not_available", mode="api_only")

    scheduler = WorkerScheduler(
        notify_callback=lambda text: notify_telegram(bot, text)
    )
    set_scheduler(scheduler)
    scheduler.start()

    # Telegram user-bot — listens for DMs on second account
    async def on_tg_dm(msg: dict):
        log.info("on_tg_dm_called", sender=msg.get("sender"), preview=msg.get("text", "")[:50])
        from app.workers.message_worker import _save_message
        try:
            saved = await _save_message(msg)
        except Exception as e:
            log.error("on_tg_dm_save_error", error=str(e))
            saved = msg  # still notify even if DB save failed
        if not saved:
            log.info("on_tg_dm_skipped_dedup")
            return
        text = (
            f"📩 <b>Личное сообщение — Telegram (2-й аккаунт)</b>\n\n"
            f"👤 {msg.get('sender') or 'Неизвестно'}"
            + (f" (@{msg['sender_username']})" if msg.get("sender_username") else "")
            + f"\n\n{msg.get('text','')[:600]}"
        )
        try:
            await bot.send_message(
                chat_id=settings.tg_admin_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            log.info("on_tg_dm_notified")
        except Exception as e:
            log.error("tg_userbot_notify_error", error=str(e))

    try:
        from app.services.tg_userbot import init_userbot
        ub = init_userbot(on_tg_dm)
        ub_ok = await ub.start()
        log.info("tg_userbot_init", ok=ub_ok)
    except Exception as e:
        log.warning("tg_userbot_init_failed", error=str(e))

    await notify_telegram(
        bot,
        "🚀 <b>Job Hunter запущен!</b>\n\n"
        f"Позиция: {settings.desired_position}\n"
        f"Зарплата: {settings.desired_salary_min:,}–{settings.desired_salary_max:,}\n"
        f"Интервал: {settings.check_interval_sec // 60} мин\n"
        f"Лимит: {settings.max_applies_per_day} откликов/день\n"
        f"Режим: {'Playwright' if playwright_ok else 'API-only'}",
    )

    # Wait for proxy connectivity before starting polling
    if settings.tg_proxy:
        for attempt in range(20):  # up to ~3 min
            try:
                await bot.get_me()
                log.info("proxy_ready", attempt=attempt + 1)
                break
            except Exception as e:
                log.info("waiting_for_proxy", attempt=attempt + 1, err=str(e)[:80])
                await asyncio.sleep(10)
        else:
            log.error("proxy_unreachable_after_retries")

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.stop()
        if playwright_ok:
            await browser_manager.close()
        await engine.dispose()
        log.info("job_hunter_stopped")


if __name__ == "__main__":
    asyncio.run(main())
