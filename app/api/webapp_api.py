"""HTTP API для Telegram Mini App (поиск вакансий, профиль).

Отдельное aiohttp-приложение, поднимается рядом с ботом в том же процессе
(см. main.py). Все запросы, кроме /api/health, требуют валидную Telegram
initData — проверяется на сервере (webapp_auth). Поиск идёт под личным
OAuth-токеном пользователя (HHUserClient), поэтому чужие данные недоступны.

Наружу проксируется nginx'ом (hh.volnacrm.ru → 127.0.0.1:miniapp_port).
"""
from __future__ import annotations

import datetime as _dt

import structlog
from aiohttp import web
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.user import User
from app.parsers.hh_user_client import HHUserClient
from app.api.webapp_auth import telegram_id_from_init_data

log = structlog.get_logger()

# Параметры поиска hh, которые принимаем как повторяющиеся (мультивыбор).
_MULTI = (
    "area", "metro", "experience", "employment", "schedule", "work_format",
    "professional_role", "industry", "education", "label", "search_field",
    "driver_license_types",
)
# Одиночные скалярные параметры hh.
_SCALAR = ("text", "salary", "currency", "only_with_salary", "employer_id",
           "period", "date_from", "order_by")

_ALLOWED_ORDER = {"relevance", "publication_time", "salary_desc", "salary_asc"}


def _build_hh_params(query) -> dict:
    """Собрать безопасный dict параметров hh из query-строки запроса.

    Только whitelist-ключи; значения — как есть (hh валидирует коды сам,
    неизвестные молча игнорирует). Пагинация/сортировка обрабатываются отдельно.
    """
    params: dict = {}
    for key in _MULTI:
        vals = [v for v in query.getall(key, []) if v]
        if vals:
            params[key] = vals
    for key in _SCALAR:
        v = query.get(key)
        if v:
            params[key] = v
    order = query.get("order_by")
    if order in _ALLOWED_ORDER:
        params["order_by"] = order
    elif "order_by" in params:
        params.pop("order_by")
    return params


def _vacancy_card(v: dict) -> dict:
    """Урезать вакансию hh до карточки для списка (без лишнего веса)."""
    salary = v.get("salary") or {}
    emp = v.get("employer") or {}
    area = v.get("area") or {}
    return {
        "id": v.get("id"),
        "name": v.get("name"),
        "company": emp.get("name"),
        "company_logo": (emp.get("logo_urls") or {}).get("90") if emp.get("logo_urls") else None,
        "area": area.get("name"),
        "salary_from": salary.get("from"),
        "salary_to": salary.get("to"),
        "currency": salary.get("currency"),
        "experience": (v.get("experience") or {}).get("name"),
        "schedule": (v.get("schedule") or {}).get("name"),
        "employment": (v.get("employment") or {}).get("name"),
        "published_at": v.get("published_at"),
        "url": v.get("alternate_url"),
        "requirement": (v.get("snippet") or {}).get("requirement"),
        "responsibility": (v.get("snippet") or {}).get("responsibility"),
    }


async def _client_for(user: User) -> HHUserClient | None:
    """Построить HHUserClient из токенов пользователя (или None, если не подключён)."""
    if not user.hh_connected or not user.hh_access_token:
        return None
    exp = user.hh_token_expires
    expires_at = exp.timestamp() if isinstance(exp, _dt.datetime) else 0.0
    return HHUserClient(
        access_token=user.hh_access_token or "",
        refresh_token=user.hh_refresh_token or "",
        resume_id=user.hh_resume_id,
        expires_at=expires_at,
    )


async def _persist_token(session, user: User, client: HHUserClient) -> None:
    """Сохранить ротированный токен, если hh его обновил во время запроса."""
    if not client.new_token:
        return
    user.hh_access_token = client.new_token["access_token"]
    user.hh_refresh_token = client.new_token["refresh_token"]
    user.hh_token_expires = _dt.datetime.fromtimestamp(
        client.new_token["expires_at"], tz=_dt.timezone.utc)
    await session.commit()


# ---- middleware: аутентификация по Telegram initData ------------------------

@web.middleware
async def _auth_mw(request: web.Request, handler):
    if request.path == "/api/health":
        return await handler(request)
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("tma "):
            init_data = auth[4:]
    tid = telegram_id_from_init_data(init_data, settings.tg_bot_token)
    if not tid:
        return web.json_response({"error": "unauthorized"}, status=401)
    request["telegram_id"] = tid
    return await handler(request)


# ---- handlers ---------------------------------------------------------------

async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _me(request: web.Request) -> web.Response:
    tid = request["telegram_id"]
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == tid))).scalar_one_or_none()
        if not user:
            return web.json_response({"connected": False, "exists": False})
        return web.json_response({
            "exists": True,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "connected": bool(user.hh_connected),
            "has_resume": bool(user.hh_resume_id),
            "is_paid": user.is_paid,
        })


async def _search(request: web.Request) -> web.Response:
    tid = request["telegram_id"]
    q = request.query
    try:
        page = max(0, int(q.get("page", "0")))
    except ValueError:
        page = 0
    try:
        per_page = min(100, max(1, int(q.get("per_page", "20"))))
    except ValueError:
        per_page = 20

    params = _build_hh_params(q)
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == tid))).scalar_one_or_none()
        if not user:
            return web.json_response({"error": "user_not_found"}, status=404)
        client = await _client_for(user)
        if client is None:
            return web.json_response({"error": "hh_not_connected"}, status=409)
        items = await client.search(params, per_page=per_page, page=page)
        await _persist_token(session, user, client)
        if client.token_revoked:
            return web.json_response({"error": "hh_token_revoked"}, status=409)

    found = client.last_found or 0
    return web.json_response({
        "found": found,
        "page": page,
        "per_page": per_page,
        "items": [_vacancy_card(v) for v in items],
    })


def create_webapp_api() -> web.Application:
    app = web.Application(middlewares=[_auth_mw])
    app.router.add_get("/api/health", _health)
    app.router.add_get("/api/me", _me)
    app.router.add_get("/api/vacancies/search", _search)
    return app
