import json

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models.vacancy import Vacancy, VacancyStatus
from app.models.company import Company
from app.models.blacklist import Blacklist
from app.parsers.base import ParsedVacancy
from app.parsers.hh import HHParser
from app.parsers.workspace import WorkspaceParser
from app.parsers.geekjob import GeekJobParser
from app.parsers.habr import HabrParser
from app.ai.claude import claude_ai
from app.ai.rule_analyzer import analyze_vacancy as rule_analyze
from app.utils.anti_detect import random_delay

log = structlog.get_logger()

SEARCH_QUERIES = [
    "бизнес аналитик",
    "системный аналитик",
    "бизнес-аналитик",
    "системный аналитик middle",
    "аналитик",
    "фулстек аналитик",
    "product analyst",
    "аналитик REST API",
    "аналитик BPMN",
]

def _build_parsers() -> dict:
    """Active platforms based on per-platform caps (>0 = enabled)."""
    parsers = {"hh": HHParser()}
    if settings.max_applies_per_day_habr > 0:
        parsers["habr"] = HabrParser()
    return parsers


PARSERS = _build_parsers()


async def run_vacancy_search():
    log.info("vacancy_search_started")
    total_new = 0

    for platform_name, parser in PARSERS.items():
        try:
            logged_in = await parser.login()
            if not logged_in:
                log.warning("parser_login_failed", platform=platform_name)
                continue

            for query in SEARCH_QUERIES:
                try:
                    vacancies = await parser.search_vacancies(
                        query,
                        remote=True,
                    )
                    saved = await _save_vacancies(vacancies)
                    total_new += saved
                    log.info("search_batch_done", platform=platform_name, query=query, new=saved)
                    await random_delay(5, 15)
                except Exception as e:
                    log.error("search_query_error", platform=platform_name, query=query, error=str(e))
                    continue

        except Exception as e:
            log.error("parser_error", platform=platform_name, error=str(e))

    log.info("vacancy_search_complete", total_new=total_new)
    return total_new


async def _save_vacancies(parsed: list[ParsedVacancy]) -> int:
    saved = 0
    async with async_session() as session:
        blacklisted = await _get_blacklist(session)

        for pv in parsed:
            # Дедупликация
            existing = await session.scalar(
                select(Vacancy.id).where(
                    Vacancy.platform == pv.platform,
                    Vacancy.external_id == pv.external_id,
                )
            )
            if existing:
                continue

            # Проверка чёрного списка
            if _is_blacklisted(pv, blacklisted):
                continue

            # Компания
            company = None
            if pv.company_name:
                company = await session.scalar(
                    select(Company).where(
                        Company.name == pv.company_name,
                        Company.platform == pv.platform,
                    )
                )
                if not company:
                    company = Company(
                        name=pv.company_name,
                        url=pv.company_url,
                        platform=pv.platform,
                    )
                    session.add(company)
                    await session.flush()

                if company.is_blacklisted:
                    continue

            vacancy = Vacancy(
                platform=pv.platform,
                external_id=pv.external_id,
                url=pv.url,
                title=pv.title,
                description=pv.description,
                salary_from=pv.salary_from,
                salary_to=pv.salary_to,
                salary_currency=pv.salary_currency,
                location=pv.location,
                is_remote=pv.is_remote,
                experience=pv.experience,
                employment_type=pv.employment_type,
                skills=json.dumps(pv.skills, ensure_ascii=False) if pv.skills else None,
                company_id=company.id if company else None,
                status=VacancyStatus.NEW,
            )
            session.add(vacancy)
            saved += 1

        await session.commit()
    return saved


async def _get_blacklist(session: AsyncSession) -> dict[str, set[str]]:
    result = await session.execute(select(Blacklist))
    items = result.scalars().all()
    bl: dict[str, set[str]] = {"company": set(), "keyword": set(), "vacancy": set()}
    for item in items:
        bl.setdefault(item.entry_type, set()).add(item.value.lower())
    return bl


def _is_blacklisted(pv: ParsedVacancy, blacklist: dict[str, set[str]]) -> bool:
    if pv.company_name.lower() in blacklist.get("company", set()):
        return True
    if pv.external_id in blacklist.get("vacancy", set()):
        return True
    title_lower = pv.title.lower()
    for kw in blacklist.get("keyword", set()):
        if kw in title_lower:
            return True
    return False


async def run_vacancy_analysis():
    log.info("vacancy_analysis_started")
    analyzed = 0

    async with async_session() as session:
        result = await session.execute(
            select(Vacancy)
            .where(Vacancy.status == VacancyStatus.NEW)
            .order_by(Vacancy.created_at.desc())
            .limit(20)
        )
        vacancies = result.scalars().all()

    for vacancy in vacancies:
        try:
            # Загружаем детали если нет описания
            if not vacancy.description:
                parser = PARSERS.get(vacancy.platform)
                if parser:
                    details = await parser.get_vacancy_details(vacancy.url)
                    if details and details.description:
                        async with async_session() as session:
                            v = await session.get(Vacancy, vacancy.id)
                            v.description = details.description
                            v.skills = json.dumps(details.skills, ensure_ascii=False) if details.skills else v.skills
                            v.experience = details.experience or v.experience
                            await session.commit()
                            vacancy = v
                        await random_delay(3, 8)

            # Rule-based анализ — без трат AI-токенов
            analysis = rule_analyze(
                title=vacancy.title,
                description=vacancy.description or "",
                skills=vacancy.skills or "",
                salary_from=vacancy.salary_from,
                salary_to=vacancy.salary_to,
                is_remote=bool(vacancy.is_remote),
                desired_salary_min=settings.desired_salary_min,
                desired_salary_max=settings.desired_salary_max,
            )

            async with async_session() as session:
                v = await session.get(Vacancy, vacancy.id)
                v.ai_score = analysis.get("score", 0)
                v.ai_reason = analysis.get("reason", "")
                v.status = VacancyStatus.ANALYZED
                if analysis.get("is_relevant") and analysis.get("score", 0) >= 60:
                    v.status = VacancyStatus.APPROVED
                await session.commit()

            analyzed += 1
            await random_delay(0, 2)

        except Exception as e:
            log.error("vacancy_analysis_error", vacancy_id=vacancy.id, error=str(e))

    log.info("vacancy_analysis_complete", analyzed=analyzed)
    return analyzed
