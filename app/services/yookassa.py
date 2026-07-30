"""
Приём оплаты через ЮKassa (магазин).

Создаём платёж через API (Basic-auth shopId:secretKey), пользователь платит по
confirmation_url, ЮKassa шлёт вебхук payment.succeeded. Подпись ЮKassa не даёт,
поэтому уведомление подтверждаем повторным запросом статуса платежа по его id
(доверяем только тому, что реально succeeded в API).

Docs: https://yookassa.ru/developers/api
"""
from __future__ import annotations

import uuid

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

API = "https://api.yookassa.ru/v3"


def _auth() -> tuple[str, str]:
    return (settings.yookassa_shop_id, settings.yookassa_secret_key)


def is_configured() -> bool:
    return bool(settings.yookassa_shop_id and settings.yookassa_secret_key)


async def create_payment(telegram_id: int, amount: int, description: str,
                         days: int | None = None) -> str | None:
    """Создать платёж, вернуть ссылку на оплату (confirmation_url) или None.
    days — на сколько дней тариф (неделя/месяц); вебхук начислит именно их."""
    if not is_configured():
        return None
    meta = {"telegram_id": str(telegram_id)}
    if days:
        meta["days"] = str(days)
    body = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": settings.yookassa_return_url},
        "description": description[:128],
        "metadata": meta,
    }
    headers = {"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{API}/payments", json=body, headers=headers, auth=_auth())
        if r.status_code in (200, 201):
            return ((r.json() or {}).get("confirmation") or {}).get("confirmation_url")
        log.warning("yookassa_create_failed", status=r.status_code, body=r.text[:300])
    except Exception as e:
        log.warning("yookassa_create_error", error=str(e))
    return None


async def fetch_payment(payment_id: str) -> dict | None:
    """Получить платёж по id (для подтверждения статуса из вебхука)."""
    if not is_configured() or not payment_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{API}/payments/{payment_id}", auth=_auth())
        if r.status_code == 200:
            return r.json()
        log.warning("yookassa_fetch_failed", status=r.status_code)
    except Exception as e:
        log.warning("yookassa_fetch_error", error=str(e))
    return None
