import asyncio

import structlog
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from app.config import settings
from app.database import engine
from app.models.base import Base
import app.models  # noqa: F401 — регистрирует все модели в metadata до create_all
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
        # Лёгкая авто-миграция для существующей SQLite-БД: досоздаём новые
        # колонки users (create_all не делает ALTER для уже созданных таблиц).
        if settings.database_url.startswith("sqlite"):
            migrations = {
                "users": {
                    "tg_api_id": "BIGINT",
                    "tg_api_hash": "TEXT",
                    "tg_session": "TEXT",
                    "tg_userbot_active": "BOOLEAN DEFAULT 0",
                "hh_cookies": "TEXT",
                "connect_reminders": "INTEGER DEFAULT 0",
                "tier_reminders": "INTEGER DEFAULT 0",
                "limit_hint_sent": "INTEGER DEFAULT 0",
                },
                "vacancies": {"account_ref": "VARCHAR(32)", "skip_reason": "VARCHAR(20)",
                              "search_task_id": "INTEGER"},
                "applications": {"account_ref": "VARCHAR(32)", "search_task_id": "INTEGER",
                                 "letter_variant": "VARCHAR(1)"},
                "search_tasks": {
                    "resume_id": "VARCHAR(64)",
                    "resume_title": "VARCHAR(255)",
                    "resume_text": "TEXT",
                    "settings_json": "TEXT",
                    "rec_found": "INTEGER",
                    "last_run_at": "VARCHAR(32)",
                    "invites": "INTEGER",
                    "invites_today": "INTEGER",
                    "views": "INTEGER",
                    "views_today": "INTEGER",
                    "ab_inv_a": "INTEGER",
                    "ab_inv_b": "INTEGER",
                },
            }
            for table, add in migrations.items():
                cols = {r[1] for r in (await conn.exec_driver_sql(
                    f"PRAGMA table_info({table})")).fetchall()}
                for name, ddl in add.items():
                    if name not in cols:
                        await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                        log.info("db_migrate_add_column", table=table, column=name)
            # Привязать историю к основному аккаунту: старые записи с NULL
            # account_ref принадлежат основному аккаунту "u<user_id>". Иначе
            # дедуп не узнаёт уже отработанные вакансии и крутит их вхолостую.
            for table in ("vacancies", "applications"):
                await conn.exec_driver_sql(
                    f"UPDATE {table} SET account_ref = 'u' || user_id "
                    f"WHERE account_ref IS NULL AND user_id IS NOT NULL"
                )
            # Бэкфилл skip_reason для старых отсеянных ИИ вакансий — чтобы
            # новая статистика показала историю, а не только новые прогоны.
            await conn.exec_driver_sql(
                "UPDATE vacancies SET skip_reason = 'ai_low' "
                "WHERE skip_reason IS NULL AND status = 'rejected' "
                "AND ai_score IS NOT NULL"
            )
    # Ограничить доступ к файлу БД (в нём токены/сессии) — только владелец.
    if settings.database_url.startswith("sqlite"):
        import os
        path = settings.database_url.split(":///", 1)[-1]
        try:
            if path and os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError as e:
            log.warning("db_chmod_failed", error=str(e))
    if not settings.encryption_key:
        log.warning("encryption_key_not_set",
                    hint="Задай ENCRYPTION_KEY в .env — токены/сессии хранятся без шифрования")
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


def _is_quiet_hours() -> bool:
    """Тихие часы (МСК): ночью не пушим уведомления (в т.ч. DM 2-го аккаунта)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
    return not (settings.notify_hour_start <= hour < settings.notify_hour_end)


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
    # В мультиюзерном режиме сначала подключаем per-user роутер hh-подключения,
    # чтобы /connect и его FSM (телефон/код) обрабатывались для всех пользователей.
    if settings.mode == "multi":
        from app.bot.hh_connect import router as hh_connect_router
        from app.bot.task_menu import router as task_menu_router
        from app.bot.payments import router as payments_router
        from app.bot.userbot_connect import router as userbot_router
        dp.include_router(userbot_router)     # /forwarding — второй ТГ (userbot)
        dp.include_router(task_menu_router)   # /start, /task, настройки
        dp.include_router(payments_router)    # pay:start, /grant
        dp.include_router(hh_connect_router)  # /connect (FSM телефон/код)

        # Пер-юзерные userbot'ы: пересылка входящих ЛС со второго ТГ.
        from app.userbot.manager import manager as userbot_manager
        userbot_manager.bind_bot(bot)
        asyncio.create_task(userbot_manager.start_all())

        # Вебхук оплаты (если настроен ЮMoney-кошелёк или ЮKassa-магазин)
        if (settings.yoomoney_wallet and settings.yoomoney_secret) or (
                settings.yookassa_shop_id and settings.yookassa_secret_key):
            from app.api.payment_webhook import create_payment_app
            pay_app = create_payment_app(bot)
            runner = web.AppRunner(pay_app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", settings.payment_webhook_port)
            await site.start()
            log.info("payment_webhook_started", port=settings.payment_webhook_port)
    else:
        # Старый одиночный роутер (глобальные /stats, /vacancies, /messages…) —
        # ТОЛЬКО в single-режиме. В multi он бы показывал данные всех пользователей.
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
        notify_callback=lambda text: notify_telegram(bot, text),
        bot=bot,
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
        if _is_quiet_hours():
            log.info("on_tg_dm_quiet_skip")  # сохранили в БД, ночью не пушим
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

    async def _start_userbot_bg():
        try:
            from app.services.tg_userbot import init_userbot
            ub = init_userbot(on_tg_dm)
            ub_ok = await ub.start()
            log.info("tg_userbot_init", ok=ub_ok)
        except Exception as e:
            log.warning("tg_userbot_init_failed", error=str(e))

    # Старый глобальный userbot (один общий аккаунт из .env) — только для
    # single-режима. В multi его заменяет пер-юзерный UserBotManager, иначе
    # один входящий ЛС уходил бы дважды и два клиента делили бы сессию.
    if settings.mode != "multi":
        asyncio.create_task(_start_userbot_bg())

    # Тихий старт: без спама-карточкой при каждом рестарте (только в лог).
    log.info("service_started", mode=settings.mode,
             engine="playwright" if playwright_ok else "api_only")

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
