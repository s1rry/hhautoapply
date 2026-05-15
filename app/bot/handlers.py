import json

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models.vacancy import Vacancy, VacancyStatus
from app.models.application import Application, ApplicationStatus
from app.models.blacklist import Blacklist
from app.models.message import RecruiterMessage
from app.ai.claude import claude_ai
from app.bot.keyboards import vacancy_keyboard, message_keyboard, confirm_apply_keyboard, settings_keyboard

router = Router()


def admin_only(func):
    async def wrapper(msg_or_cb, *args, **kwargs):
        chat_id = msg_or_cb.chat.id if isinstance(msg_or_cb, Message) else msg_or_cb.message.chat.id
        if str(chat_id) != settings.tg_admin_chat_id:
            return
        return await func(msg_or_cb, *args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ══════════════════════════════════════════════════════════════

@router.message(Command("start"))
@admin_only
async def cmd_start(message: Message):
    await message.answer(
        "<b>Job Hunter Bot</b>\n\n"
        "Автоматический поиск и отклик на вакансии\n\n"
        "<b>Команды:</b>\n"
        "/stats — статистика\n"
        "/vacancies — последние вакансии\n"
        "/apply — ручной отклик\n"
        "/messages — сообщения от рекрутеров\n"
        "/blacklist — чёрный список\n"
        "/settings — настройки\n"
        "/pause — пауза\n"
        "/resume — возобновить\n"
        "/logs — последние события",
        parse_mode="HTML",
    )


@router.message(Command("stats"))
@admin_only
async def cmd_stats(message: Message):
    async with async_session() as session:
        total = await session.scalar(select(func.count(Vacancy.id)))
        analyzed = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status == VacancyStatus.ANALYZED)
        )
        applied = await session.scalar(
            select(func.count(Application.id)).where(Application.status == ApplicationStatus.SENT)
        )
        responses = await session.scalar(select(func.count(RecruiterMessage.id)))
        unread = await session.scalar(
            select(func.count(RecruiterMessage.id)).where(RecruiterMessage.is_read == False)
        )
        avg_score = await session.scalar(
            select(func.avg(Vacancy.ai_score)).where(Vacancy.ai_score.is_not(None))
        )

    await message.answer(
        "<b>Статистика</b>\n\n"
        f"Всего вакансий: <b>{total or 0}</b>\n"
        f"Проанализировано: <b>{analyzed or 0}</b>\n"
        f"Отправлено откликов: <b>{applied or 0}</b>\n"
        f"Ответов рекрутеров: <b>{responses or 0}</b>\n"
        f"Непрочитано: <b>{unread or 0}</b>\n"
        f"Средний AI-скор: <b>{avg_score:.1f}</b>" if avg_score else "Средний AI-скор: <b>—</b>",
        parse_mode="HTML",
    )


@router.message(Command("vacancies"))
@admin_only
async def cmd_vacancies(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(Vacancy)
            .where(Vacancy.status.in_([VacancyStatus.NEW, VacancyStatus.ANALYZED, VacancyStatus.APPROVED]))
            .order_by(Vacancy.ai_score.desc().nullslast(), Vacancy.created_at.desc())
            .limit(10)
        )
        vacancies = result.scalars().all()

    if not vacancies:
        await message.answer("Нет новых вакансий")
        return

    for v in vacancies:
        salary = ""
        if v.salary_from or v.salary_to:
            parts = []
            if v.salary_from:
                parts.append(f"от {v.salary_from:,}")
            if v.salary_to:
                parts.append(f"до {v.salary_to:,}")
            salary = f"\n💰 {' '.join(parts)} {v.salary_currency or 'руб.'}"

        score = f"\n🤖 AI-скор: {v.ai_score:.0f}/100" if v.ai_score else ""
        remote = " 🏠" if v.is_remote else ""

        text = (
            f"<b>{v.title}</b>{remote}\n"
            f"🏢 {v.location or 'Не указано'}{salary}{score}\n"
            f"📌 {v.status.value}\n"
            f"🔗 <a href='{v.url}'>Открыть</a>"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=vacancy_keyboard(v.id))


@router.message(Command("messages"))
@admin_only
async def cmd_messages(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(RecruiterMessage)
            .where(RecruiterMessage.is_read == False)
            .order_by(RecruiterMessage.created_at.desc())
            .limit(10)
        )
        messages_list = result.scalars().all()

    if not messages_list:
        await message.answer("Нет непрочитанных сообщений")
        return

    for msg in messages_list:
        text = (
            f"<b>Сообщение от рекрутера</b>\n\n"
            f"👤 {msg.sender_name or 'Неизвестно'}\n"
            f"🏢 {msg.sender_company or '—'}\n"
            f"📧 {msg.platform}\n\n"
            f"{msg.text[:500]}"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=message_keyboard(msg.id))


@router.message(Command("blacklist"))
@admin_only
async def cmd_blacklist(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        async with async_session() as session:
            result = await session.execute(select(Blacklist).limit(20))
            items = result.scalars().all()

        if not items:
            await message.answer("Чёрный список пуст\n\nДобавить: /blacklist <company|keyword> <значение>")
            return

        lines = [f"• [{b.entry_type}] {b.value}" for b in items]
        await message.answer("<b>Чёрный список:</b>\n\n" + "\n".join(lines), parse_mode="HTML")
        return

    entry_type = args[1] if args[1] in ("company", "keyword", "vacancy") else "keyword"
    value = args[2] if len(args) > 2 else args[1]

    async with async_session() as session:
        session.add(Blacklist(entry_type=entry_type, value=value))
        await session.commit()

    await message.answer(f"Добавлено в чёрный список: [{entry_type}] {value}")


@router.message(Command("settings"))
@admin_only
async def cmd_settings(message: Message):
    await message.answer(
        "<b>Настройки</b>\n\n"
        f"Интервал проверки: {settings.check_interval_sec // 60} мин\n"
        f"Макс. откликов/день: {settings.max_applies_per_day}\n"
        f"Позиция: {settings.desired_position}\n"
        f"Зарплата: {settings.desired_salary_min:,}–{settings.desired_salary_max:,}\n"
        f"Прокси: {'Да' if settings.proxy_url else 'Нет'}",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


@router.message(Command("pause"))
@admin_only
async def cmd_pause(message: Message):
    # Сигнал воркеру через Redis
    await message.answer("⏸ Парсинг и отклики приостановлены\n/resume — возобновить")


@router.message(Command("resume"))
@admin_only
async def cmd_resume(message: Message):
    await message.answer("▶️ Парсинг и отклики возобновлены")


@router.message(Command("logs"))
@admin_only
async def cmd_logs(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(Application)
            .order_by(Application.created_at.desc())
            .limit(10)
        )
        apps = result.scalars().all()

    if not apps:
        await message.answer("Пока нет записей в логах")
        return

    lines = []
    for a in apps:
        status_emoji = {"sent": "✅", "failed": "❌", "pending": "⏳"}.get(a.status.value, "❓")
        lines.append(f"{status_emoji} [{a.platform}] ID:{a.vacancy_id} — {a.status.value}")

    await message.answer("<b>Последние отклики:</b>\n\n" + "\n".join(lines), parse_mode="HTML")


# ══════════════════════════════════════════════════════════════
#  CALLBACK-ХЭНДЛЕРЫ
# ══════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("apply:"))
@admin_only
async def cb_apply(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if not vacancy:
            await callback.answer("Вакансия не найдена")
            return

    await callback.answer("Генерирую сопроводительное письмо...")

    letter, _, _ = await claude_ai.generate_cover_letter(
        vacancy.title,
        vacancy.description or "",
        "",
    )

    await callback.message.answer(
        f"<b>Сопроводительное письмо для:</b>\n{vacancy.title}\n\n{letter}",
        parse_mode="HTML",
        reply_markup=confirm_apply_keyboard(vacancy_id),
    )


@router.callback_query(F.data.startswith("confirm_apply:"))
@admin_only
async def cb_confirm_apply(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    await callback.answer("Отправляю отклик...")
    await callback.message.edit_text(
        callback.message.text + "\n\n⏳ Отправка...",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("skip:"))
@admin_only
async def cb_skip(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if vacancy:
            vacancy.status = VacancyStatus.REJECTED
            await session.commit()
    await callback.answer("Вакансия пропущена")
    await callback.message.delete()


@router.callback_query(F.data.startswith("details:"))
@admin_only
async def cb_details(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if not vacancy:
            await callback.answer("Не найдена")
            return

    desc = vacancy.description or "Описание не загружено"
    if len(desc) > 3000:
        desc = desc[:3000] + "..."

    ai_info = ""
    if vacancy.ai_score is not None:
        ai_info = f"\n\n<b>AI-анализ (скор: {vacancy.ai_score:.0f}/100):</b>\n{vacancy.ai_reason or '—'}"

    await callback.message.answer(
        f"<b>{vacancy.title}</b>\n\n{desc}{ai_info}",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("blacklist:"))
@admin_only
async def cb_blacklist(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if vacancy:
            vacancy.status = VacancyStatus.REJECTED
            session.add(Blacklist(entry_type="vacancy", value=str(vacancy.external_id), reason="Manual blacklist"))
            await session.commit()
    await callback.answer("Добавлено в чёрный список")
    await callback.message.delete()


@router.callback_query(F.data.startswith("ai_reply:"))
@admin_only
async def cb_ai_reply(callback: CallbackQuery):
    msg_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        msg = await session.get(RecruiterMessage, msg_id)
        if not msg:
            await callback.answer("Сообщение не найдено")
            return

    await callback.answer("Генерирую ответ...")
    reply, _, _ = await claude_ai.generate_reply(msg.text)

    async with async_session() as session:
        msg = await session.get(RecruiterMessage, msg_id)
        if msg:
            msg.ai_suggested_reply = reply
            await session.commit()

    await callback.message.answer(
        f"<b>AI-ответ:</b>\n\n{reply}",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("mark_read:"))
@admin_only
async def cb_mark_read(callback: CallbackQuery):
    msg_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        msg = await session.get(RecruiterMessage, msg_id)
        if msg:
            msg.is_read = True
            await session.commit()
    await callback.answer("Отмечено как прочитанное")
    await callback.message.delete()
