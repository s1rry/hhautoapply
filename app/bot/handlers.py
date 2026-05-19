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
        # By-platform vacancy counts
        platform_rows = (await session.execute(
            select(Vacancy.platform, func.count(Vacancy.id))
            .group_by(Vacancy.platform)
        )).all()
        platform_vac = {p: c for p, c in platform_rows}

        # By-platform application counts (today + total sent)
        app_today_rows = (await session.execute(
            select(Application.platform, func.count(Application.id))
            .where(
                Application.status == ApplicationStatus.SENT,
                func.date(Application.created_at) == func.current_date(),
            )
            .group_by(Application.platform)
        )).all()
        app_today = {p: c for p, c in app_today_rows}

        app_total_rows = (await session.execute(
            select(Application.platform, func.count(Application.id))
            .where(Application.status == ApplicationStatus.SENT)
            .group_by(Application.platform)
        )).all()
        app_total = {p: c for p, c in app_total_rows}

        failed_today = await session.scalar(
            select(func.count(Application.id)).where(
                Application.status == ApplicationStatus.FAILED,
                func.date(Application.created_at) == func.current_date(),
            )
        ) or 0

        new = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status == VacancyStatus.NEW)
        ) or 0
        analyzed = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status == VacancyStatus.ANALYZED)
        ) or 0
        approved = await session.scalar(
            select(func.count(Vacancy.id)).where(Vacancy.status == VacancyStatus.APPROVED)
        ) or 0

        # Recruiter messages by platform
        msg_rows = (await session.execute(
            select(RecruiterMessage.platform, func.count(RecruiterMessage.id))
            .group_by(RecruiterMessage.platform)
        )).all()
        msg_by_plat = {p: c for p, c in msg_rows}

        avg_score = await session.scalar(
            select(func.avg(Vacancy.ai_score)).where(Vacancy.ai_score.is_not(None))
        )

    score_text = f"{avg_score:.0f}" if avg_score else "—"

    PLATFORMS = [
        ("hh", "hh.ru", settings.max_applies_per_day_hh),
        ("habr", "Хабр Карьера", settings.max_applies_per_day_habr),
        ("avito", "Авито", 0),
    ]
    by_plat_lines = []
    for code, label, cap in PLATFORMS:
        v = platform_vac.get(code, 0)
        t = app_today.get(code, 0)
        tt = app_total.get(code, 0)
        m = msg_by_plat.get(code, 0)
        if cap == 0 and v == 0 and tt == 0:
            by_plat_lines.append(f"<b>{label}</b> — (отключено)")
            continue
        cap_txt = f"/{cap}" if cap else ""
        by_plat_lines.append(
            f"<b>{label}</b>\n"
            f"  📦 Вакансий в БД: {v}\n"
            f"  📨 Отклики сегодня: <b>{t}{cap_txt}</b>\n"
            f"  📨 Откликов всего: {tt}\n"
            f"  💬 Сообщений рекрутеров: {m}"
        )

    total_vac = sum(platform_vac.values())
    total_today = sum(app_today.values())
    total_all = sum(app_total.values())
    total_cap = settings.max_applies_per_day_hh + settings.max_applies_per_day_habr

    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"📦 Всего вакансий: <b>{total_vac}</b>\n"
        f"🆕 Новые: <b>{new}</b>\n"
        f"🤖 Проанализировано: <b>{analyzed}</b>\n"
        f"⭐ Одобрено AI: <b>{approved}</b>\n"
        f"📈 Средний AI-скор: <b>{score_text}</b>\n\n"
        f"📨 <b>Отклики (всего):</b>\n"
        f"  • Сегодня: <b>{total_today}/{total_cap}</b>\n"
        f"  • Ошибок сегодня: <b>{failed_today}</b>\n"
        f"  • Всего отправлено: <b>{total_all}</b>\n\n"
        "🏷 <b>По платформам:</b>\n\n"
        + "\n\n".join(by_plat_lines),
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
    await message.answer("🔄 Проверяю статусы откликов на hh.ru...")
    from app.parsers.hh_playwright import hh_playwright
    if not hh_playwright:
        await message.answer("❌ Playwright не доступен")
        return

    statuses = await hh_playwright.check_negotiations_status()

    if not statuses:
        await message.answer("📭 Нет активных откликов")
        return

    invites = [s for s in statuses if s.get("tab") == "invitations"]
    discards = [s for s in statuses if s.get("tab") == "discard"]
    pending = [s for s in statuses if s.get("tab") == "pending"]

    parts = ["📩 <b>Сообщения и отклики hh.ru</b>"]
    parts.append(f"\n🎉 Приглашения и собеседования: <b>{len(invites)}</b>")
    parts.append(f"❌ Отказы: <b>{len(discards)}</b>")
    parts.append(f"⏳ Без ответа: <b>{len(pending)}</b>")

    if invites:
        parts.append("\n🎉 <b>Приглашения / собеседования:</b>")
        for s in invites[:15]:
            parts.append(f"  • {s['title'][:60]}\n    🏢 {s['company']} — {s.get('status','')[:50]}")

    if discards:
        parts.append("\n❌ <b>Отказы:</b>")
        for s in discards[:15]:
            parts.append(f"  • {s['title'][:60]} — {s['company'][:30]}")

    text = "\n".join(parts)
    # Telegram limit ~4096 chars
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    await message.answer(text, parse_mode="HTML")


def _settings_text(paused: bool, auto: bool) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"📍 Позиция: {settings.desired_position}\n"
        f"💰 Зарплата: {settings.desired_salary_min:,}–{settings.desired_salary_max:,}\n"
        f"⏱ Интервал поиска: {settings.check_interval_sec // 60} мин\n"
        f"🎯 Лимит откликов/день:\n"
        f"   • hh.ru: <b>{settings.max_applies_per_day_hh}</b>\n"
        f"   • Хабр Карьера: <b>{settings.max_applies_per_day_habr}</b>\n"
        f"⏱ Задержка между откликами: {settings.apply_delay_min}–{settings.apply_delay_max} сек\n"
        f"⌨️ Скорость печати: {settings.type_delay_min}–{settings.type_delay_max} мс/символ\n"
        f"🔔 Уведомления: {settings.notify_hour_start}:00–{settings.notify_hour_end}:00 МСК\n\n"
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
    # Show only enabled platforms (habr cap=0 means disabled)
    enabled = ["hh"]
    if settings.max_applies_per_day_habr > 0:
        enabled.append("habr")
    async with async_session() as session:
        result = await session.execute(
            select(Application)
            .where(Application.platform.in_(enabled))
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


async def _fetch_balance(base_url: str, api_key: str) -> dict | None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base_url}/v1/balance",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


# Providers that don't expose /v1/balance — only dashboard
_DASHBOARD_URLS = {
    "api.tonwave.dev": "https://tonwave.dev/dashboard",
    "waveapi.tonvarex.ru": "https://wave.tonvarex.ru/dashboard",
}


def _provider_name(base_url: str) -> str:
    if "tonwave" in base_url:
        return "TonWave"
    if "tonvarex" in base_url or "waveapi" in base_url:
        return "WaveAPI"
    return base_url


def _dashboard_for(base_url: str) -> str | None:
    for host, url in _DASHBOARD_URLS.items():
        if host in base_url:
            return url
    return None


def _format_provider(label: str, base_url: str, data: dict | None) -> str:
    name = _provider_name(base_url)
    dash = _dashboard_for(base_url)
    if not data:
        if dash:
            return f"<b>{label} — {name}</b>\n  ℹ️ Баланс через API недоступен.\n  🔗 <a href=\"{dash}\">Открыть дашборд</a>"
        return f"<b>{label} — {name}</b>\n  ❌ нет ответа от {base_url}"
    balance = data.get("balance_cents", 0)
    inp = data.get("total_input_tokens", 0)
    out = data.get("total_output_tokens", 0)
    total = data.get("total_tokens_used", 0)
    return (
        f"<b>{label} — {name}</b>\n"
        f"  💰 {balance} центов (${balance/100:.2f})\n"
        f"  📥 in: <b>{inp:,}</b>\n"
        f"  📤 out: <b>{out:,}</b>\n"
        f"  📊 total: <b>{total:,}</b>"
    )


async def _send_balance(target):
    """Отправить баланс AI — основной + резерв."""
    parts = ["💎 <b>Балансы AI</b>"]

    primary_data = await _fetch_balance(settings.anthropic_base_url, settings.anthropic_api_key)
    parts.append(_format_provider("Основной", settings.anthropic_base_url, primary_data))

    if settings.anthropic_fallback_api_key:
        fb_data = await _fetch_balance(settings.anthropic_fallback_base_url, settings.anthropic_fallback_api_key)
        parts.append(_format_provider("Резерв", settings.anthropic_fallback_base_url, fb_data))

    text = "\n\n".join(parts)
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", disable_web_page_preview=True)


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
    async with async_session() as session:
        m = await session.get(RecruiterMessage, msg_id)
        plat = m.platform if m else ""
    reply, _, _ = await claude_ai.generate_reply(msg_text, platform=plat)

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
    _scheduler.set_auto_apply(not _scheduler.auto_apply)
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
    await callback.message.answer(f"Отправлено сообщений: {count}")
    # Always send diagnostic screenshots so we can see what hh.ru showed
    from pathlib import Path
    from aiogram.types import FSInputFile
    for name in (
        "debug_thanks_step1_home.png",
        "debug_thanks_step2_no_activator.png",
        "debug_thanks_step2_widget_open.png",
        "debug_thanks_step3_no_chats.png",
        "debug_thanks_step3_chat_open.png",
        "debug_thanks_step3_no_input.png",
        "debug_thanks_step4_filled.png",
        "debug_thanks_step5_no_send_btn.png",
        "debug_thanks_step6_after_send.png",
        "debug_thanks_overall_error.png",
    ):
        p = Path(f"data/{name}")
        if p.exists():
            try:
                await callback.message.answer_photo(FSInputFile(p), caption=name[6:-4])
            except Exception:
                pass


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


@router.message(Command("test_apply"))
@admin_only
async def cmd_test_apply(message: Message, **kw):
    """Run N test applies on hh with full screenshots in TG.
    Usage: /test_apply [count]   default 10
    """
    parts = (message.text or "").split()
    n = 10
    if len(parts) > 1:
        try:
            n = max(1, min(int(parts[1]), 20))
        except ValueError:
            pass

    await message.answer(f"🧪 Запускаю тест-отклики hh: {n} штук со скриншотами. Это займёт ~{n*2} мин.")

    async with async_session() as session:
        result = await session.execute(
            select(Vacancy)
            .options(selectinload(Vacancy.company))
            .where(
                Vacancy.platform == "hh",
                Vacancy.status == VacancyStatus.APPROVED,
                Vacancy.ai_score >= 70,
            )
            .order_by(Vacancy.ai_score.desc())
            .limit(n)
        )
        vacancies = result.scalars().all()

    if not vacancies:
        await message.answer("❌ Нет одобренных вакансий hh для теста")
        return

    from app.parsers.hh_oauth import hh_oauth
    from app.parsers.hh_api import hh_api_client
    from app.ai.claude import claude_ai
    import asyncio as _async
    import re as _re
    from app.utils.anti_detect import random_delay
    from app.models.application import Application, ApplicationStatus

    # Pre-sync applied list so we don't re-try the same ones
    from app.workers.apply_worker import sync_applied_from_hh
    marked = await sync_applied_from_hh()
    if marked:
        await message.answer(f"🔄 Помечено уже-откликнутых: {marked}. Беру новые.")

    # Verify OAuth token works
    token = await hh_oauth.get_token()
    if not token:
        await message.answer("❌ Не удалось получить OAuth токен. Нужен VNC-логин в hh.")
        return

    stats = {"sent": 0, "already": 0, "failed": 0}
    for i, v in enumerate(vacancies, 1):
        tag = f"{i:02d}"
        title = (v.title or "")[:60]
        company = _company_name(v) or "—"

        await message.answer(f"<b>[{tag}]</b> {title}\n🏢 {company}\n🤖 Генерирую письмо...", parse_mode="HTML")

        try:
            letter, _, _ = await claude_ai.generate_cover_letter(v.title, v.description or "")
        except Exception as e:
            await message.answer(f"❌ AI ошибка: {e}")
            stats["failed"] += 1
            continue

        m_id = _re.search(r"/vacancy/(\d+)", v.url)
        vid = m_id.group(1) if m_id else v.external_id
        try:
            res, info = await _async.wait_for(hh_oauth.apply(vid, letter), timeout=20)
        except _async.TimeoutError:
            res, info = False, {"error": "timeout"}

        # Fallback to Playwright for vacancies requiring questionnaire
        if res is False and (info or {}).get("error") == "needs_test":
            await message.answer(f"📋 <b>[{tag}]</b> Опросник — переключаюсь на Playwright…", parse_mode="HTML")
            try:
                ai_letter, _, _ = await claude_ai.generate_cover_letter(v.title, v.description or "")
            except Exception:
                ai_letter = letter
            from app.parsers.hh import HHParser
            pw_parser = HHParser()
            try:
                await _async.wait_for(pw_parser.login(), timeout=60)
                res = await _async.wait_for(
                    pw_parser.apply_to_vacancy(v.url, ai_letter, screenshot_name=tag),
                    timeout=180,
                )
                info = {"path": "playwright", "result": str(res)}
            except _async.TimeoutError:
                res = False
                info = {"error": "playwright_timeout"}

        status_emoji = "✅" if res is True else ("ℹ️" if res == "already" else "❌")
        result_label = {True: "ОТПРАВЛЕНО", "already": "Уже откликались", False: "ОШИБКА"}.get(res, "ОШИБКА")
        info_str = ""
        if res is not True and info:
            short = str(info)[:200]
            info_str = f"\n<i>{short}</i>"
        await message.answer(
            f"{status_emoji} <b>[{tag}]</b> {result_label}\n🔗 {v.url}{info_str}",
            parse_mode="HTML",
        )
        # If Playwright was used — send screenshots
        if info and info.get("path") == "playwright":
            from pathlib import Path as _Path
            from aiogram.types import FSInputFile as _FSI
            for stage in ("before", "after"):
                p = _Path(f"data/test_apply_{tag}_{stage}.png")
                if p.exists():
                    try:
                        await message.answer_photo(_FSI(p), caption=f"[{tag}] {stage}")
                    except Exception:
                        pass

        if res is True:
            stats["sent"] += 1
            # Record real application
            async with async_session() as session:
                vv = await session.get(Vacancy, v.id)
                if vv:
                    vv.status = VacancyStatus.APPLIED
                    session.add(Application(
                        vacancy_id=v.id, platform="hh",
                        cover_letter=letter,
                        status=ApplicationStatus.SENT,
                        attempt_count=1,
                    ))
                    await session.commit()
        elif res == "already":
            stats["already"] += 1
            async with async_session() as session:
                vv = await session.get(Vacancy, v.id)
                if vv:
                    vv.status = VacancyStatus.APPLIED
                    await session.commit()
        else:
            stats["failed"] += 1
            async with async_session() as session:
                session.add(Application(
                    vacancy_id=v.id, platform="hh",
                    cover_letter=letter,
                    status=ApplicationStatus.FAILED,
                    attempt_count=1,
                ))
                await session.commit()

        if i < len(vacancies):
            await random_delay(settings.apply_delay_min, settings.apply_delay_max)

    await message.answer(
        f"📊 <b>Итоги теста ({len(vacancies)} попыток):</b>\n"
        f"✅ Отправлено: {stats['sent']}\n"
        f"ℹ️ Уже откликались: {stats['already']}\n"
        f"❌ Ошибки: {stats['failed']}",
        parse_mode="HTML",
    )


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
