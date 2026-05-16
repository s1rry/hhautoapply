import json
import functools

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

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

_scheduler = None


def set_scheduler(scheduler):
    global _scheduler
    _scheduler = scheduler


def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(event, **kwargs):
        chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
        if str(chat_id) != settings.tg_admin_chat_id:
            return
        return await fn(event, **kwargs)
    return wrapper


def _company_name(vacancy) -> str:
    if vacancy.company and vacancy.company.name:
        return vacancy.company.name
    return ""


# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ И КНОПКИ МЕНЮ
# ══════════════════════════════════════════════════════════════

@router.message(Command("start"))
@admin_only
async def cmd_start(message: Message, **kw):
    await message.answer(
        "👋 <b>Job Hunter Bot</b>\n\n"
        "Автоматический поиск вакансий аналитика\n"
        "Используй кнопки ниже 👇",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@router.message(F.text == "📊 Статистика")
@router.message(Command("stats"))
@admin_only
async def btn_stats(message: Message, **kw):
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
        applied_total = await session.scalar(
            select(func.count(Application.id)).where(Application.status == ApplicationStatus.SENT)
        ) or 0
        applied_today = await session.scalar(
            select(func.count(Application.id)).where(
                Application.status == ApplicationStatus.SENT,
                func.date(Application.created_at) == func.current_date(),
            )
        ) or 0
        failed_today = await session.scalar(
            select(func.count(Application.id)).where(
                Application.status == ApplicationStatus.FAILED,
                func.date(Application.created_at) == func.current_date(),
            )
        ) or 0
        responses = await session.scalar(select(func.count(RecruiterMessage.id))) or 0
        avg_score = await session.scalar(
            select(func.avg(Vacancy.ai_score)).where(Vacancy.ai_score.is_not(None))
        )

    score_text = f"{avg_score:.0f}" if avg_score else "—"
    limit = settings.max_applies_per_day

    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"📦 Всего вакансий: <b>{total}</b>\n"
        f"🆕 Новые: <b>{new}</b>\n"
        f"🤖 Проанализировано: <b>{analyzed}</b>\n"
        f"⭐ Одобрено AI: <b>{approved}</b>\n\n"
        f"📨 <b>Отклики:</b>\n"
        f"  • Сегодня: <b>{applied_today}/{limit}</b>\n"
        f"  • Ошибок сегодня: <b>{failed_today}</b>\n"
        f"  • Всего отправлено: <b>{applied_total}</b>\n\n"
        f"💬 Ответов рекрутеров: <b>{responses}</b>\n"
        f"📈 Средний AI-скор: <b>{score_text}</b>",
        parse_mode="HTML",
    )


@router.message(F.text == "🔍 Вакансии")
@router.message(Command("vacancies"))
@admin_only
async def btn_vacancies(message: Message, **kw):
    await _send_vacancy_page(message, page=0)


@router.message(F.text == "⭐ Топ вакансии")
@admin_only
async def btn_top(message: Message, **kw):
    await _send_vacancy_page(message, page=0, top_only=True)


@router.message(F.text == "📩 Сообщения")
@router.message(Command("messages"))
@admin_only
async def btn_messages(message: Message, **kw):
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


def _settings_text(paused: bool, auto: bool) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"📍 Позиция: {settings.desired_position}\n"
        f"💰 Зарплата: {settings.desired_salary_min:,}–{settings.desired_salary_max:,}\n"
        f"⏱ Интервал: {settings.check_interval_sec // 60} мин\n"
        f"🎯 Макс. откликов/день: {settings.max_applies_per_day}\n"
        f"🔔 Уведомления: {settings.notify_hour_start}:00–{settings.notify_hour_end}:00 МСК\n"
        f"{'⏸ Пауза' if paused else '▶️ Работает'} | "
        f"{'🟢 Авто-отклик ВКЛ' if auto else '⚪ Авто-отклик ВЫКЛ'}"
    )


@router.message(F.text == "⚙️ Настройки")
@router.message(Command("settings"))
@admin_only
async def btn_settings(message: Message, **kw):
    paused = _scheduler.is_paused if _scheduler else False
    auto = _scheduler.auto_apply if _scheduler else False
    await message.answer(
        _settings_text(paused, auto),
        parse_mode="HTML",
        reply_markup=settings_keyboard(paused, auto),
    )


@router.message(F.text == "📋 Логи")
@router.message(Command("logs"))
@admin_only
async def btn_logs(message: Message, **kw):
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


@router.message(Command("blacklist"))
@admin_only
async def cmd_blacklist(message: Message, **kw):
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


@router.message(Command("pause"))
@admin_only
async def cmd_pause(message: Message, **kw):
    if _scheduler:
        _scheduler.pause()
    await message.answer("⏸ Пауза. /resume — возобновить", reply_markup=main_menu())


@router.message(Command("resume"))
@admin_only
async def cmd_resume(message: Message, **kw):
    if _scheduler:
        _scheduler.resume()
    await message.answer("▶️ Возобновлено", reply_markup=main_menu())


@router.message(Command("balance"))
@admin_only
async def cmd_balance(message: Message, **kw):
    await _send_balance(message)


async def _send_balance(target):
    """Отправить баланс AI — работает и для Message, и для CallbackQuery."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.anthropic_base_url}/v1/balance",
                headers={"Authorization": f"Bearer {settings.anthropic_api_key}"},
            )
            data = resp.json()
        balance = data.get("balance_cents", 0)
        inp = data.get("total_input_tokens", 0)
        out = data.get("total_output_tokens", 0)
        total = data.get("total_tokens_used", 0)
        text = (
            f"💎 <b>Баланс WaveAPI</b>\n\n"
            f"💰 Баланс: <b>{balance} центов</b> (${balance/100:.2f})\n"
            f"📥 Input токены: <b>{inp:,}</b>\n"
            f"📤 Output токены: <b>{out:,}</b>\n"
            f"📊 Всего использовано: <b>{total:,}</b>"
        )
        if isinstance(target, CallbackQuery):
            await target.message.answer(text, parse_mode="HTML")
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML")
    except Exception as e:
        err = f"❌ Ошибка: {e}"
        if isinstance(target, CallbackQuery):
            await target.message.answer(err)
            await target.answer()
        else:
            await target.answer(err)


# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════

async def _send_vacancy_page(target, page: int = 0, top_only: bool = False):
    async with async_session() as session:
        base_filter = Vacancy.status.in_([VacancyStatus.NEW, VacancyStatus.ANALYZED, VacancyStatus.APPROVED])

        count_q = select(func.count(Vacancy.id)).where(base_filter)
        if top_only:
            count_q = count_q.where(Vacancy.ai_score >= 60)
        total = await session.scalar(count_q) or 0

        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages - 1)

        query = (
            select(Vacancy)
            .options(selectinload(Vacancy.company))
            .where(base_filter)
        )
        if top_only:
            query = query.where(Vacancy.ai_score >= 60)

        query = query.order_by(Vacancy.ai_score.desc().nullslast(), Vacancy.created_at.desc())
        result = await session.execute(query.offset(page * PAGE_SIZE).limit(PAGE_SIZE))
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
        cname = _company_name(v)
        company = f"\n   🏢 {cname}" if cname else ""

        lines.append(
            f"<b>{num}. {v.title}</b>{remote}{company}"
            f"\n   📍 {v.location or '—'}{salary}{score}"
            f"\n   🔗 <a href='{v.url}'>Открыть</a>"
        )
        ids.append(v.id)

    header = "⭐ <b>Топ вакансии</b>" if top_only else "🔍 <b>Вакансии</b>"
    prefix = "top_" if top_only else ""
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
async def cb_page(callback: CallbackQuery, **kw):
    page = int(callback.data.split(":")[1])
    await _send_vacancy_page(callback, page=page)


@router.callback_query(F.data.startswith("top_page:"))
@admin_only
async def cb_top_page(callback: CallbackQuery, **kw):
    page = int(callback.data.split(":")[1])
    await _send_vacancy_page(callback, page=page, top_only=True)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery, **kw):
    await callback.answer()


@router.callback_query(F.data.startswith("apply:"))
@admin_only
async def cb_apply(callback: CallbackQuery, **kw):
    vacancy_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if not vacancy:
            await callback.answer("Вакансия не найдена")
            return
        title = vacancy.title
        desc = vacancy.description or ""

    await callback.answer("🤖 Генерирую письмо...")

    letter, _, _ = await claude_ai.generate_cover_letter(title, desc, "")

    await callback.message.answer(
        letter,
        reply_markup=confirm_apply_keyboard(vacancy_id),
    )


@router.callback_query(F.data.startswith("confirm_apply:"))
@admin_only
async def cb_confirm_apply(callback: CallbackQuery, **kw):
    vacancy_id = int(callback.data.split(":")[1])

    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if not vacancy:
            await callback.answer("Вакансия не найдена")
            return

    # Check if Playwright is available
    from app.parsers.hh import HHParser
    parser = HHParser()
    pw = parser._get_playwright()
    if not pw:
        await callback.answer("❌ Playwright не доступен (только на VPS)")
        return

    await callback.answer("📨 Отправляю отклик...")

    # Get the cover letter from the previous message
    cover_letter = ""
    if callback.message and callback.message.reply_to_message:
        cover_letter = callback.message.reply_to_message.text or ""
    elif callback.message:
        # Extract letter from the message text
        text = callback.message.text or ""
        if "\n\n" in text:
            parts = text.split("\n\n", 2)
            if len(parts) > 1:
                cover_letter = parts[-1]

    result = await parser.apply_to_vacancy(vacancy.url, cover_letter)

    from pathlib import Path
    from aiogram.types import FSInputFile

    if result == "already":
        # Vacancy already had a response (from before or some other source)
        async with async_session() as session:
            v = await session.get(Vacancy, vacancy_id)
            if v:
                v.status = VacancyStatus.APPLIED
                await session.commit()
        await callback.message.answer(
            "ℹ️ На эту вакансию уже есть отклик (новый не отправлен).\n"
            "Возможно, ты откликался раньше с этого аккаунта."
        )
        p = Path("data/debug_already_applied.png")
        if p.exists():
            try:
                await callback.message.answer_photo(FSInputFile(p), caption="🖼 Что видит бот")
            except Exception:
                pass
    elif result:
        async with async_session() as session:
            v = await session.get(Vacancy, vacancy_id)
            if v:
                v.status = VacancyStatus.APPLIED
                from app.models.application import Application, ApplicationStatus
                session.add(Application(
                    vacancy_id=vacancy_id,
                    platform=v.platform,
                    cover_letter=cover_letter,
                    status=ApplicationStatus.SENT,
                    attempt_count=1,
                ))
                await session.commit()
        await callback.message.answer("✅ Отклик отправлен!")
    else:
        await callback.message.answer("❌ Не удалось отправить отклик")
        for name in ("debug_apply_fail.png", "debug_apply_timeout.png", "debug_apply_no_btn.png"):
            p = Path(f"data/{name}")
            if p.exists():
                try:
                    await callback.message.answer_photo(FSInputFile(p), caption=f"🖼 {name}")
                except Exception:
                    pass


@router.callback_query(F.data.startswith("skip:"))
@admin_only
async def cb_skip(callback: CallbackQuery, **kw):
    vacancy_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        if vacancy:
            vacancy.status = VacancyStatus.REJECTED
            await session.commit()
    await callback.answer("❌ Пропущена")


@router.callback_query(F.data.startswith("details:"))
@admin_only
async def cb_details(callback: CallbackQuery, **kw):
    vacancy_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        vacancy = await session.get(Vacancy, vacancy_id, options=[selectinload(Vacancy.company)])
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

        cname = _company_name(vacancy)
        company = f"\n🏢 {cname}" if cname else ""

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
async def cb_blacklist(callback: CallbackQuery, **kw):
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
async def cb_ai_reply(callback: CallbackQuery, **kw):
    msg_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        msg = await session.get(RecruiterMessage, msg_id)
        if not msg:
            await callback.answer("Не найдено")
            return
        msg_text = msg.text

    await callback.answer("🤖 Генерирую ответ...")
    reply, _, _ = await claude_ai.generate_reply(msg_text)

    async with async_session() as session:
        msg = await session.get(RecruiterMessage, msg_id)
        if msg:
            msg.ai_suggested_reply = reply
            await session.commit()

    await callback.message.answer(f"🤖 <b>AI-ответ:</b>\n\n{reply}", parse_mode="HTML")


@router.callback_query(F.data.startswith("mark_read:"))
@admin_only
async def cb_mark_read(callback: CallbackQuery, **kw):
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
async def cb_toggle_pause(callback: CallbackQuery, **kw):
    if not _scheduler:
        await callback.answer("Scheduler не найден")
        return
    if _scheduler.is_paused:
        _scheduler.resume()
        await callback.answer("▶️ Возобновлено")
    else:
        _scheduler.pause()
        await callback.answer("⏸ На паузе")
    await callback.message.edit_text(
        _settings_text(_scheduler.is_paused, _scheduler.auto_apply),
        parse_mode="HTML",
        reply_markup=settings_keyboard(_scheduler.is_paused, _scheduler.auto_apply),
    )


@router.callback_query(F.data == "toggle_auto")
@admin_only
async def cb_toggle_auto(callback: CallbackQuery, **kw):
    if not _scheduler:
        await callback.answer("Scheduler не найден")
        return
    _scheduler.auto_apply = not _scheduler.auto_apply
    status = "🟢 ВКЛ" if _scheduler.auto_apply else "⚪ ВЫКЛ"
    await callback.answer(f"Авто-отклик: {status}")
    await callback.message.edit_text(
        _settings_text(_scheduler.is_paused, _scheduler.auto_apply),
        parse_mode="HTML",
        reply_markup=settings_keyboard(_scheduler.is_paused, _scheduler.auto_apply),
    )


@router.callback_query(F.data == "force_search")
@admin_only
async def cb_force_search(callback: CallbackQuery, **kw):
    await callback.answer("🔄 Запускаю поиск...")
    from app.workers.vacancy_worker import run_vacancy_search
    count = await run_vacancy_search()
    await callback.message.answer(f"🔍 Найдено <b>{count}</b> новых вакансий", parse_mode="HTML")


@router.callback_query(F.data == "show_balance")
@admin_only
async def cb_show_balance(callback: CallbackQuery, **kw):
    await _send_balance(callback)


@router.callback_query(F.data == "bump_resume")
@admin_only
async def cb_bump_resume(callback: CallbackQuery, **kw):
    await callback.answer("⬆️ Поднимаю резюме...")
    from app.parsers.hh_playwright import hh_playwright
    if not hh_playwright:
        await callback.message.answer("❌ Playwright не доступен")
        return
    count = await hh_playwright.bump_resumes()
    if count > 0:
        await callback.message.answer(f"✅ Поднято резюме: {count}")
    else:
        await callback.message.answer("ℹ️ Резюме нельзя поднять сейчас (попробуй через 4 часа)")


@router.callback_query(F.data == "thank_rejections")
@admin_only
async def cb_thank_rejections(callback: CallbackQuery, **kw):
    await callback.answer("💬 Отправляю благодарности...")
    from app.workers.message_worker import process_rejection_thanks
    count = await process_rejection_thanks(max_count=3)
    await callback.message.answer(f"✅ Отправлено сообщений: {count}")


@router.callback_query(F.data.startswith("cancel_apply:"))
@admin_only
async def cb_cancel_apply(callback: CallbackQuery, **kw):
    await callback.answer("Отменено")
    await callback.message.delete()


# ══════════════════════════════════════════════════════════════
#  PLAYWRIGHT / HH.RU LOGIN
# ══════════════════════════════════════════════════════════════

@router.message(Command("login"))
@admin_only
async def cmd_login(message: Message, **kw):
    """Вручную залогиниться на hh.ru через Playwright."""
    from app.parsers.hh import HHParser
    parser = HHParser()
    pw = parser._get_playwright()

    if not pw:
        await message.answer(
            "❌ <b>Playwright не доступен</b>\n\n"
            "Для входа на hh.ru нужен Playwright.\n"
            "Разверните бота на VPS с установленным Chromium.",
            parse_mode="HTML",
        )
        return

    await message.answer("🔐 Выполняю вход на hh.ru...")
    success = await pw.login()

    if success:
        await message.answer("✅ Успешный вход на hh.ru! Отклики и сообщения доступны.")
    else:
        await message.answer(
            "❌ Не удалось войти на hh.ru\n\n"
            "Проверьте:\n"
            "• HH_LOGIN и HH_PASSWORD в .env\n"
            "• Возможно требуется капча (попробуйте позже)\n"
            "• Может потребоваться ручной вход через VNC",
        )
        # Send debug screenshot if available
        from pathlib import Path
        from aiogram.types import FSInputFile
        for name in ("debug_login_failed.png", "debug_login_check.png"):
            p = Path(f"data/{name}")
            if p.exists():
                try:
                    await message.answer_photo(FSInputFile(p), caption=f"🖼 {name}")
                except Exception:
                    pass


@router.message(Command("negotiations"))
@admin_only
async def cmd_negotiations(message: Message, **kw):
    """Проверить статусы откликов на hh.ru."""
    from app.parsers.hh import HHParser
    parser = HHParser()
    pw = parser._get_playwright()

    if not pw:
        await message.answer("❌ Playwright не доступен")
        return

    await message.answer("🔄 Проверяю отклики...")
    statuses = await parser.check_negotiations()

    if not statuses:
        await message.answer("📭 Нет активных откликов или не удалось загрузить")
        return

    # Group by tab
    invites = [s for s in statuses if s.get("tab") == "invitations"]
    discards = [s for s in statuses if s.get("tab") == "discard"]
    active = [s for s in statuses if s.get("tab") == "active"]

    text_parts = ["📋 <b>Статусы откликов hh.ru</b>\n"]

    if invites:
        text_parts.append(f"\n🎉 <b>Приглашения ({len(invites)}):</b>")
        for s in invites[:5]:
            text_parts.append(f"  • {s['title'][:50]} — {s['company']}")

    if active:
        text_parts.append(f"\n📨 <b>Активные ({len(active)}):</b>")
        for s in active[:5]:
            text_parts.append(f"  • {s['title'][:50]} — {s['status']}")

    if discards:
        text_parts.append(f"\n❌ <b>Отказы ({len(discards)}):</b>")
        for s in discards[:5]:
            text_parts.append(f"  • {s['title'][:50]} — {s['company']}")

    await message.answer("\n".join(text_parts), parse_mode="HTML")
