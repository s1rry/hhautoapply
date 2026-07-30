"""
Кнопки и команды оплаты расширенного тарифа (мультиюзер, Фаза 6).

pay:start — показать варианты оплаты (ЮMoney-ссылка + крипта).
/grant <user_id> [дней] — ручное подтверждение (крипта), только админ.
"""
from __future__ import annotations

import structlog
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.database import async_session
from app.services.payments import build_yoomoney_url, apply_payment
from app.services import yookassa

log = structlog.get_logger()

router = Router()


@router.callback_query(F.data == "pay:start")
async def cb_pay_start(cb: CallbackQuery, **kw):
    price = settings.subscription_price
    days = settings.subscription_days
    lines = [f"💳 <b>Оплата расширенного тарифа</b>\n\n{price}₽ — {days} дней.\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    paid_online = False
    if yookassa.is_configured():
        url = await yookassa.create_payment(
            cb.from_user.id, price, "Расширенный тариф авто-откликов")
        if url:
            buttons.append([InlineKeyboardButton(text=f"Оплатить {price}₽ картой", url=url)])
            paid_online = True
    if not paid_online and settings.yoomoney_wallet:
        url = build_yoomoney_url(cb.from_user.id)
        buttons.append([InlineKeyboardButton(text=f"Оплатить {price}₽ картой (ЮMoney)", url=url)])
        paid_online = True
    if paid_online:
        lines.append("После оплаты тариф поднимется автоматически в течение минуты.")
    else:
        lines.append("Онлайн-оплата картой временно недоступна.")

    crypto = []
    if settings.crypto_ton:
        crypto.append(f"TON: <code>{settings.crypto_ton}</code>")
    if settings.crypto_usdt_trc20:
        crypto.append(f"USDT (TRC20): <code>{settings.crypto_usdt_trc20}</code>")
    if crypto:
        lines.append("\nИли криптой (подтверждение вручную, напиши в поддержку после оплаты):\n" + "\n".join(crypto))

    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="task:menu")])
    await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await cb.answer()


@router.message(Command("test"))
async def cmd_test(message: Message, **kw):
    """Пометить/снять тестовый аккаунт (свой бот). Исключается из воронки.
    Только админ: /test <@username или telegram_id>"""
    if str(message.chat.id) != settings.tg_admin_chat_id:
        return
    from sqlalchemy import select as _sel
    from app.models.user import User
    parts = (message.text or "").split()
    if len(parts) < 2:
        # без аргумента — показать текущие тестовые
        async with async_session() as session:
            rows = (await session.execute(
                _sel(User.username, User.telegram_id).where(User.is_test == 1))).all()
        lst = "\n".join(f"• @{un}" if un else f"• id{tg}" for un, tg in rows) or "нет"
        await message.answer("Использование: /test <@username или id>\n\n"
                             f"Сейчас помечены тестовыми:\n{lst}", parse_mode="HTML")
        return
    arg = parts[1].lstrip("@")
    async with async_session() as session:
        if arg.isdigit():
            u = (await session.execute(_sel(User).where(User.telegram_id == int(arg)))).scalar_one_or_none()
        else:
            u = (await session.execute(_sel(User).where(User.username == arg))).scalar_one_or_none()
        if not u:
            await message.answer("Пользователь не найден.")
            return
        u.is_test = 0 if u.is_test else 1
        await session.commit()
        state = "помечен ТЕСТОВЫМ (убран из воронки)" if u.is_test else "снят из тестовых"
    await message.answer(f"✅ @{u.username or u.telegram_id} {state}.")


@router.message(Command("grant"))
async def cmd_grant(message: Message, **kw):
    """Ручное поднятие тарифа (крипта). Только админ: /grant <telegram_id> [дней]."""
    if str(message.chat.id) != settings.tg_admin_chat_id:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /grant <telegram_id> [дней]")
        return
    telegram_id = int(parts[1])
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else settings.subscription_days
    async with async_session() as session:
        ok = await apply_payment(session, telegram_id, provider="crypto", amount=0, days=days)
    if ok:
        await message.answer(f"✅ Тариф поднят пользователю {telegram_id} на {days} дней.")
        try:
            await message.bot.send_message(telegram_id, "✅ Оплата подтверждена, расширенный тариф активирован. Спасибо!")
        except Exception:
            pass
    else:
        await message.answer("Не удалось (пользователь не найден).")


def _is_admin(chat_id) -> bool:
    return str(chat_id) == str(settings.tg_admin_chat_id or "")


async def _ai_report() -> str:
    """Текст проверки ключей скоринга (дёргает каждый эндпоинт реальным запросом)."""
    from app.ai.claude import claude_ai
    pool = claude_ai.score_pool
    if not pool:
        return "AI_SCORE_POOL пуст — скоринг идёт на платном провайдере."
    lines = []
    for ep in pool:
        host = ep["base_url"].split("//")[-1].split("/")[0]
        try:
            text, _, _ = await claude_ai._call_openai_compatible(
                "Ответь одним числом.", "Сколько будет 2+2? Верни только число.",
                max_tokens=800, model=ep["model"],
                base_url=ep["base_url"], api_key=ep["api_key"])
            got = (text or "").strip()[:20]
            lines.append(f"✅ {host} ({ep['model']}) → {got or 'пустой ответ'}")
        except Exception as e:
            lines.append(f"❌ {host} ({ep['model']}) → {type(e).__name__}: {str(e)[:80]}")
    return "🔑 <b>Ключи скоринга</b>\n\n" + "\n".join(lines)


async def _funnel_text() -> str:
    """Агрегированная воронка (без персональных данных). Основной пульт."""
    import datetime as _dt
    from sqlalchemy import select, func
    from app.models.user import User
    from app.models.application import Application, ApplicationStatus
    from app.models.search_task import SearchTask
    from app.models.payment import Payment
    now = _dt.datetime.now(_dt.timezone.utc)
    async with async_session() as session:
        # Исключаем из воронки тестовые аккаунты и админа — они не клиенты
        # и завышают метрики (например, тестовый платёж владельца).
        excl = set(r[0] for r in (await session.execute(
            select(User.id).where(User.is_test == 1))).all())
        arow = (await session.execute(
            select(User.id).where(User.telegram_id == int(settings.tg_admin_chat_id or 0)))).first()
        if arow:
            excl.add(arow[0])
        NOT = User.id.not_in(excl) if excl else True
        UNOT = Application.user_id.not_in(excl) if excl else True
        SNOT = SearchTask.user_id.not_in(excl) if excl else True

        async def cnt(q):
            return (await session.execute(q)).scalar() or 0
        total = await cnt(select(func.count(User.id)).where(NOT))
        # Под-воронка подключения hh (главная точка обрыва).
        started_conn = await cnt(select(func.count(User.id)).where(User.connect_stage >= 1, NOT))
        login_sent = await cnt(select(func.count(User.id)).where(User.connect_stage >= 2, NOT))
        connected = await cnt(select(func.count(User.id)).where(User.hh_connected.is_(True), NOT))
        with_task = await cnt(select(func.count(func.distinct(SearchTask.user_id))).where(SNOT))
        with_apply = await cnt(select(func.count(func.distinct(Application.user_id)))
                               .where(Application.status == ApplicationStatus.SENT, UNOT))
        with_invite = await cnt(
            select(func.count(func.distinct(SearchTask.user_id)))
            .where(func.coalesce(SearchTask.invites, 0) > 0, SNOT))
        free = await cnt(select(func.count(User.id)).where(User.tier == "free", NOT))
        # Реально заплатившие — есть успешный платёж (кроме тестовых/админа).
        payers = set(r[0] for r in (await session.execute(
            select(func.distinct(Payment.user_id))
            .where(Payment.status == "paid", Payment.user_id.not_in(excl) if excl else True))).all())
        real_paid = len(payers)
        # Пробные vs истёкшие среди paid без платежа.
        prows = (await session.execute(
            select(User.id, User.tier_until).where(User.tier == "paid", NOT))).all()
        trial_active = trial_expired = 0
        for uid, tu in prows:
            if uid in payers:
                continue
            if tu is None:
                trial_active += 1
                continue
            if tu.tzinfo is None:
                tu = tu.replace(tzinfo=_dt.timezone.utc)
            if tu > now:
                trial_active += 1
            else:
                trial_expired += 1
        survey_sent = await cnt(select(func.count(User.id)).where(User.survey_sent == 1, NOT))

    def p(n):  # доля от total
        return f"{round(n / total * 100)}%" if total else "0%"
    mrr = real_paid * settings.subscription_price
    return (
        "📊 <b>Воронка</b>\n\n"
        f"Зарегистрировано: <b>{total}</b>\n"
        f"🔌 Начали подключение: <b>{started_conn}</b> ({p(started_conn)})\n"
        f"🔑 Ввели логин (код отправлен): <b>{login_sent}</b> ({p(login_sent)})\n"
        f"🔗 Подключили hh: <b>{connected}</b> ({p(connected)})\n"
        f"📋 Создали задачу: <b>{with_task}</b> ({p(with_task)})\n"
        f"📤 Получили отклики: <b>{with_apply}</b> ({p(with_apply)})\n"
        f"🎯 Получили приглашения: <b>{with_invite}</b> ({p(with_invite)})\n\n"
        f"💎 Пробный активен: <b>{trial_active}</b>\n"
        f"⌛️ Пробный истёк: <b>{trial_expired}</b>\n"
        f"💰 Оплатили: <b>{real_paid}</b>\n"
        f"🆓 На бесплатном: <b>{free}</b>\n"
        f"📝 Опрос отправлен: <b>{survey_sent}</b>\n\n"
        f"💵 MRR (оценка): <b>{mrr} ₽</b>\n\n"
        f"⚠️ Главная точка обрыва — подключение hh: "
        f"из {total} дошли {connected}."
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, **kw):
    """Пульт админа: воронка + кнопки на клиентов и ключи ИИ."""
    if not _is_admin(message.chat.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Клиенты", callback_data="admin:clients"),
         InlineKeyboardButton(text="🔑 Ключи ИИ", callback_data="admin:ai")],
        [InlineKeyboardButton(text="🔄 Обновить воронку", callback_data="admin:funnel")],
    ])
    await message.answer(await _funnel_text(), parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin:funnel")
async def cb_admin_funnel(cb: CallbackQuery, **kw):
    if not _is_admin(cb.message.chat.id):
        return
    kb = cb.message.reply_markup
    await cb.message.edit_text(await _funnel_text(), parse_mode="HTML", reply_markup=kb)
    await cb.answer("Обновлено")


@router.callback_query(F.data == "admin:ai")
async def cb_admin_ai(cb: CallbackQuery, **kw):
    if not _is_admin(cb.message.chat.id):
        return
    await cb.answer("Проверяю ключи...")
    await cb.message.answer(await _ai_report(), parse_mode="HTML")


@router.callback_query(F.data == "admin:clients")
async def cb_admin_clients(cb: CallbackQuery, **kw):
    if not _is_admin(cb.message.chat.id):
        return
    for chunk in await _clients_chunks():
        await cb.message.answer(chunk, parse_mode="HTML")
    await cb.answer()


@router.message(Command("ai"))
async def cmd_ai(message: Message, **kw):
    """Проверить ключи скоринга. Только админ: /ai"""
    if not _is_admin(message.chat.id):
        return
    await message.answer("⏳ Проверяю ключи...")
    await message.answer(await _ai_report(), parse_mode="HTML")


async def _clients_chunks() -> list[str]:
    """Список клиентов кусками ≤3500 символов (для Telegram)."""
    import datetime as _dt
    from sqlalchemy import select, func
    from app.models.user import User
    from app.models.application import Application, ApplicationStatus
    from app.models.search_task import SearchTask
    now = _dt.datetime.now(_dt.timezone.utc)
    async with async_session() as session:
        users = (await session.execute(select(User).order_by(User.id))).scalars().all()
        # Отклики всего и за сегодня — одним запросом на всех, потом разложим.
        sent_total = dict((uid, n) for uid, n in (await session.execute(
            select(Application.user_id, func.count(Application.id))
            .where(Application.status == ApplicationStatus.SENT)
            .group_by(Application.user_id))).all())
        sent_today = dict((uid, n) for uid, n in (await session.execute(
            select(Application.user_id, func.count(Application.id))
            .where(Application.status == ApplicationStatus.SENT,
                   func.date(Application.created_at) == func.current_date())
            .group_by(Application.user_id))).all())
        invites = dict((uid, n or 0) for uid, n in (await session.execute(
            select(SearchTask.user_id, func.sum(func.coalesce(SearchTask.invites, 0)))
            .group_by(SearchTask.user_id))).all())

    total = len(users)
    paid = sum(1 for u in users if u.tier == "paid")
    connected = sum(1 for u in users if u.hh_connected)
    lines = [f"👥 <b>Клиенты: {total}</b>  •  💎 платных/пробных: {paid}  •  🔗 подключили hh: {connected}\n"]
    for u in users:
        # Срок доступа
        if u.tier == "paid" and u.tier_until:
            tu = u.tier_until
            if tu.tzinfo is None:
                tu = tu.replace(tzinfo=_dt.timezone.utc)
            days = (tu - now).days
            access = f"💎 ещё {days}д" if days >= 0 else "💎 истёк"
        else:
            access = "🆓 free"
        hh = "🔗" if u.hh_connected else "➖"
        uname = f"@{u.username}" if u.username else f"id{u.telegram_id}"
        st = sent_today.get(u.id, 0)
        tot = sent_total.get(u.id, 0)
        inv = invites.get(u.id, 0)
        lines.append(f"{hh} <b>{uname}</b> · {access} · сегодня {st}, всего {tot}, пригл. {inv}")

    text = "\n".join(lines)
    # Телеграм режет длинные сообщения — бьём на части по 3500 символов.
    chunks = []
    while text:
        chunks.append(text[:3500])
        text = text[3500:]
    return chunks


@router.message(Command("clients"))
async def cmd_clients(message: Message, **kw):
    """Сводка по клиентам: тариф, срок пробного, активность. Только админ."""
    if not _is_admin(message.chat.id):
        return
    for chunk in await _clients_chunks():
        await message.answer(chunk, parse_mode="HTML")
