"""
HTTP-вебхук уведомлений ЮMoney (мультиюзер, Фаза 6).

ЮMoney шлёт POST (form-urlencoded) на этот эндпоинт при оплате. Проверяем
sha1-подпись, поднимаем тариф пользователю (по label=jh_<telegram_id>) и
уведомляем его в Telegram. Сервер поднимается рядом с ботом в main.py.
"""
from __future__ import annotations

import structlog
from aiohttp import web

from app.config import settings
from app.database import async_session
from app.services.payments import verify_yoomoney, user_id_from_label, apply_payment
from app.services import yookassa

log = structlog.get_logger()


async def _yoomoney_handler(request: web.Request) -> web.Response:
    data = dict(await request.post())
    if not verify_yoomoney(data):
        log.warning("yoomoney_bad_signature")
        return web.Response(text="bad signature", status=400)

    telegram_id = user_id_from_label(data.get("label", ""))
    if not telegram_id:
        log.warning("yoomoney_no_label", label=data.get("label"))
        return web.Response(text="OK")  # 200, чтобы ЮMoney не долбил повторами

    try:
        amount = int(float(data.get("amount", "0")))
    except ValueError:
        amount = 0

    async with async_session() as session:
        applied = await apply_payment(
            session, telegram_id, provider="yoomoney", amount=amount,
            days=settings.subscription_days, operation_id=data.get("operation_id"),
        )

    if applied:
        bot = request.app.get("bot")
        if bot:
            try:
                await bot.send_message(
                    telegram_id,
                    "✅ Оплата получена, расширенный тариф активирован. Спасибо!",
                )
            except Exception as e:
                log.warning("yoomoney_notify_failed", error=str(e))
    return web.Response(text="OK")


async def _yookassa_handler(request: web.Request) -> web.Response:
    """Вебхук ЮKassa. Подписи нет — подтверждаем статус повторным запросом."""
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="OK")
    if data.get("event") != "payment.succeeded":
        return web.Response(text="OK")
    obj = data.get("object") or {}
    payment_id = obj.get("id")
    # Доверяем только реальному статусу из API.
    verified = await yookassa.fetch_payment(payment_id)
    if not verified or verified.get("status") != "succeeded":
        log.warning("yookassa_unverified", payment_id=payment_id)
        return web.Response(text="OK")

    meta = verified.get("metadata") or {}
    try:
        telegram_id = int(meta.get("telegram_id", "0"))
    except ValueError:
        telegram_id = 0
    if not telegram_id:
        return web.Response(text="OK")
    try:
        amount = int(float((verified.get("amount") or {}).get("value", "0")))
    except ValueError:
        amount = 0
    # Дни из метаданных платежа (неделя/месяц); нет — месяц по умолчанию.
    try:
        days = int(meta.get("days") or settings.subscription_days)
    except ValueError:
        days = settings.subscription_days

    async with async_session() as session:
        applied = await apply_payment(
            session, telegram_id, provider="yookassa", amount=amount,
            days=days, operation_id=payment_id,
        )
    if applied:
        bot = request.app.get("bot")
        if bot:
            try:
                await bot.send_message(
                    telegram_id, "✅ Оплата получена, расширенный тариф активирован. Спасибо!")
            except Exception as e:
                log.warning("yookassa_notify_failed", error=str(e))
    return web.Response(text="OK")


def create_payment_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/yoomoney", _yoomoney_handler)
    app.router.add_post("/yookassa", _yookassa_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    return app
