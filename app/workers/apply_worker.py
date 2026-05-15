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


async def run_auto_apply(auto_mode: bool = False, min_score: float = 70):
    log.info("auto_apply_started", auto_mode=auto_mode, min_score=min_score)
    applied = 0

    # Проверяем дневной лимит
    async with async_session() as session:
        today_count = await session.scalar(
            select(func.count(Application.id)).where(
                Application.status == ApplicationStatus.SENT,
                func.date(Application.created_at) == func.current_date(),
            )
        )
        if today_count >= settings.max_applies_per_day:
            log.info("daily_limit_reached", count=today_count)
            return 0

        remaining = settings.max_applies_per_day - today_count

        # Берём одобренные вакансии с высоким скором
        result = await session.execute(
            select(Vacancy)
            .where(
                Vacancy.status == VacancyStatus.APPROVED,
                Vacancy.ai_score >= min_score,
            )
            .order_by(Vacancy.ai_score.desc())
            .limit(remaining)
        )
        vacancies = result.scalars().all()

    for vacancy in vacancies:
        try:
            # Генерируем сопроводительное
            letter, inp_tok, out_tok = await claude_ai.generate_cover_letter(
                vacancy.title,
                vacancy.description or "",
            )

            # Сохраняем генерацию AI
            async with async_session() as session:
                session.add(AIGeneration(
                    vacancy_id=vacancy.id,
                    gen_type="cover_letter",
                    prompt=f"Cover letter for: {vacancy.title}",
                    response=letter,
                    input_tokens=inp_tok,
                    output_tokens=out_tok,
                ))
                await session.commit()

            # Отправляем отклик
            success = False
            if vacancy.platform == "hh":
                parser = HHParser()
                success = await parser.apply_to_vacancy(vacancy.url, letter)

            # Записываем результат
            async with async_session() as session:
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
                await session.commit()

            log.info(
                "apply_result",
                vacancy_id=vacancy.id,
                platform=vacancy.platform,
                success=success,
            )

            await random_delay(10, 30)

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
