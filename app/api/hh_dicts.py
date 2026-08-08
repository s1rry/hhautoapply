"""Справочники hh для фильтров Mini App (публичные, без токена).

Тянем с api.hh.ru и кэшируем в памяти на несколько часов — коды меняются
редко, а дёргать hh на каждый показ фильтров незачем. Отдаём только то, что
реально принимает поиск /vacancies, чтобы не рисовать неработающие фильтры.
"""
from __future__ import annotations

import time

import httpx
import structlog

log = structlog.get_logger()

_UA = "hh-miniapp/0.1 (+telegram)"
_TTL = 6 * 3600
_cache: dict[str, tuple[float, object]] = {}


async def _fetch(url: str) -> object | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers={"User-Agent": _UA})
        if r.status_code == 200:
            return r.json()
        log.warning("hh_dict_failed", url=url, status=r.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("hh_dict_error", url=url, error=str(e))
    return None


async def _cached(key: str, url: str) -> object | None:
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    data = await _fetch(url)
    if data is not None:
        _cache[key] = (time.time(), data)
        return data
    return hit[1] if hit else None  # отдаём протухшее, если свежее не пришло


def _flatten_roles(categories: list) -> list[dict]:
    """Профроли hh: категории с вложенными ролями → плоский список с группой."""
    out: list[dict] = []
    for cat in categories or []:
        cat_name = cat.get("name")
        for role in cat.get("roles", []):
            out.append({"id": role.get("id"), "name": role.get("name"), "group": cat_name})
    return out


def _flatten_industries(items: list) -> list[dict]:
    """Отрасли hh: дерево → плоский список (родитель + подотрасли)."""
    out: list[dict] = []
    for top in items or []:
        out.append({"id": top.get("id"), "name": top.get("name"), "group": top.get("name")})
        for sub in top.get("industries", []):
            out.append({"id": sub.get("id"), "name": sub.get("name"), "group": top.get("name")})
    return out


async def get_dictionaries() -> dict:
    """Единый ответ со всеми справочниками для панели фильтров."""
    base = await _cached("dictionaries", "https://api.hh.ru/dictionaries") or {}
    roles = await _cached("roles", "https://api.hh.ru/professional_roles") or {}
    industries = await _cached("industries", "https://api.hh.ru/industries") or []

    def simple(key: str) -> list[dict]:
        return [{"id": x.get("id"), "name": x.get("name")} for x in (base.get(key) or [])]

    return {
        "experience": simple("experience"),
        "employment": simple("employment"),
        "schedule": simple("schedule"),
        "order_by": simple("vacancy_search_order"),
        # Формат работы — новое поле hh; коды стабильны, справочник в /dictionaries
        # не всегда есть, поэтому фиксируем известные.
        "work_format": [
            {"id": "ON_SITE", "name": "На месте работодателя"},
            {"id": "REMOTE", "name": "Удалённо"},
            {"id": "HYBRID", "name": "Гибрид"},
            {"id": "FIELD_WORK", "name": "Разъездной"},
        ],
        "education": [
            {"id": "not_required_or_not_specified", "name": "Не требуется или не указано"},
            {"id": "higher", "name": "Высшее"},
            {"id": "special_secondary", "name": "Среднее профессиональное"},
        ],
        "professional_role": _flatten_roles(roles.get("categories") if isinstance(roles, dict) else []),
        "industry": _flatten_industries(industries if isinstance(industries, list) else []),
    }


async def suggest_areas(text: str) -> list[dict]:
    """Автокомплит региона/города через публичный suggests/areas hh."""
    if not text.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.hh.ru/suggests/areas",
                            params={"text": text}, headers={"User-Agent": _UA})
        if r.status_code == 200:
            items = (r.json() or {}).get("items") or []
            return [{"id": i.get("id"), "name": i.get("text")} for i in items if i.get("id")]
    except Exception as e:  # noqa: BLE001
        log.warning("suggest_areas_error", error=str(e))
    return []
