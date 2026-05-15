import asyncio

import structlog
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode

from app.config import settings
from app.database import engine
from app.models.base import Base
from app.bot.handlers import router
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

    bot = Bot(token=settings.tg_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    if HAS_PLAYWRIGHT:
        try:
            await browser_manager.start()
            log.info("playwright_started")
        except Exception as e:
            HAS_PLAYWRIGHT = False
            log.warning("playwright_start_failed", error=str(e), mode="api_only")
    else:
        log.info("playwright_not_available", mode="api_only")

    scheduler = WorkerScheduler(
        notify_callback=lambda text: notify_telegram(bot, text)
    )
    scheduler.start()

    await notify_telegram(
        bot,
        "🚀 <b>Job Hunter запущен!</b>\n\n"
        f"Позиция: {settings.desired_position}\n"
        f"Зарплата: {settings.desired_salary_min:,}–{settings.desired_salary_max:,}\n"
        f"Интервал: {settings.check_interval_sec // 60} мин\n"
        f"Лимит: {settings.max_applies_per_day} откликов/день\n"
        f"Режим: {'Playwright' if HAS_PLAYWRIGHT else 'API-only'}",
    )

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.stop()
        if HAS_PLAYWRIGHT:
            await browser_manager.close()
        await engine.dispose()
        log.info("job_hunter_stopped")


if __name__ == "__main__":
    asyncio.run(main())
