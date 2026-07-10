"""
Прогон автоотклика для одного пользователя (Фаза 3, мультиюзер).

Поиск идёт прямо по фильтрам пользователя (его настройки = параметры hh),
поэтому результаты уже релевантны — отдельный тяжёлый скоринг пока не нужен.
Отклик выполняется токеном пользователя, всё пишется с его user_id, с учётом
дневного лимита и окна расписания.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select, func

from app.database import async_session
from app.models.user import User
from app.models.vacancy import Vacancy, VacancyStatus
from app.models.application import Application, ApplicationStatus
from app.ai.claude import claude_ai
from app.parsers.hh_user_client import HHUserClient
from app.parsers.letter_template import render_letter
from app.utils.anti_detect import random_delay

log = structlog.get_logger()

_REFUSAL = ("не могу", "as an ai", "извините", "не имею возможности")


async def _build_letter(item: dict, title: str, st, resume_text: str) -> str:
    """Собрать письмо по настройкам режима:
      off      — без письма ("")
      required — письмо только если вакансия его требует
      always   — всегда; ИИ-персонализация при ai_enabled (иначе шаблон)."""
    mode = getattr(st, "letter_mode", "always")
    if mode == "off":
        return ""
    if mode == "required" and not item.get("response_letter_required"):
        return ""
    if st.ai_enabled and resume_text:
        snip = item.get("snippet") or {}
        desc = " ".join(x for x in (snip.get("responsibility"), snip.get("requirement")) if x)
        try:
            text, _, _ = await claude_ai.generate_cover_letter(
                title, desc, resume=resume_text, custom_prompt=(st.ai_custom_prompt or None)
            )
            text = (text or "").strip()
            low = text.lower()
            if text and len(text) >= 40 and not any(m in low for m in _REFUSAL):
                return text
        except Exception as e:
            log.warning("ai_letter_failed", error=str(e))
    return render_letter(title)


def _within_window(start: int, end: int) -> bool:
    hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
    return start <= hour < end


async def _sent_today(session, user_id: int) -> int:
    return (await session.execute(
        select(func.count(Application.id)).where(
            Application.user_id == user_id,
            Application.status == ApplicationStatus.SENT,
            func.date(Application.created_at) == func.current_date(),
        )
    )).scalar() or 0


async def run_user_cycle(user_id: int) -> int:
    """Один цикл автоотклика пользователя. Возвращает число новых откликов."""
    applied = 0
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user or not user.is_active or not user.hh_connected or not user.hh_access_token:
            return 0
        st = user.get_settings()
        resume_text = user.resume_text or ""
        # Без ключевых слов hh вернёт всё подряд — не откликаемся во избежание спама.
        if not (st.search_text or "").strip():
            log.info("user_no_keywords_skip", user_id=user.id)
            return 0
        if not _within_window(st.apply_hour_start, st.apply_hour_end):
            return 0
        remaining = st.daily_limit - await _sent_today(session, user.id)
        if remaining <= 0:
            return 0

        client = HHUserClient(
            access_token=user.hh_access_token,
            refresh_token=user.hh_refresh_token or "",
            resume_id=user.hh_resume_id,
            expires_at=user.hh_token_expires.timestamp() if user.hh_token_expires else 0.0,
        )
        items = await client.search(st.to_hh_params(), per_page=min(remaining + 10, 100))

    # Сохраняем обновлённый токен, если рефрешнулся
    if client.new_token:
        async with async_session() as session:
            u = await session.get(User, user_id)
            if u:
                u.hh_access_token = client.new_token["access_token"]
                u.hh_refresh_token = client.new_token["refresh_token"]
                from datetime import timezone
                u.hh_token_expires = datetime.fromtimestamp(client.new_token["expires_at"], tz=timezone.utc)
                await session.commit()

    for item in items:
        if applied >= remaining:
            break
        vid = str(item.get("id") or "")
        if not vid:
            continue
        title = item.get("name") or ""
        url = (item.get("alternate_url") or f"https://hh.ru/vacancy/{vid}")

        async with async_session() as session:
            # Дедуп: уже откликались этой вакансией?
            vac = (await session.execute(
                select(Vacancy).where(
                    Vacancy.user_id == user_id,
                    Vacancy.platform == "hh",
                    Vacancy.external_id == vid,
                )
            )).scalar_one_or_none()
            if vac and vac.status == VacancyStatus.APPLIED:
                continue
            if vac is None:
                vac = Vacancy(
                    user_id=user_id, platform="hh", external_id=vid,
                    url=url, title=title, status=VacancyStatus.NEW,
                )
                session.add(vac)
                await session.commit()
                await session.refresh(vac)
            vac_id = vac.id

        letter = await _build_letter(item, title, st, resume_text)
        try:
            result, info = await client.apply(vid, letter)
        except Exception as e:
            log.error("user_apply_error", user_id=user_id, vid=vid, error=str(e))
            continue

        if info.get("error") == "daily_limit":
            log.info("user_daily_limit_hit", user_id=user_id)
            break

        success = result is True
        already = result == "already"

        async with async_session() as session:
            if not already:
                session.add(Application(
                    user_id=user_id, vacancy_id=vac_id, platform="hh",
                    cover_letter=letter,
                    status=ApplicationStatus.SENT if success else ApplicationStatus.FAILED,
                    attempt_count=1,
                ))
            v = await session.get(Vacancy, vac_id)
            if v and (success or already):
                v.status = VacancyStatus.APPLIED
            await session.commit()

        if success:
            applied += 1
        await random_delay(st.apply_delay_min, st.apply_delay_max)

    log.info("user_cycle_done", user_id=user_id, applied=applied)
    return applied
