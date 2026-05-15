import json

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.models.vacancy import Vacancy, VacancyStatus
from app.models.application import Application, ApplicationStatus
from app.models.blacklist import Blacklist
from app.models.message import RecruiterMessage
from app.ai.claude import claude_ai
from app.bot.keyboards import (
    main_menu,
    vacancy_keyboard,
    vacancy_list_keyboard,
    message_keyboard,
    confirm_apply_keyboard,
    settings_keyboard,
)

router = Router()

PAGE_SIZE = 5


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
        "👋 <b>Job Hunter Bot</b>\n\n"
        "Автоматический поиск вакансий аналитика\n"
        "Используй кнопки ниже 👇",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# ══════════════════════════════════════════════════════════════
#  КНОПКИ ГЛАВНОГО МЕНЮ
# ══════════════════════════════════════════════════════════════

@router.message(F.text == "📊 Статистика")
@admin_only
async def btn_stats(message: Message):
    async with async_session() as session:
        total = await session.scalar(select(func.count(Vacancy.id))) or 0
        new = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status == VacancyStatus.NEW)
        ) or 0
        analyzed = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status == VacancyStatus.ANALYZED)
        ) or 0
        approved = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status == VacancyStatus.APPROVED)
        ) or 0
        applied_v = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status == VacancyStatus.APPLIED)
        ) or 0
        applied = await session.scalar(
            select(func.count(Application.id)).where(Application.status == ApplicationStatus.SENT)
        ) or 0
        responses = await session.scalar(select(func.count(RecruiterMessage.id))) or 0
        avg_score = await session.scalar(
            select(func.avg(Vacancy.ai_score)).where(Vacancy.ai_score.is_not(None))
        )

    score_text = f"{avg_score:.0f}" if avg_score else "—"

    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"📦 Всего вакансий: <b>{total}</b>\n"
        f"🆕 Новые: <b>{new}</b>\n"
        f"🤖 Проанализировано: <b>{analyzed}</b>\n"
        f"⭐ Одобрено AI: <b>{approved}</b>\n"
        f"📨 Отправлено откликов: <b>{applied}</b>\n"
        f"💬 Ответов рекрутеров: <b>{responses}</b>\n"
        f"📈 Средний AI-скор: <b>{score_text}</b>",
        parse_mode="HTML",
    )


@router.message(F.text == "🔍 Вакансии")
@admin_only
async def btn_vacancies(message: Message):
    await _send_vacancy_page(message, page=0)


@router.message(F.text == "⭐ Топ вакансии")
@admin_only
async def btn_top(message: Message):
    await _send_vacancy_page(message, page=0, top_only=True)


@router.message(F.text == "📩 Сообщения")
@admin_only
async def btn_messages(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(RecruiterMessage)
            .where(RecruiterMessage.is_read == False)
            .order_by(RecruiterMessage.created_at.desc())
            .limit(10)
        )
        messages_list = result.scalars().all()

    if not messages_list:
        await message.answer("📩 Нет непрочитанных сообщений")
        return

    for msg in messages_list:
        text = (
            f"📩 <b>Сообщение</b>\n\n"
            f"👤 {msg.sender_name or 'Неизвестно'}\n"
            f"🏢 {msg.sender_company or '—'}\n"
            f"📧 {msg.platform}\n\n"
            f"{msg.text[:500]}"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=message_keyboard(msg.id))


@router.message(F.text == "⚙️ Настройки")
@admin_only
async def btn_settings(message: Message):
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"📍 Позиция: {settings.desired_position}\n"
        f"💰 Зарплата: {settings.desired_salary_min:,}–{settings.desired_salary_max:,}\n"
        f"⏱ Интервал: {settings.check_interval_sec // 60} мин\n"
        f"🎯 Макс. откликов/день: {settings.max_applies_per_day}",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


@router.message(F.text == "📋 Логи")
@admin_only
async def btn_logs(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(Application)
            .order_by(Application.created_at.desc())
            .limit(10)
        )
        apps = result.scalars().all()

    if not apps:
        await message.answer("📋 Пока нет записей")
        return

    lines = []
    for a in apps:
        emoji = {"sent": "✅", "failed": "❌", "pending": "⏳"}.get(a.status.value, "❓")
        lines.append(f"{emoji} [{a.platform}] ID:{a.vacancy_id} — {a.status.value}")

    await message.answer(
        "📋 <b>Последние отклики</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════
#  СТАРЫЕ КОМАНДЫ (тоже работают)
# ══════════════════════════════════════════════════════════════

@router.message(Command("stats"))
@admin_only
async def cmd_stats(message: Message):
    await btn_stats(message)


@router.message(Command("vacancies"))
@admin_only
async def cmd_vacancies(message: Message):
    await btn_vacancies(message)


@router.message(Command("messages"))
@admin_only
async def cmd_messages(message: Message):
    await btn_messages(message)


@router.message(Command("settings"))
@admin_only
async def cmd_settings(message: Message):
    await btn_settings(message)


@router.message(Command("blacklist"))
@admin_only
async def cmd_blacklist(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        async with async_session() as session:
            result = await session.execute(select(Blacklist).limit(20))
            items = result.scalars().all()

        if not items:
            await message.answer("🚫 Чёрный список пуст\n\nДобавить: /blacklist <company|keyword> <значение>")
            return

        lines = [f"• [{b.entry_type}] {b.value}" for b in items]
        await message.answer("🚫 <b>Чёрный список:</b>\n\n" + "\n".join(lines), parse_mode="HTML")
        return

    entry_type = args[1] if args[1] in ("company", "keyword", "vacancy") else "keyword"
    value = args[2] if len(args) > 2 else args[1]

    async with async_session() as session:
        session.add(Blacklist(entry_type=entry_type, value=value))
        await session.commit()

    await message.answer(f"✅ Добавлено в ЧС: [{entry_type}] {value}")


# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════

async def _send_vacancy_page(target, page: int = 0, top_only: bool = False):
    async with async_session() as session:
        query = select(Vacancy).where(
            Vacancy.status.in_([VacancyStatus.NEW, VacancyStatus.ANALYZED, VacancyStatus.APPROVED])
        )
        if top_only:
            query = query.where(Vacancy.ai_score >= 60)

        query = query.order_by(Vacancy.ai_score.desc().nullslast(), Vacancy.created_at.desc())

        total = await session.scalar(
            select(func.count(Vacancy.id)).where(
                Vacancy.status.in_([VacancyStatus.NEW, VacancyStatus.ANALYZED, VacancyStatus.APPROVED])
            ).where(Vacancy.ai_score >= 60) if top_only else
            select(func.count(Vacancy.id)).where(
                Vacancy.status.in_([VacancyStatus.NEW, VacancyStatus.ANALYZED, VacancyStatus.APPROVED])
            )
        ) or 0

        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages - 1)

        result = await session.execute(
            query.offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
        vacancies = result.scalars().all()

    if not vacancies:
        label = "⭐ топ-вакансий" if top_only else "🔍 вакансий"
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(f"Нет {label}")
            await target.answer()
        else:
            await target.answer(f"Нет {label}")
        return

    lines = []
    ids = []
    for i, v in enumerate(vacancies):
        num = page * PAGE_SIZE + i + 1
        salary = ""
        if v.salary_from or v.salary_to:
            parts = []
            if v.salary_from:
                parts.append(f"от {v.salary_from:,}")
            if v.salary_to:
                parts.append(f"до {v.salary_to:,}")
            salary = f" | 💰 {' '.join(parts)} {v.salary_currency or ''}"

        score = f" | 🤖 {v.ai_score:.0f}" if v.ai_score else ""
        remote = " 🏠" if v.is_remote else ""
        company = f"\n   🏢 {v.company_name}" if v.company_name else ""

        lines.append(
            f"<b>{num}. {v.title}</b>{remote}{company}"
            f"\n   📍 {v.location or '—'}{salary}{score}"
            f"\n   🔗 <a href='{v.url}'>Открыть на hh.ru</a>"
        )
        ids.append(v.id)

    header = "⭐ <b>Топ вакансии</b>" if top_only else "🔍 <b>Вакансии</b>"
    prefix = "top:" if top_only else ""
    text = f"{header} ({total} шт.)\n\n" + "\n\n".join(lines)
    kb = vacancy_list_keyboard(ids, page, total_pages, prefix)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# ══════════════════════════════════════════════════════════════
#  CALLBACK-ХЭНДЛЕРЫ
# ══════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("page:"))
@admin_only
async def cb_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await _send_vacancy_page(callback, page=page)


@router.callback_query(F.data.startswith("top_page:"))
@admin_only
async def cb_top_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await _send_vacancy_page(callback, page=page, top_only=True)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("apply:"))
@admin_only
async def cb_apply(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if not vacancy:
            await callback.answer("Вакансия не найдена")
            return

    await callback.answer("🤖 Генерирую письмо...")

    letter, _, _ = await claude_ai.generate_cover_letter(
        vacancy.title,
        vacancy.description or "",
        "",
    )

    await callback.message.answer(
        f"✉️ <b>Сопроводительное для:</b>\n{vacancy.title}\n\n{letter}",
        parse_mode="HTML",
        reply_markup=confirm_apply_keyboard(vacancy_id),
    )


@router.callback_query(F.data.startswith("confirm_apply:"))
@admin_only
async def cb_confirm_apply(callback: CallbackQuery):
    vacancy_id = int(callback.data.split(":")[1])
    await callback.answer("📨 Отправляю отклик...")
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
    await callback.answer("❌ Пропущена")


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
        ai_info = f"\n\n🤖 <b>AI-скор: {vacancy.ai_score:.0f}/100</b>\n{vacancy.ai_reason or '—'}"

    salary = ""
    if vacancy.salary_from or vacancy.salary_to:
        parts = []
        if vacancy.salary_from:
            parts.append(f"от {vacancy.salary_from:,}")
        if vacancy.salary_to:
            parts.append(f"до {vacancy.salary_to:,}")
        salary = f"\n💰 {' '.join(parts)} {vacancy.salary_currency or ''}"

    company = f"\n🏢 {vacancy.company_name}" if vacancy.company_name else ""

    await callback.message.answer(
        f"<b>{vacancy.title}</b>{company}{salary}{ai_info}\n\n"
        f"{desc}\n\n"
        f"🔗 <a href='{vacancy.url}'>Открыть</a>",
        parse_mode="HTML",
        reply_markup=vacancy_keyboard(vacancy_id),
        disable_web_page_preview=True,
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
    await callback.answer("🚫 В чёрном списке")


@router.callback_query(F.data.startswith("ai_reply:"))
@admin_only
async def cb_ai_reply(callback: CallbackQuery):
    msg_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        msg = await session.get(RecruiterMessage, msg_id)
        if not msg:
            await callback.answer("Не найдено")
            return

    await callback.answer("🤖 Генерирую ответ...")
    reply, _, _ = await claude_ai.generate_reply(msg.text)

    async with async_session() as session:
        msg = await session.get(RecruiterMessage, msg_id)
        if msg:
            msg.ai_suggested_reply = reply
            await session.commit()

    await callback.message.answer(
        f"🤖 <b>AI-ответ:</b>\n\n{reply}",
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
    await callback.answer("✅ Прочитано")
    await callback.message.delete()


@router.callback_query(F.data == "toggle_pause")
@admin_only
async def cb_toggle_pause(callback: CallbackQuery):
    await callback.answer("Используй /pause или /resume")


@router.callback_query(F.data == "toggle_auto")
@admin_only
async def cb_toggle_auto(callback: CallbackQuery):
    await callback.answer("Авто-отклик переключён")


@router.callback_query(F.data == "force_search")
@admin_only
async def cb_force_search(callback: CallbackQuery):
    await callback.answer("🔄 Запускаю поиск...")
    from app.workers.vacancy_worker import run_vacancy_search
    count = await run_vacancy_search()
    await callback.message.answer(f"🔍 Найдено <b>{count}</b> новых вакансий", parse_mode="HTML")


@router.callback_query(F.data.startswith("cancel_apply:"))
@admin_only
async def cb_cancel_apply(callback: CallbackQuery):
    await callback.answer("Отменено")
    await callback.message.delete()


# Поддержка старых команд
@router.message(Command("pause"))
@admin_only
async def cmd_pause(message: Message):
    await message.answer("⏸ Пауза. /resume — возобновить", reply_markup=main_menu())


@router.message(Command("resume"))
@admin_only
async def cmd_resume(message: Message):
    await message.answer("▶️ Возобновлено", reply_markup=main_menu())


@router.message(Command("logs"))
@admin_only
async def cmd_logs(message: Message):
    await btn_logs(message)
