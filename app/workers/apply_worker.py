import asyncio
import re
import structlog
from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.models.vacancy import Vacancy, VacancyStatus
from app.models.application import Application, ApplicationStatus
from app.models.ai_generation import AIGeneration
from app.ai.claude import claude_ai
from app.parsers.hh import HHParser
from app.utils.anti_detect import random_delay

log = structlog.get_logger()


async def sync_applied_from_hh() -> int:
    """Fetch list of vacancies already applied to on hh.ru via API and
    mark them as APPLIED in DB so the bot doesn't waste time re-applying."""
    from app.parsers.hh_api import hh_api_client
    try:
        ids = await asyncio.wait_for(hh_api_client.fetch_applied_vacancy_ids(), timeout=30)
    except asyncio.TimeoutError:
        log.warning("sync_applied_timeout")
        return 0
    if not ids:
        return 0
    marked = 0
    async with async_session() as session:
        result = await session.execute(
            select(Vacancy).where(
                Vacancy.platform == "hh",
                Vacancy.external_id.in_(ids),
                Vacancy.status != VacancyStatus.APPLIED,
            )
        )
        for v in result.scalars().all():
            v.status = VacancyStatus.APPLIED
            marked += 1
        if marked:
            await session.commit()
    log.info("sync_applied_complete", marked=marked, fetched=len(ids))
    return marked


async def run_auto_apply(auto_mode: bool = False, min_score: float = 70):
    log.info("auto_apply_started", auto_mode=auto_mode, min_score=min_score)

    # Платформы на паузе (из scheduler_state.json) — для них пропускаем
    # выборку. Объединяем auto-pause (login_health) и manual-pause (юзер).
    paused_platforms: set[str] = set()
    try:
        import json as _json
        from pathlib import Path as _Path
        _sf = _Path("data/scheduler_state.json")
        if _sf.exists():
            _st = _json.loads(_sf.read_text())
            paused_platforms = set(_st.get("paused_platforms", []))
            paused_platforms |= set(_st.get("manual_paused_platforms", []))
    except Exception as e:
        log.warning("read_paused_platforms_error", error=str(e))

    # Pre-sync from HH negotiations to skip already-applied vacancies (только если hh не на паузе)
    if "hh" not in paused_platforms:
        try:
            await sync_applied_from_hh()
        except Exception as e:
            log.warning("sync_applied_skip", error=str(e))
    applied = 0

    # Тиринг день/ночь (МСК):
    #  День 9–22 — откликаемся на ВСЁ реальное (score>=1), от высокого score
    #  к низкому (ORDER BY score DESC ниже задаёт порядок тиров
    #  100→80→60→40→30→20→15→…), чтобы добить дневной лимит до 200.
    #  Ночь 22–9 — только высокоценные (score>=50): на них реагируем сразу,
    #  остальное ждёт утра.
    #  score=0 (дисквалифицированные 1С/junior/qa/не-аналитик) не берём никогда.
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    _hour = _dt.now(_ZI("Europe/Moscow")).hour
    _is_daytime = 9 <= _hour < 22
    effective_min_score = 1 if _is_daytime else 50
    log.info("apply_window", daytime=_is_daytime, min_score=effective_min_score)

    # Per-platform daily limits
    platform_caps = {
        "hh": settings.max_applies_per_day_hh,
        "habr": settings.max_applies_per_day_habr,
    }
    # Платформы на паузе исключаем целиком
    for p in list(platform_caps.keys()):
        if p in paused_platforms:
            log.info("apply_skip_paused_platform", platform=p)
            del platform_caps[p]
    async with async_session() as session:
        today_rows = (await session.execute(
            select(Application.platform, func.count(Application.id))
            .where(
                Application.status == ApplicationStatus.SENT,
                func.date(Application.created_at) == func.current_date(),
            )
            .group_by(Application.platform)
        )).all()
        today_by_plat = {p: c for p, c in today_rows}

        remaining_by_plat: dict[str, int] = {}
        for plat, cap in platform_caps.items():
            done = today_by_plat.get(plat, 0)
            left = max(0, cap - done)
            if left > 0:
                remaining_by_plat[plat] = left

        if not remaining_by_plat:
            log.info("daily_limit_reached", today=today_by_plat)
            return 0

        # Берём одобренные вакансии по платформам с лимитом per-platform
        # Исключаем те, что уже падали 3+ раз (бессмысленно ретраить)
        from sqlalchemy import select as _select
        failed_3plus = _select(Application.vacancy_id).where(
            Application.status == ApplicationStatus.FAILED,
        ).group_by(Application.vacancy_id).having(func.count(Application.id) >= 3)

        all_vacs = []
        for plat, limit in remaining_by_plat.items():
            result = await session.execute(
                select(Vacancy)
                .where(
                    Vacancy.platform == plat,
                    Vacancy.status == VacancyStatus.APPROVED,
                    Vacancy.ai_score >= effective_min_score,
                    Vacancy.id.notin_(failed_3plus),
                )
                .order_by(Vacancy.ai_score.desc())
                .limit(limit)
            )
            all_vacs.extend(result.scalars().all())
        # Mix platforms a bit: interleave
        vacancies = all_vacs

    # Static cover letter — used for normal applies (no questions, no required letter).
    # Saves ~2-3k AI tokens per apply. AI letters used only when Playwright fallback
    # kicks in for vacancies with questionnaires.
    STATIC_LETTER = (
        "Заинтересовала ваша вакансия. Имею коммерческий опыт в роли "
        "системного и бизнес-аналитика: сбор и анализ требований, "
        "BPMN / UML, проектирование REST API и интеграций, SQL, постановка "
        "задач разработчикам, приёмка результатов. Готов обсудить детали "
        "и пройти интервью.\n\n"
        "Контакты: i.egorov8080@gmail.com, tg https://t.me/egorov_analyst"
    )

    # Глобальные ошибки (daily_limit, истёкший токен, нет резюме) одинаково
    # бьют по всем вакансиям платформы — нет смысла ретраить и засорять БД
    # фейлами. Прерываем платформу до следующего запуска.
    aborted_platforms: set[str] = set()
    GLOBAL_ERRORS = {"daily_limit", "auth_required", "auth_expired", "no_oauth_token", "no_resume_id"}

    for vacancy in vacancies:
        if vacancy.platform in aborted_platforms:
            continue
        try:
            letter = STATIC_LETTER

            # HH через OAuth API (быстро, обходит DDoS Guard)
            result = False
            skip_record = False  # True для глобальных ошибок — не пишем FAILED
            if vacancy.platform == "hh":
                from app.parsers.hh_oauth import hh_oauth
                m_id = re.search(r"/vacancy/(\d+)", vacancy.url)
                vid = m_id.group(1) if m_id else vacancy.external_id
                try:
                    res, info = await asyncio.wait_for(
                        hh_oauth.apply(vid, letter),
                        timeout=20,
                    )
                    result = res
                    if res is not True and res != "already":
                        err = ((info or {}).get("error", "") or "").lower()
                        if err in GLOBAL_ERRORS:
                            log.warning(
                                "hh_apply_run_aborted",
                                reason=err,
                                vacancy_id=vacancy.id,
                            )
                            aborted_platforms.add(vacancy.platform)
                            skip_record = True
                        else:
                            log.warning("hh_oauth_failed", vacancy_id=vacancy.id, info=info)
                        # Fallback to Playwright only on quota / needs_test
                        if err == "needs_test":
                            log.info("hh_fallback_playwright_for_test", vacancy_id=vacancy.id)
                            # Vacancy has questionnaire — generate AI letter (it's worth tokens here)
                            try:
                                ai_letter, inp_tok, out_tok = await claude_ai.generate_cover_letter(
                                    vacancy.title,
                                    vacancy.description or "",
                                )
                                # Save AI generation record
                                async with async_session() as session:
                                    session.add(AIGeneration(
                                        vacancy_id=vacancy.id,
                                        gen_type="cover_letter",
                                        prompt=f"Cover letter for: {vacancy.title}",
                                        response=ai_letter,
                                        input_tokens=inp_tok,
                                        output_tokens=out_tok,
                                    ))
                                    await session.commit()
                            except Exception as e:
                                log.warning("ai_letter_skip", error=str(e))
                                ai_letter = letter
                            parser = HHParser()
                            try:
                                await asyncio.wait_for(parser.login(), timeout=60)
                                result = await asyncio.wait_for(
                                    parser.apply_to_vacancy(vacancy.url, ai_letter),
                                    timeout=300,
                                )
                            except asyncio.TimeoutError:
                                result = False
                except asyncio.TimeoutError:
                    log.error("hh_oauth_timeout", vacancy_id=vacancy.id)
                    result = False
            elif vacancy.platform == "habr":
                from app.parsers.habr import HabrParser
                parser = HabrParser()
                try:
                    await asyncio.wait_for(parser.login(), timeout=30)
                    # Habr apply: ограничиваем сверху 15с (по запросу пользователя).
                    # Если за 15с не отозвалось — пропускаем вакансию, идём дальше.
                    result = await asyncio.wait_for(
                        parser.apply_to_vacancy(vacancy.url, letter),
                        timeout=15,
                    )
                except asyncio.TimeoutError:
                    log.error("apply_timeout_global", vacancy_id=vacancy.id, url=vacancy.url)
                    result = False

            if skip_record:
                # Глобальная ошибка платформы — не пишем фейк-FAILED, идём дальше.
                # Цикл пропустит остальные вакансии этой платформы через aborted_platforms.
                continue

            success = result is True  # True != "already"
            already = result == "already"

            # Записываем результат
            async with async_session() as session:
                if not already:
                    # Don't log application if already applied
                    app = Application(
                        vacancy_id=vacancy.id,
                        platform=vacancy.platform,
                        cover_letter=letter,
                        status=ApplicationStatus.SENT if success else ApplicationStatus.FAILED,
                        attempt_count=1,
                    )
                    session.add(app)

                v = await session.get(Vacancy, vacancy.id)
                if success:
                    v.status = VacancyStatus.APPLIED
                    applied += 1
                elif already:
                    v.status = VacancyStatus.APPLIED
                await session.commit()

            log.info(
                "apply_result",
                vacancy_id=vacancy.id,
                platform=vacancy.platform,
                success=success,
            )

            # Habr — фиксированная задержка 11с. HH — рандом из настроек (3-12с).
            if vacancy.platform == "habr":
                await asyncio.sleep(11)
            else:
                await random_delay(settings.apply_delay_min, settings.apply_delay_max)

        except Exception as e:
            log.error("apply_error", vacancy_id=vacancy.id, error=str(e))
            async with async_session() as session:
                session.add(Application(
                    vacancy_id=vacancy.id,
                    platform=vacancy.platform,
                    status=ApplicationStatus.FAILED,
                    error_message=str(e),
                    attempt_count=1,
                ))
                await session.commit()

    log.info("auto_apply_complete", applied=applied)
    return applied
