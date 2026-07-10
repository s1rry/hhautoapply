"""
Получение резюме пользователя из hh по OAuth-токену.

Используется после OTP-входа в мультиюзере: тянем первый резюме пользователя,
берём его id (нужен для откликов) и собираем текст (для ИИ-писем и скоринга).
"""
from __future__ import annotations

import httpx
import structlog

from app.parsers.hh_oauth import UA

log = structlog.get_logger()

API = "https://api.hh.ru"


def _resume_to_text(data: dict) -> str:
    """Собрать читаемый текст из структуры резюме hh."""
    parts: list[str] = []
    title = data.get("title")
    if title:
        parts.append(f"Должность: {title}")
    area = (data.get("area") or {}).get("name")
    if area:
        parts.append(f"Регион: {area}")
    salary = data.get("salary") or {}
    if salary.get("amount"):
        parts.append(f"Зарплата: {salary['amount']} {salary.get('currency', '')}")
    skills = data.get("skill_set") or []
    if skills:
        parts.append("Навыки: " + ", ".join(skills))
    if data.get("skills"):
        parts.append(data["skills"])
    for exp in (data.get("experience") or [])[:6]:
        company = exp.get("company", "")
        position = exp.get("position", "")
        desc = (exp.get("description") or "").strip()
        line = f"Опыт: {position} в {company}".strip()
        parts.append(line)
        if desc:
            parts.append(desc)
    return "\n".join(p for p in parts if p).strip()


async def fetch_resume(access_token: str) -> tuple[str | None, str | None, str | None]:
    """
    Вернуть (resume_id, resume_text, title) первого резюме пользователя.
    title — желаемая должность (для ключевых слов по умолчанию).
    При ошибке — (None, None, None).
    """
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": UA}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{API}/resumes/mine", headers=headers)
            if r.status_code != 200:
                log.warning("resume_mine_failed", status=r.status_code, body=r.text[:200])
                return None, None, None
            items = (r.json() or {}).get("items") or []
            if not items:
                return None, None, None
            resume_id = items[0].get("id")
            title = items[0].get("title")
            if not resume_id:
                return None, None, None
            rr = await c.get(f"{API}/resumes/{resume_id}", headers=headers)
            data = rr.json() if rr.status_code == 200 else {}
            text = _resume_to_text(data) if data else None
            title = data.get("title") or title
            return resume_id, text, title
    except Exception as e:
        log.error("fetch_resume_error", error=str(e))
        return None, None, None
