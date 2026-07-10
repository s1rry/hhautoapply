"""
UI настройки автоотклика ("Задача") для мультиюзерного режима (Фаза 5).

/task показывает меню: ключевые слова, регион, формат, опыт, занятость,
зарплата, слова-исключения, ИИ-письма, лимит, расписание, тумблер автоотклика
и кнопку тарифа. Множественный выбор — переключатели, числа/текст — ввод (FSM).
"""
from __future__ import annotations

import structlog
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.bot.media import send_photo_or_text
from app.models.application import Application, ApplicationStatus
from app.models.user_settings import UserSettings
from app.services.user_service import get_or_create_user

log = structlog.get_logger()

router = Router()

WORK_FORMAT = {"ON_SITE": "На месте", "REMOTE": "Удалённо", "HYBRID": "Гибрид", "FIELD_WORK": "Разъездной"}
EXPERIENCE = {"noExperience": "Нет опыта", "between1And3": "1–3 года", "between3And6": "3–6 лет", "moreThan6": "6+ лет"}
EMPLOYMENT = {"full": "Полная", "part": "Частичная", "project": "Проект", "probation": "Стажировка"}
GROUPS = {"fmt": ("work_format", WORK_FORMAT), "exp": ("experience", EXPERIENCE), "emp": ("employment", EMPLOYMENT)}

AREAS = {
    "москва": 1, "санкт-петербург": 2, "спб": 2, "питер": 2, "россия": 113, "вся россия": 113,
    "новосибирск": 4, "екатеринбург": 3, "казань": 88, "кемерово": 1229,
    "нижний новгород": 66, "краснодар": 53, "самара": 78, "ростов-на-дону": 76,
}
AREA_NAMES = {1: "Москва", 2: "Санкт-Петербург", 113: "Вся Россия", 4: "Новосибирск",
              3: "Екатеринбург", 88: "Казань", 1229: "Кемерово", 66: "Нижний Новгород",
              53: "Краснодар", 78: "Самара", 76: "Ростов-на-Дону"}

FREE_DAILY_LIMIT = 50
PAID_DAILY_LIMIT = 200
ADMIN_DAILY_LIMIT = 1000  # админ не ограничен тарифом


def _is_admin(telegram_id: int) -> bool:
    return str(telegram_id) == str(settings.tg_admin_chat_id or "")


def _limit_cap(user) -> int:
    if _is_admin(user.telegram_id):
        return ADMIN_DAILY_LIMIT
    return PAID_DAILY_LIMIT if user.is_paid else FREE_DAILY_LIMIT

LETTER_MODES = {"always": "всегда", "required": "только где требуется", "off": "без писем"}

BTN_TASK = "📋 Задача"
BTN_STATS = "📊 Статистика"
BTN_SETTINGS = "⚙️ Настройки"
BTN_SUPPORT = "🆘 Поддержка"
BTN_PROJECTS = "🚀 Другие проекты"


def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TASK), KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_SUPPORT), KeyboardButton(text=BTN_PROJECTS)],
        ],
        resize_keyboard=True,
    )


class TaskInput(StatesGroup):
    value = State()


async def _load(session, cb_or_msg):
    tg = cb_or_msg.from_user
    return await get_or_create_user(session, tg.id, tg.username)


def _summary(s: UserSettings) -> str:
    areas = ", ".join(AREA_NAMES.get(a, str(a)) for a in s.areas) or "не задан"
    fmt = ", ".join(WORK_FORMAT[c] for c in s.work_format) or "любой"
    exp = ", ".join(EXPERIENCE[c] for c in s.experience) or "любой"
    emp = ", ".join(EMPLOYMENT[c] for c in s.employment) or "любая"
    return (
        f"🔑 Ключевые слова: <b>{s.search_text or '⚠️ не задано (укажи!)'}</b>\n"
        f"📍 Регион: <b>{areas}</b>\n"
        f"💻 Формат: <b>{fmt}</b>\n"
        f"📈 Опыт: <b>{exp}</b>\n"
        f"🕒 Занятость: <b>{emp}</b>\n"
        f"💰 Зарплата от: <b>{s.salary_min or '—'}</b>\n"
        f"🚫 Исключения: <b>{s.excluded_text or '—'}</b>\n"
        f"✉️ Письма: <b>{LETTER_MODES.get(s.letter_mode, s.letter_mode)}</b>"
        f"{' + ИИ' if s.ai_enabled else ''}\n"
        f"📊 Лимит/день: <b>{s.daily_limit}</b>\n"
        f"⏰ Окно: <b>{s.apply_hour_start}:00–{s.apply_hour_end}:00 МСК</b>"
    )


def _main_kb(is_active: bool, s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text=("⏸ Остановить автоотклик" if is_active else "▶️ Запустить автоотклик"),
           callback_data="task:toggle_active")],
        [b(text=f"📈 Поднятие резюме: {'вкл' if s.resume_bump else 'выкл'}",
           callback_data="task:toggle_bump")],
        [b(text="🔑 Ключевые слова", callback_data="task:input:search_text"),
         b(text="📍 Регион", callback_data="task:input:areas")],
        [b(text="💻 Формат", callback_data="task:sub:fmt"),
         b(text="📈 Опыт", callback_data="task:sub:exp")],
        [b(text="🕒 Занятость", callback_data="task:sub:emp"),
         b(text="💰 Зарплата", callback_data="task:input:salary_min")],
        [b(text="🚫 Исключения", callback_data="task:input:excluded_text"),
         b(text="📊 Лимит/день", callback_data="task:input:daily_limit")],
        [b(text="⏰ Расписание", callback_data="task:input:window"),
         b(text="✉️ Письма", callback_data="task:letters")],
    ])


async def _show_main(target, s: UserSettings, is_active: bool, edit=False):
    text = "⚙️ <b>Задача автоотклика</b>\n\n" + _summary(s)
    kb = _main_kb(is_active, s)
    if edit and isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, **kw):
    await state.clear()
    async with async_session() as session:
        user = await _load(session, message)
        connected = user.hh_connected
        s, active = user.get_settings(), user.is_active
    if not connected:
        await send_photo_or_text(
            message, "welcome",
            "🔥 <b>Работай эффективнее 99% кандидатов и находи работу быстрее!</b>\n\n"
            "🤖 Персональный AI-ассистент по поиску работы на hh.ru. Работает 24/7, "
            "чтобы ты не упустил ни одной подходящей вакансии.\n\n"
            "<b>Что умеет:</b>\n"
            "⚡️ Авто-отклики на hh.ru по твоим фильтрам\n"
            "✉️ Персональные сопроводительные письма (ИИ)\n"
            "📈 Авто-поднятие резюме\n"
            "📊 Статистику откликов\n\n"
            "Сейчас бета-тест — бесплатно ❤️\n\n"
            "<b>Как начать:</b>\n"
            "1️⃣ Подключи hh.ru — /connect\n"
            "2️⃣ Настрой задачу — кнопка 📋 Задача внизу",
            reply_markup=main_reply_kb(),
        )
    else:
        async with async_session() as session:
            user = await _load(session, message)
            today = (await session.execute(
                select(func.count(Application.id)).where(
                    Application.user_id == user.id,
                    Application.status == ApplicationStatus.SENT,
                    func.date(Application.created_at) == func.current_date())
            )).scalar() or 0
        status = "🟢 работает" if active else "⚪️ остановлен"
        kw = s.search_text or "⚠️ не задано"
        await send_photo_or_text(
            message, "welcome",
            "👋 <b>С возвращением!</b>\n\n"
            f"🤖 Автоотклик: <b>{status}</b>\n"
            f"🔑 Ключевые слова: <b>{kw}</b>\n"
            f"📊 Откликов сегодня: <b>{today}</b>\n\n"
            "Управление — кнопками внизу 👇\n"
            "📋 <b>Задача</b> — фильтры и запуск · 📊 <b>Статистика</b> · ⚙️ <b>Настройки</b>",
            reply_markup=main_reply_kb(),
        )


# ── Нижние кнопки ──
@router.message(F.text == BTN_TASK)
async def btn_task(message: Message, state: FSMContext, **kw):
    await cmd_task(message, state)


@router.message(F.text == BTN_STATS)
async def btn_stats(message: Message, **kw):
    async with async_session() as session:
        user = await _load(session, message)
        total = (await session.execute(
            select(func.count(Application.id)).where(
                Application.user_id == user.id, Application.status == ApplicationStatus.SENT)
        )).scalar() or 0
        today = (await session.execute(
            select(func.count(Application.id)).where(
                Application.user_id == user.id, Application.status == ApplicationStatus.SENT,
                func.date(Application.created_at) == func.current_date())
        )).scalar() or 0
        active = user.is_active
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Откликов всего: <b>{total}</b>\n"
        f"Сегодня: <b>{today}</b>\n"
        f"Автоотклик: <b>{'работает' if active else 'остановлен'}</b>",
        parse_mode="HTML",
    )


@router.message(F.text == BTN_SETTINGS)
async def btn_settings(message: Message, **kw):
    async with async_session() as session:
        user = await _load(session, message)
        connected = user.hh_connected
        paid = user.is_paid
        resume_line = (user.resume_text.splitlines()[0] if user.resume_text else "")
        contact = (user.get_settings().contact or "").strip()
    if not connected:
        await message.answer("🔗 hh.ru пока не подключён. Нажми /connect.")
        return
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🔗 Аккаунтов hh подключено: <b>1</b>\n"
        f"📄 {resume_line or 'резюме загружено'}\n"
        f"✉️ Контакт для писем: <b>{contact or 'личный ТГ (по умолчанию)'}</b>\n"
        f"💎 Тариф: <b>{'Расширенный' if paid else 'Бесплатный'}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Контакт для писем", callback_data="task:input:contact")],
        [InlineKeyboardButton(text="📨 Пересылка сообщений (2-й ТГ)", callback_data="ub:menu")],
        [InlineKeyboardButton(text="➕ Подключить ещё аккаунт", callback_data="acc:add")],
        [InlineKeyboardButton(text="🚪 Выйти из аккаунта hh", callback_data="acc:logout")],
        [InlineKeyboardButton(text="💎 Тариф", callback_data="task:tariff")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "acc:add")
async def cb_acc_add(cb: CallbackQuery, **kw):
    await cb.answer()
    await cb.message.answer(
        "Несколько hh-аккаунтов одновременно — функция расширенного тарифа (скоро). "
        "Сейчас можно переподключить текущий: /connect"
    )


@router.callback_query(F.data == "acc:logout")
async def cb_acc_logout(cb: CallbackQuery, **kw):
    """Подтверждение выхода. С несколькими аккаунтами здесь будет выбор,
    из какого именно выйти; сейчас аккаунт один."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚪 Да, выйти из аккаунта hh", callback_data="acc:logout_yes")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="task:menu")],
    ])
    await cb.message.edit_text(
        "Выйти из подключённого аккаунта hh?\n\n"
        "Токен будет удалён, автоотклик остановится. Резюме и настройки сохранятся. "
        "Заново — /connect.",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data == "acc:logout_yes")
async def cb_acc_logout_yes(cb: CallbackQuery, **kw):
    async with async_session() as session:
        user = await _load(session, cb)
        user.hh_connected = False
        user.hh_access_token = None
        user.hh_refresh_token = None
        user.hh_token_expires = None
        user.hh_resume_id = None
        user.is_active = False
        await session.commit()
    await cb.message.edit_text("🚪 Вышел из аккаунта hh. Подключить снова — /connect.")
    await cb.answer("Готово")


@router.message(F.text == BTN_SUPPORT)
async def btn_support(message: Message, **kw):
    await send_photo_or_text(
        message, "support",
        f"🆘 <b>Поддержка</b>\n\nПо любым вопросам пиши: {settings.support_contact}",
    )


@router.message(F.text == BTN_PROJECTS)
async def btn_projects(message: Message, **kw):
    await message.answer(
        "🚀 <b>Другие проекты</b>\n\n"
        "🌊 <b>Volna VPN</b> — быстрый VPN без ограничений: @volnabbot",
        parse_mode="HTML",
    )


@router.message(Command("task"))
async def cmd_task(message: Message, state: FSMContext, **kw):
    await state.clear()
    async with async_session() as session:
        user = await _load(session, message)
        if not user.hh_connected:
            await message.answer("Сначала подключи hh.ru: /connect")
            return
        await _show_main(message, user.get_settings(), user.is_active)


@router.callback_query(F.data == "task:menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext, **kw):
    await state.clear()
    async with async_session() as session:
        user = await _load(session, cb)
        await _show_main(cb, user.get_settings(), user.is_active, edit=True)
    await cb.answer()


@router.callback_query(F.data == "task:toggle_active")
async def cb_toggle_active(cb: CallbackQuery, **kw):
    async with async_session() as session:
        user = await _load(session, cb)
        if not user.hh_connected:
            await cb.answer("Сначала подключи hh: /connect", show_alert=True)
            return
        if not user.is_active and not (user.get_settings().search_text or "").strip():
            await cb.answer("Сначала укажи «Ключевые слова», иначе бот откликнется на всё подряд.", show_alert=True)
            return
        user.is_active = not user.is_active
        await session.commit()
        state_on = user.is_active
        s = user.get_settings()
    await _show_main(cb, s, state_on, edit=True)
    await cb.answer("Автоотклик запущен" if state_on else "Автоотклик остановлен")


# ── Подменю писем ──
def _letters_kb(s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton

    def mode_btn(code: str):
        mark = "🔘 " if s.letter_mode == code else "▫️ "
        return b(text=mark + LETTER_MODES[code].capitalize(), callback_data=f"task:lmode:{code}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [mode_btn("always")],
        [mode_btn("required")],
        [mode_btn("off")],
        [b(text=f"🤖 ИИ-персонализация: {'вкл' if s.ai_enabled else 'выкл'}", callback_data="task:toggle_ai")],
        [b(text=("📝 Свой промт: задан" if s.ai_custom_prompt else "📝 Свой промт: стандартный"),
           callback_data="task:input:ai_custom_prompt")],
        [b(text="⬅️ Назад", callback_data="task:menu")],
    ])


async def _show_letters(cb: CallbackQuery, s: UserSettings):
    text = (
        "✉️ <b>Сопроводительные письма</b>\n\n"
        "• <b>Режим</b> — прикладывать письмо всегда, только где вакансия требует, или совсем без писем.\n"
        "• <b>ИИ-персонализация</b> — письмо под каждую вакансию по твоему резюме (иначе шаблон).\n"
        "• <b>Свой промт</b> — задать, как ИИ должен писать письма."
    )
    await cb.message.edit_text(text, reply_markup=_letters_kb(s), parse_mode="HTML")


@router.callback_query(F.data == "task:letters")
async def cb_letters(cb: CallbackQuery, **kw):
    async with async_session() as session:
        user = await _load(session, cb)
        s = user.get_settings()
    await _show_letters(cb, s)
    await cb.answer()


@router.callback_query(F.data.startswith("task:lmode:"))
async def cb_lmode(cb: CallbackQuery, **kw):
    mode = cb.data.split(":")[2]
    async with async_session() as session:
        user = await _load(session, cb)
        s = user.get_settings()
        s.letter_mode = mode
        user.set_settings(s)
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=_letters_kb(s))
    await cb.answer(LETTER_MODES.get(mode, mode))


@router.callback_query(F.data == "task:toggle_bump")
async def cb_toggle_bump(cb: CallbackQuery, **kw):
    async with async_session() as session:
        user = await _load(session, cb)
        s = user.get_settings()
        s.resume_bump = not s.resume_bump
        user.set_settings(s)
        await session.commit()
        active = user.is_active
    await _show_main(cb, s, active, edit=True)
    await cb.answer("Поднятие резюме включено" if s.resume_bump else "Выключено")


@router.callback_query(F.data == "task:toggle_ai")
async def cb_toggle_ai(cb: CallbackQuery, **kw):
    async with async_session() as session:
        user = await _load(session, cb)
        s = user.get_settings()
        s.ai_enabled = not s.ai_enabled
        user.set_settings(s)
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=_letters_kb(s))
    await cb.answer("ИИ включён" if s.ai_enabled else "ИИ выключен")


# ── Подменю множественного выбора (формат/опыт/занятость) ──
def _sub_kb(group: str, selected: list[str]) -> InlineKeyboardMarkup:
    field, options = GROUPS[group]
    rows = []
    for code, label in options.items():
        mark = "✅ " if code in selected else "▫️ "
        rows.append([InlineKeyboardButton(text=mark + label, callback_data=f"task:tog:{group}:{code}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="task:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("task:sub:"))
async def cb_sub(cb: CallbackQuery, **kw):
    group = cb.data.split(":")[2]
    field, _ = GROUPS[group]
    async with async_session() as session:
        user = await _load(session, cb)
        selected = getattr(user.get_settings(), field)
    await cb.message.edit_text(
        "Отметь нужные варианты (можно несколько):",
        reply_markup=_sub_kb(group, selected),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("task:tog:"))
async def cb_tog(cb: CallbackQuery, **kw):
    _, _, group, code = cb.data.split(":")
    field, _ = GROUPS[group]
    async with async_session() as session:
        user = await _load(session, cb)
        s = user.get_settings()
        cur = list(getattr(s, field))
        if code in cur:
            cur.remove(code)
        else:
            cur.append(code)
        setattr(s, field, cur)
        user.set_settings(s)
        await session.commit()
        selected = cur
    await cb.message.edit_reply_markup(reply_markup=_sub_kb(group, selected))
    await cb.answer()


# ── Ввод значений (FSM) ──
_PROMPTS = {
    "search_text": "Пришли ключевые слова для поиска (например: <code>системный аналитик</code>). Пусто = по умолчанию.",
    "areas": "Пришли город (например: <code>Москва</code>, <code>СПб</code>, <code>вся Россия</code>).",
    "salary_min": "Пришли минимальную зарплату числом (например: <code>200000</code>). 0 = без ограничения.",
    "excluded_text": "Пришли слова-исключения через запятую (например: <code>1С, junior</code>).",
    "daily_limit": "Пришли лимит откликов в день числом.",
    "window": "Пришли окно откликов в формате <code>9-21</code> (часы МСК). Круглосуточно — пришли <code>0-24</code>.",
    "ai_custom_prompt": "Пришли свой промт для ИИ-писем (как писать сопроводительное). Пусто/<code>-</code> — вернуть стандартный.",
    "contact": "Пришли контакт для сопроводительных писем — его увидит HR вместо твоего личного ТГ "
               "(например второй ТГ-аккаунт <code>@my_work_tg</code>, почта или телефон). "
               "Пусто/<code>-</code> — убрать контакт.",
}


@router.callback_query(F.data.startswith("task:input:"))
async def cb_input(cb: CallbackQuery, state: FSMContext, **kw):
    field = cb.data.split(":")[2]
    await state.set_state(TaskInput.value)
    await state.update_data(field=field)
    await cb.message.answer(_PROMPTS.get(field, "Пришли значение:") + "\n\nОтмена: /task", parse_mode="HTML")
    await cb.answer()


@router.message(TaskInput.value)
async def on_value(message: Message, state: FSMContext, **kw):
    data = await state.get_data()
    field = data.get("field")
    raw = (message.text or "").strip()
    err = None
    async with async_session() as session:
        user = await _load(session, message)
        s = user.get_settings()
        if field == "search_text":
            s.search_text = raw
        elif field == "ai_custom_prompt":
            s.ai_custom_prompt = "" if raw in ("", "-") else raw
        elif field == "contact":
            s.contact = "" if raw in ("", "-") else raw
        elif field == "excluded_text":
            s.excluded_text = raw
        elif field == "areas":
            aid = AREAS.get(raw.lower())
            if aid:
                s.areas = [aid]
            else:
                err = "Не узнал город. Попробуй: Москва, СПб, вся Россия и т.п."
        elif field == "salary_min":
            if raw.isdigit():
                s.salary_min = int(raw)
            else:
                err = "Нужно число."
        elif field == "daily_limit":
            if raw.isdigit():
                cap = _limit_cap(user)
                val = min(int(raw), cap)
                s.daily_limit = val
                if int(raw) > cap and not _is_admin(user.telegram_id) and not user.is_paid:
                    err = f"На бесплатном тарифе максимум {FREE_DAILY_LIMIT}/день. Поставил {val}. Больше — в тарифе 💎"
            else:
                err = "Нужно число."
        elif field == "window":
            try:
                a, b = raw.replace(" ", "").split("-")
                a, b = int(a), int(b)
                if 0 <= a < b <= 24:
                    s.apply_hour_start, s.apply_hour_end = a, b
                else:
                    err = "Диапазон должен быть 0–24 и начало меньше конца."
            except Exception:
                err = "Формат: 9-21"
        if err is None or field in ("daily_limit",):
            user.set_settings(s)
            await session.commit()
        active = user.is_active
        s_final = user.get_settings()
    await state.clear()
    if err:
        await message.answer("⚠️ " + err)
    await _show_main(message, s_final, active)


@router.callback_query(F.data == "task:tariff")
async def cb_tariff(cb: CallbackQuery, **kw):
    async with async_session() as session:
        user = await _load(session, cb)
        paid = user.is_paid
    text = (
        "💎 <b>Тариф</b>\n\n"
        f"Сейчас: <b>{'Расширенный' if paid else 'Бесплатный'}</b>\n\n"
        "Бесплатный:\n"
        f"• 1 аккаунт hh, до {FREE_DAILY_LIMIT} откликов/день\n"
        "• ИИ-письма, статистика, анализ вакансий\n\n"
        "Расширенный (100₽/мес):\n"
        "• несколько hh-аккаунтов\n"
        f"• до {PAID_DAILY_LIMIT} откликов/день\n"
        "• чаще проверка и приоритет\n"
        "• расширенная статистика и напоминания\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оформить за 100₽", callback_data="pay:start")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="task:menu")],
    ])
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()
