"""HTTP API для Telegram Mini App (поиск вакансий, профиль).

Отдельное aiohttp-приложение, поднимается рядом с ботом в том же процессе
(см. main.py). Все запросы, кроме /api/health, требуют валидную Telegram
initData — проверяется на сервере (webapp_auth). Поиск идёт под личным
OAuth-токеном пользователя (HHUserClient), поэтому чужие данные недоступны.

Наружу проксируется nginx'ом (hh.volnacrm.ru → 127.0.0.1:miniapp_port).
"""
from __future__ import annotations

import datetime as _dt
import json as _json

import structlog
from aiohttp import web
from sqlalchemy import select, delete

from app.config import settings
from app.database import async_session
from app.models.user import User
from app.models.favorite import Favorite
from app.models.saved_search import SavedSearch, SearchHistory
from app.parsers.hh_user_client import HHUserClient
from app.api.webapp_auth import telegram_id_from_init_data
from app.api import hh_dicts

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


def _vacancy_detail(v: dict) -> dict:
    """Полная карточка вакансии hh → нормализованный ответ для детальной страницы."""
    salary = v.get("salary") or {}
    emp = v.get("employer") or {}
    area = v.get("area") or {}
    addr = v.get("address") or {}
    return {
        "id": v.get("id"),
        "name": v.get("name"),
        "company": emp.get("name"),
        "company_logo": (emp.get("logo_urls") or {}).get("240") if emp.get("logo_urls") else None,
        "company_url": emp.get("alternate_url"),
        "area": area.get("name"),
        "address": addr.get("raw"),
        "salary_from": salary.get("from"),
        "salary_to": salary.get("to"),
        "currency": salary.get("currency"),
        "experience": (v.get("experience") or {}).get("name"),
        "schedule": (v.get("schedule") or {}).get("name"),
        "employment": (v.get("employment") or {}).get("name"),
        "description": v.get("description"),  # HTML
        "key_skills": [s.get("name") for s in (v.get("key_skills") or []) if s.get("name")],
        "published_at": v.get("published_at"),
        "url": v.get("alternate_url"),
    }


async def _vacancy(request: web.Request) -> web.Response:
    tid = request["telegram_id"]
    vid = request.match_info["vid"]
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == tid))).scalar_one_or_none()
        if not user:
            return web.json_response({"error": "user_not_found"}, status=404)
        client = await _client_for(user)
        if client is None:
            return web.json_response({"error": "hh_not_connected"}, status=409)
        data = await client.get_vacancy(vid)
        await _persist_token(session, user, client)
    if not data:
        if client.token_revoked:
            return web.json_response({"error": "hh_token_revoked"}, status=409)
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(_vacancy_detail(data))


async def _favorites_list(request: web.Request) -> web.Response:
    tid = request["telegram_id"]
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == tid))).scalar_one_or_none()
        if not user:
            return web.json_response({"items": []})
        rows = (await session.execute(
            select(Favorite).where(Favorite.user_id == user.id, Favorite.kind == "vacancy")
            .order_by(Favorite.created_at.desc()))).scalars().all()
    items = []
    for r in rows:
        try:
            items.append(_json.loads(r.snapshot_json))
        except Exception:
            items.append({"id": r.hh_id})
    return web.json_response({"items": items, "ids": [r.hh_id for r in rows]})


async def _favorite_add(request: web.Request) -> web.Response:
    tid = request["telegram_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    hh_id = str(body.get("id") or "").strip()
    if not hh_id:
        return web.json_response({"error": "no_id"}, status=400)
    snapshot = _json.dumps(body, ensure_ascii=False)[:8000]
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == tid))).scalar_one_or_none()
        if not user:
            return web.json_response({"error": "user_not_found"}, status=404)
        exists = (await session.execute(
            select(Favorite).where(Favorite.user_id == user.id,
                                   Favorite.kind == "vacancy", Favorite.hh_id == hh_id)
        )).scalar_one_or_none()
        if not exists:
            session.add(Favorite(user_id=user.id, kind="vacancy", hh_id=hh_id, snapshot_json=snapshot))
            await session.commit()
    return web.json_response({"ok": True, "id": hh_id})


async def _favorite_delete(request: web.Request) -> web.Response:
    tid = request["telegram_id"]
    hh_id = request.match_info["vid"]
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == tid))).scalar_one_or_none()
        if user:
            await session.execute(delete(Favorite).where(
                Favorite.user_id == user.id, Favorite.kind == "vacancy", Favorite.hh_id == hh_id))
            await session.commit()
    return web.json_response({"ok": True, "id": hh_id})


HISTORY_LIMIT = 40


async def _user_by_tid(session, tid: int) -> User | None:
    return (await session.execute(select(User).where(User.telegram_id == tid))).scalar_one_or_none()


async def _saved_list(request: web.Request) -> web.Response:
    async with async_session() as session:
        user = await _user_by_tid(session, request["telegram_id"])
        if not user:
            return web.json_response({"items": []})
        rows = (await session.execute(
            select(SavedSearch).where(SavedSearch.user_id == user.id)
            .order_by(SavedSearch.created_at.desc()))).scalars().all()
    return web.json_response({"items": [
        {"id": r.id, "name": r.name, "filters": _safe_json(r.filters_json)} for r in rows
    ]})


async def _saved_add(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    name = (str(body.get("name") or "Поиск")).strip()[:120]
    filters = _json.dumps(body.get("filters") or {}, ensure_ascii=False)[:8000]
    async with async_session() as session:
        user = await _user_by_tid(session, request["telegram_id"])
        if not user:
            return web.json_response({"error": "user_not_found"}, status=404)
        row = SavedSearch(user_id=user.id, name=name, filters_json=filters)
        session.add(row)
        await session.commit()
        return web.json_response({"id": row.id, "name": name})


async def _saved_delete(request: web.Request) -> web.Response:
    sid = int(request.match_info["sid"]) if request.match_info["sid"].isdigit() else -1
    async with async_session() as session:
        user = await _user_by_tid(session, request["telegram_id"])
        if user:
            await session.execute(delete(SavedSearch).where(
                SavedSearch.id == sid, SavedSearch.user_id == user.id))
            await session.commit()
    return web.json_response({"ok": True})


async def _history_list(request: web.Request) -> web.Response:
    async with async_session() as session:
        user = await _user_by_tid(session, request["telegram_id"])
        if not user:
            return web.json_response({"items": []})
        rows = (await session.execute(
            select(SearchHistory).where(SearchHistory.user_id == user.id)
            .order_by(SearchHistory.created_at.desc()).limit(HISTORY_LIMIT))).scalars().all()
    return web.json_response({"items": [
        {"id": r.id, "text": r.query_text, "filters": _safe_json(r.filters_json),
         "found": r.results_count} for r in rows
    ]})


async def _history_add(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    text = (str(body.get("text") or "")).strip()[:255]
    filters = _json.dumps(body.get("filters") or {}, ensure_ascii=False)[:8000]
    found = int(body.get("found") or 0)
    async with async_session() as session:
        user = await _user_by_tid(session, request["telegram_id"])
        if not user:
            return web.json_response({"error": "user_not_found"}, status=404)
        # Дедуп: если последняя запись совпадает по тексту+фильтрам — не плодим.
        last = (await session.execute(
            select(SearchHistory).where(SearchHistory.user_id == user.id)
            .order_by(SearchHistory.created_at.desc()).limit(1))).scalar_one_or_none()
        if not (last and last.query_text == text and last.filters_json == filters):
            session.add(SearchHistory(user_id=user.id, query_text=text,
                                      filters_json=filters, results_count=found))
            # Чистим хвост сверх лимита.
            old = (await session.execute(
                select(SearchHistory.id).where(SearchHistory.user_id == user.id)
                .order_by(SearchHistory.created_at.desc()).offset(HISTORY_LIMIT))).scalars().all()
            if old:
                await session.execute(delete(SearchHistory).where(SearchHistory.id.in_(old)))
            await session.commit()
    return web.json_response({"ok": True})


async def _history_clear(request: web.Request) -> web.Response:
    async with async_session() as session:
        user = await _user_by_tid(session, request["telegram_id"])
        if user:
            await session.execute(delete(SearchHistory).where(SearchHistory.user_id == user.id))
            await session.commit()
    return web.json_response({"ok": True})


def _safe_json(s: str) -> dict:
    try:
        return _json.loads(s)
    except Exception:
        return {}


def _resume_view(r: dict) -> dict:
    """Резюме hh → нормализованный ответ для экрана «Моё резюме»."""
    salary = r.get("salary") or {}
    area = r.get("area") or {}
    total = r.get("total_experience") or {}
    experience = []
    for e in r.get("experience") or []:
        experience.append({
            "company": e.get("company"),
            "position": e.get("position"),
            "start": e.get("start"),
            "end": e.get("end"),
            "description": e.get("description"),
        })
    return {
        "title": r.get("title"),
        "first_name": r.get("first_name"),
        "last_name": r.get("last_name"),
        "area": area.get("name"),
        "salary_amount": salary.get("amount"),
        "salary_currency": salary.get("currency"),
        "total_months": total.get("months"),
        "skills": r.get("skill_set") or [],
        "experience": experience,
        "education_level": (r.get("education") or {}).get("level", {}).get("name")
        if isinstance(r.get("education"), dict) else None,
        "updated_at": r.get("updated_at"),
        "url": r.get("alternate_url"),
    }


async def _resume(request: web.Request) -> web.Response:
    async with async_session() as session:
        user = await _user_by_tid(session, request["telegram_id"])
        if not user:
            return web.json_response({"error": "user_not_found"}, status=404)
        client = await _client_for(user)
        if client is None:
            return web.json_response({"error": "hh_not_connected"}, status=409)
        if not user.hh_resume_id:
            return web.json_response({"error": "no_resume"}, status=404)
        data = await client.get_resume()
        await _persist_token(session, user, client)
    if not data:
        if client.token_revoked:
            return web.json_response({"error": "hh_token_revoked"}, status=409)
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(_resume_view(data))


async def _resume_bump(request: web.Request) -> web.Response:
    async with async_session() as session:
        user = await _user_by_tid(session, request["telegram_id"])
        if not user:
            return web.json_response({"error": "user_not_found"}, status=404)
        client = await _client_for(user)
        if client is None:
            return web.json_response({"error": "hh_not_connected"}, status=409)
        ok = await client.bump_resume()
        await _persist_token(session, user, client)
    return web.json_response({"ok": ok})


async def _dictionaries(_request: web.Request) -> web.Response:
    return web.json_response(await hh_dicts.get_dictionaries())


async def _areas_suggest(request: web.Request) -> web.Response:
    text = request.query.get("text", "")
    return web.json_response({"items": await hh_dicts.suggest_areas(text)})


def create_webapp_api() -> web.Application:
    app = web.Application(middlewares=[_auth_mw])
    app.router.add_get("/api/health", _health)
    app.router.add_get("/api/me", _me)
    app.router.add_get("/api/vacancies/search", _search)
    app.router.add_get("/api/vacancies/{vid}", _vacancy)
    app.router.add_get("/api/dictionaries", _dictionaries)
    app.router.add_get("/api/areas/suggest", _areas_suggest)
    app.router.add_get("/api/favorites", _favorites_list)
    app.router.add_post("/api/favorites", _favorite_add)
    app.router.add_delete("/api/favorites/{vid}", _favorite_delete)
    app.router.add_get("/api/saved-searches", _saved_list)
    app.router.add_post("/api/saved-searches", _saved_add)
    app.router.add_delete("/api/saved-searches/{sid}", _saved_delete)
    app.router.add_get("/api/history", _history_list)
    app.router.add_post("/api/history", _history_add)
    app.router.add_delete("/api/history", _history_clear)
    app.router.add_get("/api/resume", _resume)
    app.router.add_post("/api/resume/bump", _resume_bump)
    return app
