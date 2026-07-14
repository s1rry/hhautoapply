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

# Потолок ИИ-оценок за один цикл на пользователя — защита от лишних трат/латентности
# при широком поиске (умный отбор делает отдельный ИИ-запрос на каждую вакансию).
MAX_SCORINGS_PER_CYCLE = 40
# До скольких страниц выдачи (по 50) заходить за цикл, чтобы добраться до
# ещё не отработанных вакансий, когда топ уже весь обработан.
MAX_SEARCH_PAGES = 10


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
    contact = (getattr(st, "contact", "") or "").strip()
    if st.ai_enabled and resume_text:
        snip = item.get("snippet") or {}
        desc = " ".join(x for x in (snip.get("responsibility"), snip.get("requirement")) if x)
        company = (item.get("employer") or {}).get("name") or ""
        try:
            text, _, _ = await claude_ai.generate_cover_letter(
                title, desc, company_name=company, resume=resume_text,
                custom_prompt=(st.ai_custom_prompt or None)
            )
            text = (text or "").strip()
            low = text.lower()
            if text and len(text) >= 40 and not any(m in low for m in _REFUSAL):
                if contact:
                    text += f"\n\nКонтакты: {contact}"
                return text
        except Exception as e:
            log.warning("ai_letter_failed", error=str(e))
    # Без ИИ: своё готовое письмо пользователя, иначе нейтральный шаблон.
    custom = (getattr(st, "custom_letter", "") or "").strip()
    text = render_letter(title, template=(custom or None), contact=(contact or None))
    if custom and contact and contact not in text:
        text += f"\n\nКонтакты: {contact}"
    return text


def _within_window(start: int, end: int) -> bool:
    hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
    return start <= hour < end


async def _sent_today(session, user_id: int, account_ref: str, task_id: int | None = None) -> int:
    conds = [
        Application.user_id == user_id,
        Application.account_ref == account_ref,
        Application.status == ApplicationStatus.SENT,
        func.date(Application.created_at) == func.current_date(),
    ]
    if task_id is not None:
        conds.append(Application.search_task_id == task_id)
    return (await session.execute(
        select(func.count(Application.id)).where(*conds)
    )).scalar() or 0


async def _mark_skip(vac_id: int, reason: str) -> None:
    """Пометить вакансию как пропущенную (для статистики) и убрать из будущих
    прогонов (REJECTED → дедуп её больше не берёт)."""
    async with async_session() as session:
        v = await session.get(Vacancy, vac_id)
        if v:
            v.skip_reason = reason
            v.status = VacancyStatus.REJECTED
            await session.commit()


async def _account_contexts(session, user) -> list[dict]:
    """Список аккаунтов для прогона: основной (User.hh_*) + активные доп."""
    from app.models.hh_account import HHAccount
    ctxs: list[dict] = []
    if user.hh_connected and user.hh_access_token:
        import json
        cookies = None
        if user.hh_cookies:
            try:
                cookies = json.loads(user.hh_cookies)
            except Exception:
                cookies = None
        ctxs.append({
            "ref": f"u{user.id}", "kind": "primary", "id": None,
            "access_token": user.hh_access_token,
            "refresh_token": user.hh_refresh_token or "",
            "resume_id": user.hh_resume_id,
            "expires_at": user.hh_token_expires.timestamp() if user.hh_token_expires else 0.0,
            "resume_text": user.resume_text or "",
            "cookies": cookies,
        })
    accs = (await session.execute(
        select(HHAccount).where(HHAccount.user_id == user.id, HHAccount.is_active.is_(True))
    )).scalars().all()
    for a in accs:
        if not a.hh_access_token:
            continue
        ctxs.append({
            "ref": f"a{a.id}", "kind": "extra", "id": a.id,
            "access_token": a.hh_access_token, "refresh_token": a.hh_refresh_token or "",
            "resume_id": a.hh_resume_id,
            "expires_at": a.hh_token_expires.timestamp() if a.hh_token_expires else 0.0,
            "resume_text": a.resume_text or "",
        })
    return ctxs


async def _save_refreshed_token(ctx: dict, new_token: dict) -> None:
    from datetime import timezone
    from app.models.hh_account import HHAccount
    expires = datetime.fromtimestamp(new_token["expires_at"], tz=timezone.utc)
    async with async_session() as session:
        if ctx["kind"] == "primary":
            u = await session.get(User, ctx["user_id"])
            if u:
                u.hh_access_token = new_token["access_token"]
                u.hh_refresh_token = new_token["refresh_token"]
                u.hh_token_expires = expires
                await session.commit()
        else:
            a = await session.get(HHAccount, ctx["id"])
            if a:
                a.hh_access_token = new_token["access_token"]
                a.hh_refresh_token = new_token["refresh_token"]
                a.hh_token_expires = expires
                await session.commit()


async def run_user_cycle(user_id: int) -> int:
    """Цикл автоотклика пользователя по всем его аккаунтам. Число новых откликов."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user or not user.is_active:
            return 0
        st_global = user.get_settings()
        # Задачи из активных SearchTask (ключ + своё резюме + свои настройки).
        # Fallback — старые ключевые фразы на общих настройках пользователя.
        from app.services.search_tasks import ensure_seeded, active_tasks
        await ensure_seeded(session, user)
        task_objs = await active_tasks(session, user.id)
        if task_objs:
            tasks = [{"keyword": t.keyword, "resume_id": t.resume_id,
                      "resume_text": t.resume_text, "task_id": t.id,
                      "settings": (t.get_settings() if t.settings_json else st_global)}
                     for t in task_objs]
        else:
            tasks = [{"keyword": p, "resume_id": None, "resume_text": None,
                      "task_id": None, "settings": st_global}
                     for p in st_global.search_phrases()]
        # Без ключевых слов hh вернёт всё подряд — не откликаемся во избежание спама.
        if not tasks:
            log.info("user_no_keywords_skip", user_id=user.id)
            return 0
        contexts = await _account_contexts(session, user)

    total = 0
    for ctx in contexts:
        ctx["user_id"] = user_id
        try:
            total += await run_account_cycle(user_id, ctx, tasks)
        except Exception as e:
            log.error("account_cycle_error", user_id=user_id, ref=ctx["ref"], error=str(e))
    log.info("user_cycle_done", user_id=user_id, applied=total, accounts=len(contexts))
    return total


async def run_account_cycle(user_id: int, ctx: dict, tasks: list[dict]) -> int:
    """Один цикл автоотклика для одного hh-аккаунта.

    Каждая задача (tasks[i]) идёт со своими настройками (settings), ключом,
    резюме и дневным лимитом — свой поиск, свой отбор, своё окно расписания.
    """
    ref = ctx["ref"]
    account_resume_id = ctx["resume_id"]
    account_resume_text = ctx["resume_text"]
    total_applied = 0

    client = HHUserClient(
        access_token=ctx["access_token"], refresh_token=ctx["refresh_token"],
        resume_id=ctx["resume_id"], expires_at=ctx["expires_at"],
    )
    scored = 0  # бюджет ИИ-оценок на аккаунт (общий на все задачи)

    for task in tasks:
        st = task["settings"]
        task_id = task.get("task_id")
        # Окно расписания — своё у задачи.
        if not _within_window(st.apply_hour_start, st.apply_hour_end):
            continue
        async with async_session() as session:
            remaining = st.daily_limit - await _sent_today(session, user_id, ref, task_id)
        if remaining <= 0:
            continue
        phrase = task.get("keyword") or ""
        resume_id = task.get("resume_id") or account_resume_id
        resume_text = task.get("resume_text") or account_resume_text
        client.resume_id = resume_id  # переключаем резюме под задачу
        base_params = st.to_hh_params()
        excluded = st.excluded_words()
        applied = 0
        seen = 0
        stop = False
        # Источник вакансий: ключ задачи и/или лента рекомендаций hh под резюме.
        src_mode = getattr(st, "vacancy_source", "keyword")
        sources: list[str] = []
        if src_mode in ("keyword", "both") and phrase:
            sources.append("keyword")
        if src_mode in ("recommended", "both") and resume_id:
            sources.append("recommended")
        if not sources:  # нет ключа или нет резюме — берём что доступно
            sources = ["keyword"] if phrase else (["recommended"] if resume_id else [])
        exhausted: set[str] = set()
        for source, page in [(s, p) for s in sources for p in range(MAX_SEARCH_PAGES)]:
            if stop or applied >= remaining:
                break
            if source in exhausted:
                continue
            if source == "keyword":
                params = dict(base_params)
                params["text"] = phrase
                items = await client.search(params, per_page=50, page=page)
            else:
                items = await client.similar_vacancies(resume_id, per_page=50, page=page)
            if client.new_token:
                await _save_refreshed_token(ctx, client.new_token)
            if not items:
                exhausted.add(source)  # лента кончилась — не долбим пустые страницы
                continue
            for item in items:
                if applied >= remaining:
                    break
                seen += 1
                vid = str(item.get("id") or "")
                if not vid:
                    continue
                title = item.get("name") or ""
                url = (item.get("alternate_url") or f"https://hh.ru/vacancy/{vid}")

                # hh сам помечает вакансии, где уже был отклик/отказ (relations) —
                # не тратим на них отклик (особенно важно для ленты рекомендаций).
                if item.get("relations"):
                    continue

                # Слова-исключения: отсеиваем по названию + сниппету (на своей стороне).
                if excluded:
                    snip = item.get("snippet") or {}
                    blob = " ".join(str(x) for x in (
                        title, snip.get("responsibility"), snip.get("requirement")) if x).lower()
                    if any(w in blob for w in excluded):
                        continue

                async with async_session() as session:
                    # Дедуп в рамках этого аккаунта.
                    vac = (await session.execute(
                        select(Vacancy).where(
                            Vacancy.user_id == user_id, Vacancy.platform == "hh",
                            Vacancy.external_id == vid, Vacancy.account_ref == ref,
                        )
                    )).scalar_one_or_none()
                    if vac and vac.status in (VacancyStatus.APPLIED, VacancyStatus.REJECTED):
                        continue
                    if vac is None:
                        vac = Vacancy(
                            user_id=user_id, platform="hh", external_id=vid, account_ref=ref,
                            url=url, title=title, status=VacancyStatus.NEW,
                            search_task_id=task_id,
                        )
                        session.add(vac)
                        await session.commit()
                        await session.refresh(vac)
                    vac_id = vac.id

                # Умный отбор: ИИ оценивает соответствие вакансии резюме и отсекает слабые.
                if getattr(st, "ai_score_enabled", False) and resume_text:
                    if scored >= MAX_SCORINGS_PER_CYCLE:
                        log.info("user_score_budget_reached", user_id=user_id, ref=ref, scored=scored)
                        stop = True
                        break
                    snip = item.get("snippet") or {}
                    desc = " ".join(x for x in (snip.get("responsibility"), snip.get("requirement")) if x)
                    scored += 1
                    score = await claude_ai.score_vacancy(title, desc, resume_text)
                    # Fail-open: если ИИ не смог оценить (None) — НЕ блокируем отклик,
                    # иначе сбой/формат ответа ИИ останавливает всё. Режем только при
                    # реальной оценке ниже порога.
                    if score is not None:
                        async with async_session() as session:
                            v = await session.get(Vacancy, vac_id)
                            if v:
                                v.ai_score = float(score)
                                if score < st.ai_score_min:
                                    v.status = VacancyStatus.REJECTED
                                    v.skip_reason = "ai_low"
                                    v.ai_reason = f"Умный отбор: {score}% < порога {st.ai_score_min}%"
                                await session.commit()
                        if score < st.ai_score_min:
                            log.info("user_vacancy_skipped_low_score", user_id=user_id, vid=vid, score=score)
                            continue
                    else:
                        log.warning("user_score_none_apply_anyway", user_id=user_id, vid=vid)

                letter = await _build_letter(item, title, st, resume_text)
                try:
                    if item.get("has_test"):
                        # Вакансия с тестом: обычный API-отклик не пройдёт.
                        cookies_state = ctx.get("cookies")
                        if cookies_state and st.ai_enabled and resume_text:
                            result, info = await client.apply_with_test(vid, letter, cookies_state)
                            if result is not True:
                                log.info("test_apply_skip", user_id=user_id, vid=vid, info=info)
                                await _mark_skip(vac_id, "needs_test")
                                continue
                        else:
                            # Нет веб-сессии/ИИ — не тратим на тест-вакансию.
                            await _mark_skip(vac_id, "needs_test")
                            continue
                    else:
                        result, info = await client.apply(vid, letter)
                except Exception as e:
                    log.error("user_apply_error", user_id=user_id, vid=vid, error=str(e))
                    continue

                if info.get("error") == "daily_limit":
                    log.info("user_daily_limit_hit", user_id=user_id, ref=ref)
                    stop = True
                    break

                success = result is True
                already = result == "already"

                async with async_session() as session:
                    if not already:
                        session.add(Application(
                            user_id=user_id, vacancy_id=vac_id, platform="hh",
                            cover_letter=letter, account_ref=ref, search_task_id=task_id,
                            status=ApplicationStatus.SENT if success else ApplicationStatus.FAILED,
                            attempt_count=1,
                        ))
                    v = await session.get(Vacancy, vac_id)
                    if v and (success or already):
                        v.status = VacancyStatus.APPLIED
                        if already:
                            v.skip_reason = "already"
                    await session.commit()

                if success:
                    applied += 1
                await random_delay(st.apply_delay_min, st.apply_delay_max)
            if stop:
                break
        log.info("account_task_done", user_id=user_id, ref=ref, task_id=task_id,
                 phrase=phrase, applied=applied, seen=seen)
        total_applied += applied
        if scored >= MAX_SCORINGS_PER_CYCLE:
            break  # бюджет ИИ на аккаунт исчерпан — дальше задачи не крутим

    log.info("account_cycle_done", user_id=user_id, ref=ref, applied=total_applied)
    return total_applied
