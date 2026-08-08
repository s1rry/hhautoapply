"""Чистые преобразования для Mini App API: фильтры и нормализация карточек.

Вынесено из webapp_api, чтобы: (1) не тянуть в тестах тяжёлую скрапинг-стопку
(HHUserClient → app.parsers → bs4/aiolimiter/…), (2) отделить логику от aiohttp.
Здесь только stdlib и словари/строки — легко тестировать.
"""
from __future__ import annotations

# Параметры поиска hh, которые принимаем как повторяющиеся (мультивыбор).
MULTI = (
    "area", "metro", "experience", "employment", "schedule", "work_format",
    "professional_role", "industry", "education", "label", "search_field",
    "driver_license_types",
)
# Одиночные скалярные параметры hh.
SCALAR = ("text", "salary", "currency", "only_with_salary", "employer_id",
          "period", "date_from", "order_by")

ALLOWED_ORDER = {"relevance", "publication_time", "salary_desc", "salary_asc"}


def build_hh_params(query) -> dict:
    """Собрать безопасный dict параметров hh из query-строки (whitelist-ключи).

    query — MultiDict-подобный объект (aiohttp request.query): .getall / .get.
    hh валидирует коды сам, неизвестные молча игнорирует. order_by — по белому
    списку, чтобы не прокидывать произвольные значения.
    """
    params: dict = {}
    for key in MULTI:
        vals = [v for v in query.getall(key, []) if v]
        if vals:
            params[key] = vals
    for key in SCALAR:
        v = query.get(key)
        if v:
            params[key] = v
    order = query.get("order_by")
    if order in ALLOWED_ORDER:
        params["order_by"] = order
    elif "order_by" in params:
        params.pop("order_by")
    return params


def vacancy_card(v: dict) -> dict:
    """Вакансия hh → карточка для списка (без лишнего веса)."""
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


def vacancy_detail(v: dict) -> dict:
    """Полная карточка вакансии hh → ответ для детальной страницы."""
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
        "description": v.get("description"),
        "key_skills": [s.get("name") for s in (v.get("key_skills") or []) if s.get("name")],
        "published_at": v.get("published_at"),
        "url": v.get("alternate_url"),
    }


def resume_view(r: dict) -> dict:
    """Резюме hh → нормализованный ответ для экрана «Моё резюме»."""
    salary = r.get("salary") or {}
    area = r.get("area") or {}
    total = r.get("total_experience") or {}
    experience = [{
        "company": e.get("company"),
        "position": e.get("position"),
        "start": e.get("start"),
        "end": e.get("end"),
        "description": e.get("description"),
    } for e in (r.get("experience") or [])]
    edu = r.get("education")
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
        "education_level": (edu.get("level", {}) or {}).get("name") if isinstance(edu, dict) else None,
        "updated_at": r.get("updated_at"),
        "url": r.get("alternate_url"),
    }
