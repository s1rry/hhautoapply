"""
UI настройки автоотклика ("Задача") для мультиюзерного режима (Фаза 5).

/task показывает меню: ключевые слова, регион, формат, опыт, занятость,
зарплата, слова-исключения, ИИ-письма, лимит, расписание, тумблер автоотклика
и кнопку тарифа. Множественный выбор — переключатели, числа/текст — ввод (FSM).
"""
from __future__ import annotations

import re

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
from app.models.vacancy import Vacancy, VacancyStatus
from app.models.user_settings import UserSettings
from app.services.user_service import get_or_create_user, beta_slots

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

# Источник вакансий для задачи.
SOURCE_LABELS = {"keyword": "по ключу", "recommended": "рекомендации", "both": "ключ + рекомендации"}
SOURCE_CYCLE = {"keyword": "recommended", "recommended": "both", "both": "keyword"}

BTN_TASK = "📋 Задачи"
BTN_STATS = "📊 Общая"
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


def _summary(s: UserSettings, tasks_line: str = "") -> str:
    areas = ", ".join(AREA_NAMES.get(a, str(a)) for a in s.areas) or "не задан"
    fmt = ", ".join(WORK_FORMAT[c] for c in s.work_format) or "любой"
    exp = ", ".join(EXPERIENCE[c] for c in s.experience) or "любой"
    emp = ", ".join(EMPLOYMENT[c] for c in s.employment) or "любая"
    return (
        f"🎯 Задачи (ключевые слова): <b>{tasks_line or '⚠️ не задано (укажи!)'}</b>\n"
        f"📍 Регион: <b>{areas}</b>\n"
        f"💻 Формат: <b>{fmt}</b>\n"
        f"📈 Опыт: <b>{exp}</b>\n"
        f"🕒 Занятость: <b>{emp}</b>\n"
        f"💰 Зарплата от: <b>{s.salary_min or '—'}</b>\n"
        f"🚫 Исключения: <b>{s.excluded_text or '—'}</b>\n"
        f"✉️ Письма: <b>{LETTER_MODES.get(s.letter_mode, s.letter_mode)}</b>"
        f"{' + ИИ' if s.ai_enabled else ''}\n"
        f"🎯 Умный отбор: <b>{('от ' + str(s.ai_score_min) + '%') if s.ai_score_enabled else 'выкл'}</b>\n"
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
        [b(text=f"🎯 Умный отбор (ИИ): {('от ' + str(s.ai_score_min) + '%') if s.ai_score_enabled else 'выкл'}",
           callback_data="task:score")],
        [b(text="📋 Все задачи", callback_data="task:tasks"),
         b(text="➕ Новая задача", callback_data="task:newtask")],
        [b(text="📍 Регион", callback_data="task:input:areas"),
         b(text="💰 Зарплата", callback_data="task:input:salary_min")],
        [b(text="💻 Формат", callback_data="task:sub:fmt"),
         b(text="📈 Опыт", callback_data="task:sub:exp")],
        [b(text="🕒 Занятость", callback_data="task:sub:emp"),
         b(text="🚫 Исключения", callback_data="task:input:excluded_text")],
        [b(text="📊 Лимит/день", callback_data="task:input:daily_limit"),
         b(text="⏰ Расписание", callback_data="task:input:window")],
        [b(text="✉️ Письма", callback_data="task:letters")],
    ])


async def _tasks_line(tg_user) -> str:
    from app.services.search_tasks import list_tasks
    async with async_session() as session:
        user = await get_or_create_user(session, tg_user.id, tg_user.username)
        tasks = await list_tasks(session, user.id)
    if not tasks:
        return ""
    parts = [f"{t.keyword}{'' if t.is_active else ' (выкл)'}" for t in tasks]
    return " · ".join(parts)


async def _show_main(target, s: UserSettings, is_active: bool, edit=False):
    tasks_line = await _tasks_line(target.from_user)
    text = "⚙️ <b>Задача автоотклика</b>\n\n" + _summary(s, tasks_line)
    kb = _main_kb(is_active, s)
    if edit and isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ── Задачи поиска (несколько ключевых слов) ──
class NewTaskSG(StatesGroup):
    keyword = State()


def _task_resume_line(t) -> str:
    """Короткая подпись резюме задачи для списка."""
    return f" · 📄 {t.resume_title[:24]}" if getattr(t, "resume_title", None) else ""


async def _res(session, cb_or_msg, state):
    """Куда пишем настройки: (holder, user, settings).

    Если в FSM выбрана задача (edit_task_id) — редактируем её настройки
    (при первом входе засеваем из общих). Иначе — общие настройки пользователя.
    SearchTask и User имеют одинаковые get_settings()/set_settings().
    """
    from app.models.search_task import SearchTask
    user = await _load(session, cb_or_msg)
    tid = (await state.get_data()).get("edit_task_id") if state else None
    holder = user
    if tid:
        t = await session.get(SearchTask, tid)
        if t and t.user_id == user.id:
            if not t.settings_json:  # первый вход в задачу — берём общие как основу
                t.set_settings(user.get_settings())
            holder = t
    return holder, user, holder.get_settings()


async def _task_stats_line(session, user_id: int, task_id: int) -> str:
    """Компактная статистика по одной задаче для шапки карточки."""
    async def c(*conds):
        return (await session.execute(
            select(func.count(Vacancy.id)).where(Vacancy.search_task_id == task_id, *conds)
        )).scalar() or 0
    processed = await c()
    sent = (await session.execute(
        select(func.count(Application.id)).where(
            Application.search_task_id == task_id,
            Application.status == ApplicationStatus.SENT)
    )).scalar() or 0
    ai_low = await c(Vacancy.skip_reason == "ai_low")
    return (f"📊 Обработано: <b>{processed}</b> · отправлено: <b>{sent}</b> · "
            f"фильтр ИИ: <b>{ai_low}</b>")


def _task_kb(t, s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text=("⏸ Задача активна" if t.is_active else "▶️ Задача выключена"),
           callback_data=f"task:atgl:{t.id}")],
        [b(text=f"📄 Резюме: {(t.resume_title or 'аккаунта')[:26]}",
           callback_data=f"task:res:{t.id}")],
        [b(text=f"🧭 Источник: {SOURCE_LABELS.get(getattr(s, 'vacancy_source', 'keyword'), 'по ключу')}",
           callback_data="task:source")],
        [b(text=f"🎯 Умный отбор (ИИ): {('от ' + str(s.ai_score_min) + '%') if s.ai_score_enabled else 'выкл'}",
           callback_data="task:score")],
        [b(text="📍 Регион", callback_data="task:input:areas"),
         b(text="💰 Зарплата", callback_data="task:input:salary_min")],
        [b(text="💻 Формат", callback_data="task:sub:fmt"),
         b(text="📈 Опыт", callback_data="task:sub:exp")],
        [b(text="🕒 Занятость", callback_data="task:sub:emp"),
         b(text="🚫 Исключения", callback_data="task:input:excluded_text")],
        [b(text="📊 Лимит/день", callback_data="task:input:daily_limit"),
         b(text="⏰ Расписание", callback_data="task:input:window")],
        [b(text="✉️ Письма", callback_data="task:letters"),
         b(text=f"📈 Поднятие: {'вкл' if s.resume_bump else 'выкл'}", callback_data="task:toggle_bump")],
        [b(text="⬅️ К задаче", callback_data=f"task:open:{t.id}")],
    ])


async def _show_task_settings(target, session, t, edit=False):
    """Экран настроек одной задачи (фильтры поиска)."""
    s = t.get_settings() if t.settings_json else UserSettings()
    text = f"⚙️ <b>Настройки задачи</b>\n🔎 {t.keyword}\n\n" + _summary(s, t.keyword)
    kb = _task_kb(t, s)
    if edit and isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _task_sent_counts(session, task_id: int) -> tuple[int, int]:
    total = (await session.execute(
        select(func.count(Application.id)).where(
            Application.search_task_id == task_id,
            Application.status == ApplicationStatus.SENT))).scalar() or 0
    today = (await session.execute(
        select(func.count(Application.id)).where(
            Application.search_task_id == task_id,
            Application.status == ApplicationStatus.SENT,
            func.date(Application.created_at) == func.current_date()))).scalar() or 0
    return today, total


def _task_card_kb(t, tasks) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    idx = next((i for i, x in enumerate(tasks) if x.id == t.id), 0)
    total = len(tasks)
    prev_id = tasks[idx - 1].id if idx > 0 else None
    next_id = tasks[idx + 1].id if idx < total - 1 else None
    nav = [b(text="◄", callback_data=(f"task:open:{prev_id}" if prev_id else "task:noop")),
           b(text=f"{idx + 1} из {total}", callback_data="task:noop"),
           b(text="►", callback_data=(f"task:open:{next_id}" if next_id else "task:noop"))]
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [b(text=("⏸ Остановить" if t.is_active else "▶️ Запустить"), callback_data=f"task:atgl:{t.id}"),
         b(text="🗑 Удалить", callback_data=f"task:del:{t.id}")],
        [b(text="⚙️ Настройки", callback_data=f"task:settings:{t.id}")],
        [b(text="📊 По задаче", callback_data=f"task:tstat:{t.id}"),
         b(text="➕ Новая задача", callback_data="task:newtask")],
        [b(text="⬅️ К списку задач", callback_data="task:list")],
    ])


async def _show_task_card(target, session, t, edit=False):
    """Карточка задачи: статистика и часы видны сразу, без входа в настройки."""
    from app.services.search_tasks import list_tasks
    s = t.get_settings() if t.settings_json else UserSettings()
    tasks = await list_tasks(session, t.user_id)
    today, total = await _task_sent_counts(session, t.id)
    idx = next((i for i, x in enumerate(tasks) if x.id == t.id), 0)
    src = SOURCE_LABELS.get(getattr(s, "vacancy_source", "keyword"), "по ключу")
    lines = [
        f"{'🟢' if t.is_active else '🔴'} <b>{t.keyword}</b>\n",
        f"📊 Откликов сегодня: <b>{today}</b> из {s.daily_limit} · всего: <b>{total}</b>",
        f"🕒 Часы: <b>{s.apply_hour_start}:00–{s.apply_hour_end}:00 МСК</b>",
        f"🧭 Источник: <b>{src}</b>",
    ]
    if getattr(t, "rec_found", None):
        lines.append(f"🔎 Вакансий подобрано: <b>~{t.rec_found}</b>")
    if getattr(t, "last_run_at", None):
        lines.append(f"🕓 Последний прогон: <b>{t.last_run_at[:16].replace('T', ' ')} UTC</b>")
    lines.append(f"\nЗадача {idx + 1} из {len(tasks)}")
    kb = _task_card_kb(t, tasks)
    txt = "\n".join(lines)
    if edit and isinstance(target, CallbackQuery):
        await target.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    else:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(txt, reply_markup=kb, parse_mode="HTML")


async def _render_home(target, state: FSMContext, edit=False):
    """Домашний экран задач: настройки выбранной задачи, либо список задач."""
    from app.models.search_task import SearchTask
    tid = (await state.get_data()).get("edit_task_id") if state else None
    async with async_session() as session:
        if tid:
            t = await session.get(SearchTask, tid)
            if t:
                await _show_task_settings(target, session, t, edit=edit)
                return
    # задача не выбрана — показать список
    if isinstance(target, CallbackQuery):
        await _tasks_view(target)
    else:
        await _tasks_view_msg(target)


async def _tasks_kb_and_text(cb_or_msg):
    from app.services.search_tasks import list_tasks, ensure_seeded
    b = InlineKeyboardButton
    async with async_session() as session:
        user = await _load(session, cb_or_msg)
        await ensure_seeded(session, user)
        tasks = await list_tasks(session, user.id)
        is_active = user.is_active
    rows = [[b(text=("⏸ Остановить автоотклик" if is_active else "▶️ Запустить автоотклик"),
              callback_data="task:toggle_active")]]
    for t in tasks:
        mark = "🟢" if t.is_active else "⚪️"
        rows.append([b(text=f"{mark} {t.keyword[:26]}{_task_resume_line(t)}", callback_data=f"task:open:{t.id}"),
                     b(text="🗑", callback_data=f"task:del:{t.id}")])
    rows.append([b(text="➕ Новая задача", callback_data="task:newtask")])
    text = (
        "📋 <b>Задачи</b>\n\n"
        "Каждая задача — своё название вакансии, своё резюме и свои настройки поиска. "
        "Автоотклик идёт по всем 🟢 активным.\n"
        "Тап по задаче — открыть её настройки и статистику; 🗑 — удалить."
    ) if tasks else (
        "📋 <b>Задачи</b>\n\nПока пусто. Добавь первую — «➕ Новая задача»."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _tasks_view(cb: CallbackQuery):
    text, kb = await _tasks_kb_and_text(cb)
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


async def _tasks_view_msg(message: Message):
    text, kb = await _tasks_kb_and_text(message)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "task:tasks")
async def cb_tasks(cb: CallbackQuery, state: FSMContext, **kw):
    await state.update_data(edit_task_id=None)
    await _tasks_view(cb)
    await cb.answer()


@router.callback_query(F.data == "task:list")
async def cb_task_list(cb: CallbackQuery, state: FSMContext, **kw):
    await state.update_data(edit_task_id=None)
    await _tasks_view(cb)
    await cb.answer()


@router.callback_query(F.data.startswith("task:open:"))
async def cb_task_open(cb: CallbackQuery, state: FSMContext, **kw):
    from app.models.search_task import SearchTask
    tid = int(cb.data.split(":")[2])
    async with async_session() as session:
        user = await _load(session, cb)
        t = await session.get(SearchTask, tid)
        if not t or t.user_id != user.id:
            await cb.answer("Задача не найдена", show_alert=True)
            return
        if not t.settings_json:  # старая задача без своих настроек — засеять из общих
            t.set_settings(user.get_settings())
            await session.commit()
        await state.update_data(edit_task_id=tid)
        await _show_task_card(cb, session, t, edit=True)
    await cb.answer()


@router.callback_query(F.data == "task:noop")
async def cb_task_noop(cb: CallbackQuery, **kw):
    await cb.answer()


@router.callback_query(F.data.startswith("task:settings:"))
async def cb_task_settings(cb: CallbackQuery, state: FSMContext, **kw):
    from app.models.search_task import SearchTask
    tid = int(cb.data.split(":")[2])
    async with async_session() as session:
        user = await _load(session, cb)
        t = await session.get(SearchTask, tid)
        if not t or t.user_id != user.id:
            await cb.answer("Задача не найдена", show_alert=True)
            return
        if not t.settings_json:
            t.set_settings(user.get_settings())
            await session.commit()
        await state.update_data(edit_task_id=tid)
        await _show_task_settings(cb, session, t, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("task:tstat:"))
async def cb_task_stat(cb: CallbackQuery, **kw):
    from app.models.search_task import SearchTask
    tid = int(cb.data.split(":")[2])
    b = InlineKeyboardButton

    async def cnt(session, *conds):
        return (await session.execute(
            select(func.count(Vacancy.id)).where(Vacancy.search_task_id == tid, *conds))).scalar() or 0

    async with async_session() as session:
        user = await _load(session, cb)
        t = await session.get(SearchTask, tid)
        if not t or t.user_id != user.id:
            await cb.answer("Задача не найдена", show_alert=True)
            return
        processed = await cnt(session)
        ai_low = await cnt(session, Vacancy.skip_reason == "ai_low")
        already = await cnt(session, Vacancy.skip_reason == "already")
        needs_test = await cnt(session, Vacancy.skip_reason == "needs_test")
        today, total = await _task_sent_counts(session, tid)

    def pct(n):
        return round(n / processed * 100) if processed else 0

    lines = [f"📊 <b>Статистика · {t.keyword}</b>\n",
             f"Обработано вакансий: <b>{processed}</b>",
             f"Отправлено сегодня: <b>{today}</b> · всего: <b>{total}</b>\n",
             f"✅ Отправлено: <b>{total}</b> ({pct(total)}%)", _bar(pct(total))]
    if ai_low:
        lines += [f"🤖 Фильтр ИИ: <b>{ai_low}</b> ({pct(ai_low)}%)", _bar(pct(ai_low))]
    if already:
        lines += [f"🔁 Уже откликались: <b>{already}</b> ({pct(already)}%)", _bar(pct(already))]
    if needs_test:
        lines += [f"📝 Нужен тест: <b>{needs_test}</b> ({pct(needs_test)}%)", _bar(pct(needs_test))]
    kb = InlineKeyboardMarkup(inline_keyboard=[[b(text="⬅️ К задаче", callback_data=f"task:open:{tid}")]])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@router.callback_query(F.data.startswith("task:atgl:"))
async def cb_task_active_toggle(cb: CallbackQuery, state: FSMContext, **kw):
    from app.models.search_task import SearchTask
    tid = int(cb.data.split(":")[2])
    async with async_session() as session:
        user = await _load(session, cb)
        t = await session.get(SearchTask, tid)
        if t and t.user_id == user.id:
            t.is_active = not t.is_active
            await session.commit()
            await _show_task_settings(cb, session, t, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("task:res:"))
async def cb_task_resume(cb: CallbackQuery, state: FSMContext, **kw):
    from app.parsers.hh_resume import list_resumes
    tid = int(cb.data.split(":")[2])
    await cb.answer("Загружаю резюме...")
    async with async_session() as session:
        user = await _load(session, cb)
        token = user.hh_access_token
    if not token:
        await cb.message.answer("Сначала подключи hh: /connect")
        return
    resumes = await list_resumes(token)
    if not resumes:
        await cb.message.answer("Не удалось получить список резюме.")
        return
    b = InlineKeyboardButton
    rows = [[b(text=f"📄 {r['title'][:48]}", callback_data=f"task:setres:{tid}:{r['id']}")]
            for r in resumes[:20]]
    rows.append([b(text="⬅️ Назад", callback_data=f"task:open:{tid}")])
    await cb.message.edit_text("📄 <b>Выбери резюме для этой задачи</b>",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@router.callback_query(F.data.startswith("task:setres:"))
async def cb_task_setres(cb: CallbackQuery, state: FSMContext, **kw):
    from app.parsers.hh_resume import fetch_resume_by_id
    from app.models.search_task import SearchTask
    _, _, tid_s, rid = cb.data.split(":", 3)
    tid = int(tid_s)
    async with async_session() as session:
        user = await _load(session, cb)
        token = user.hh_access_token
    rid2, text, title = await fetch_resume_by_id(token, rid)
    if not rid2:
        await cb.answer("Не удалось загрузить резюме", show_alert=True)
        return
    async with async_session() as session:
        user = await _load(session, cb)
        t = await session.get(SearchTask, tid)
        if t and t.user_id == user.id:
            t.resume_id, t.resume_title, t.resume_text = rid2, title, text
            await session.commit()
            await state.update_data(edit_task_id=tid)
            await _show_task_settings(cb, session, t, edit=True)
    await cb.answer("Резюме обновлено")


@router.callback_query(F.data.startswith("task:del:"))
async def cb_task_del(cb: CallbackQuery, state: FSMContext, **kw):
    from app.models.search_task import SearchTask
    tid = int(cb.data.split(":")[2])
    async with async_session() as session:
        user = await _load(session, cb)
        t = await session.get(SearchTask, tid)
        if t and t.user_id == user.id:
            await session.delete(t)
            await session.commit()
    await state.update_data(edit_task_id=None)
    await _tasks_view(cb)
    await cb.answer("Удалено")


@router.callback_query(F.data == "task:newtask")
async def cb_newtask(cb: CallbackQuery, state: FSMContext, **kw):
    """Шаг 1: выбор резюме, которым будет откликаться задача."""
    from app.parsers.hh_resume import list_resumes
    b = InlineKeyboardButton
    await cb.answer("Загружаю резюме...")
    async with async_session() as session:
        user = await _load(session, cb)
        if not user.hh_connected or not user.hh_access_token:
            await cb.message.answer("Сначала подключи hh: /connect")
            return
        token = user.hh_access_token
    resumes = await list_resumes(token)
    if not resumes:
        # Нет списка резюме — заводим задачу на резюме аккаунта, сразу спрашиваем ключ.
        await state.set_state(NewTaskSG.keyword)
        await state.update_data(resume_id=None, resume_title=None, resume_text=None)
        await _ask_keyword(cb.message)
        return
    rows = [[b(text=f"📄 {r['title'][:48]}", callback_data=f"task:ntres:{r['id']}")]
            for r in resumes[:20]]
    rows.append([b(text="❌ Отмена", callback_data="task:menu")])
    await cb.message.answer(
        "📄 <b>Шаг 1 из 2: каким резюме откликаемся?</b>\n\n"
        "Задача будет откликаться на вакансии именно этим резюме "
        "и письма писать по нему.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML",
    )


async def _ask_keyword(msg):
    await msg.answer(
        "🔍 <b>Шаг 2 из 2: какие вакансии ищем?</b>\n\n"
        "Пришли ОДНО название должности (например <code>системный аналитик</code>). "
        "Ищем вакансии строго с этим названием.\n\nОтмена: /task",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("task:ntres:"))
async def cb_newtask_resume(cb: CallbackQuery, state: FSMContext, **kw):
    from app.parsers.hh_resume import fetch_resume_by_id
    rid = cb.data.split(":", 2)[2]
    async with async_session() as session:
        user = await _load(session, cb)
        token = user.hh_access_token
    rid2, text, title = await fetch_resume_by_id(token, rid)
    if not rid2:
        await cb.answer("Не удалось загрузить резюме", show_alert=True)
        return
    await state.set_state(NewTaskSG.keyword)
    await state.update_data(resume_id=rid2, resume_title=title, resume_text=text)
    await cb.answer("Резюме выбрано")
    await _ask_keyword(cb.message)


@router.message(NewTaskSG.keyword)
async def on_newtask(message: Message, state: FSMContext, **kw):
    from app.models.search_task import SearchTask
    kw_text = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    if not kw_text or kw_text.startswith("/"):
        await message.answer("Пусто. Добавить задачу: 📋 Задача → ➕ Новая задача.")
        return
    async with async_session() as session:
        user = await _load(session, message)
        t = SearchTask(
            user_id=user.id, keyword=kw_text, is_active=True,
            resume_id=data.get("resume_id"), resume_title=data.get("resume_title"),
            resume_text=data.get("resume_text"),
        )
        t.set_settings(user.get_settings())  # засеваем настройки задачи из общих
        session.add(t)
        await session.commit()
        await session.refresh(t)
        tid = t.id
        res_line = f"\n📄 Резюме: <b>{data.get('resume_title')}</b>" if data.get("resume_title") else ""
        await message.answer(f"✅ Задача добавлена: <b>{kw_text}</b>{res_line}", parse_mode="HTML")
        await state.update_data(edit_task_id=tid)
        await _show_task_settings(message, session, t)


WHATS_NEW = (
    "🆕 <b>Возможности бота</b>\n\n"
    "📋 <b>Задачи</b> — у каждой свой ключ, своё резюме и свои настройки поиска "
    "(📋 Задачи → ➕ Новая задача).\n"
    "🧭 <b>Источник вакансий</b> — по ключу или по ленте рекомендаций hh под резюме "
    "(в карточке задачи). Рекомендаций в разы больше и они сами обновляются.\n"
    "🎯 <b>Умный отбор (ИИ)</b> — оценивает вакансию по резюме и отсекает нерелевантные.\n"
    "🔎 <b>Поиск по описанию</b> — вакансий ещё больше (в подменю отбора).\n"
    "📊 <b>Лимит/день</b> — до <b>50</b> бесплатно и до <b>200</b> на расширенном тарифе 💎.\n"
    "✉️ <b>Письма</b> — ИИ пишет под вакансию, или своё готовое письмо (⚙️ Письма).\n"
    "📈 <b>Авто-поднятие резюме</b> — держит резюме в топе.\n"
    "📄 <b>Клон резюме</b> — в ⚙️ Настройки.\n"
    "📨 <b>Пересылка со 2-го ТГ</b> и ✉️ <b>Контакт для писем</b> — там же.\n"
    "➕ <b>Мультиаккаунт</b> — несколько hh-аккаунтов (расширенный тариф).\n\n"
    "Открыть это снова: /new"
)


@router.message(Command("new"))
async def cmd_new(message: Message, **kw):
    await message.answer(WHATS_NEW, parse_mode="HTML")


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
            used, total = await beta_slots(session)
            paid = user.is_paid
        status = "🟢 работает" if active else "⚪️ остановлен"
        kw = s.search_text or "⚠️ не задано"
        left = max(0, total - used)
        tariff = "💎 Полный доступ" if paid else "Бесплатный"
        await send_photo_or_text(
            message, "welcome",
            "👋 <b>С возвращением!</b>\n\n"
            f"💎 Тариф: <b>{tariff}</b>\n"
            f"🎟 Бета-доступ: занято <b>{used}/{total}</b>, осталось <b>{left}</b>\n\n"
            f"🤖 Автоотклик: <b>{status}</b>\n"
            f"🔑 Ключевые слова: <b>{kw}</b>\n"
            f"📊 Откликов сегодня: <b>{today}</b>\n\n"
            "Управление — кнопками внизу 👇\n"
            "📋 <b>Задача</b> — фильтры и запуск · 📊 <b>Статистика</b> · ⚙️ <b>Настройки</b>\n"
            "🆕 Все возможности и лимиты — /new",
            reply_markup=main_reply_kb(),
        )


# ── Нижние кнопки ──
@router.message(F.text == BTN_TASK)
async def btn_task(message: Message, state: FSMContext, **kw):
    await cmd_task(message, state)


def _bar(pct: int, width: int = 10) -> str:
    """Полоска прогресса из блоков: ▓ заполнено, ░ пусто."""
    fill = max(0, min(width, round(pct / 100 * width)))
    return "▓" * fill + "░" * (width - fill)


@router.message(F.text == BTN_STATS)
async def btn_stats(message: Message, **kw):
    async def _count(session, *conds) -> int:
        return (await session.execute(
            select(func.count(Vacancy.id)).where(Vacancy.user_id == user_id, *conds)
        )).scalar() or 0

    async with async_session() as session:
        user = await _load(session, message)
        user_id = user.id
        active = user.is_active
        s = user.get_settings()
        # Реальные отправленные отклики.
        sent = (await session.execute(
            select(func.count(Application.id)).where(
                Application.user_id == user_id, Application.status == ApplicationStatus.SENT)
        )).scalar() or 0
        today = (await session.execute(
            select(func.count(Application.id)).where(
                Application.user_id == user_id, Application.status == ApplicationStatus.SENT,
                func.date(Application.created_at) == func.current_date())
        )).scalar() or 0
        # Разбивка по обработанным вакансиям.
        processed = await _count(session)
        ai_low = await _count(session, Vacancy.skip_reason == "ai_low")
        already = await _count(session, Vacancy.skip_reason == "already")
        needs_test = await _count(session, Vacancy.skip_reason == "needs_test")

    def pct(n: int) -> int:
        return round(n / processed * 100) if processed else 0

    lines = [
        "📊 <b>Общая статистика</b> (по всем задачам)\n",
        f"Всего обработано вакансий: <b>{processed}</b>",
        f"Сегодня отправлено: <b>{today}</b> · Автоотклик: <b>{'работает' if active else 'остановлен'}</b>\n",
        f"✅ Отправлено откликов: <b>{sent}</b> ({pct(sent)}%)",
        _bar(pct(sent)),
    ]
    if s.ai_score_enabled or ai_low:
        lines += [f"🤖 Не подошли (фильтр ИИ): <b>{ai_low}</b> ({pct(ai_low)}%)", _bar(pct(ai_low))]
    if already:
        lines += [f"🔁 Уже откликались: <b>{already}</b> ({pct(already)}%)", _bar(pct(already))]
    if needs_test:
        lines += [f"📝 Нужен тест на НН: <b>{needs_test}</b> ({pct(needs_test)}%)", _bar(pct(needs_test))]
    lines.append(
        "\nℹ️ «Фильтр ИИ» и «уже откликались» — это норма: бот бережёт "
        "отклики и не тратит их на нерелевантные вакансии и дубли."
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text == BTN_SETTINGS)
async def btn_settings(message: Message, state: FSMContext, **kw):
    await state.update_data(edit_task_id=None)  # аккаунт-настройки, не задача
    async with async_session() as session:
        user = await _load(session, message)
        if not user.hh_connected:
            await message.answer("🔗 hh.ru пока не подключён. Нажми /connect.")
            return
        paid = user.is_paid
        resume_line = (user.resume_text.splitlines()[0] if user.resume_text else "")
        contact = (user.get_settings().contact or "").strip()
        extra = await _extra_accounts_count(session, user.id)
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🔗 Аккаунтов hh: <b>{1 + extra}</b>\n"
        f"📄 {resume_line or 'резюме загружено'}\n"
        f"✉️ Контакт для писем: <b>{contact or 'личный ТГ (по умолчанию)'}</b>\n"
        f"💎 Тариф: <b>{'Расширенный' if paid else 'Бесплатный'}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Контакт для писем", callback_data="task:input:contact")],
        [InlineKeyboardButton(text="📨 Пересылка сообщений (2-й ТГ)", callback_data="ub:menu")],
        [InlineKeyboardButton(text="📄 Выбрать резюме", callback_data="acc:resumes"),
         InlineKeyboardButton(text="📄 Клонировать", callback_data="acc:clone_resume")],
        [InlineKeyboardButton(text="🔗 Мои аккаунты", callback_data="acc:list")],
        [InlineKeyboardButton(text="🚪 Выйти из основного hh", callback_data="acc:logout")],
        [InlineKeyboardButton(text="💎 Тариф", callback_data="task:tariff")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _extra_accounts_count(session, user_id: int) -> int:
    from app.models.hh_account import HHAccount
    try:
        return (await session.execute(
            select(func.count(HHAccount.id)).where(HHAccount.user_id == user_id)
        )).scalar() or 0
    except Exception as e:
        log.warning("extra_accounts_count_failed", error=str(e))
        return 0


async def _accounts_view(cb: CallbackQuery):
    from app.models.hh_account import HHAccount
    b = InlineKeyboardButton
    async with async_session() as session:
        user = await _load(session, cb)
        accs = (await session.execute(
            select(HHAccount).where(HHAccount.user_id == user.id).order_by(HHAccount.id)
        )).scalars().all()
        primary_line = (user.resume_text.splitlines()[0] if user.resume_text else "основной")
        total = 1 + len(accs)
    rows = [[b(text=f"① {primary_line[:40]} (основной)", callback_data="noop")]]
    for i, a in enumerate(accs, start=2):
        mark = "🟢" if a.is_active else "⚪️"
        rows.append([b(text=f"{mark} {(a.label or 'аккаунт')[:34]}", callback_data=f"acc:tgl:{a.id}"),
                     b(text="🗑", callback_data=f"acc:del:{a.id}")])
    rows.append([b(text="➕ Подключить ещё аккаунт", callback_data="acc:add")])
    rows.append([b(text="⬅️ Назад", callback_data="task:menu")])
    text = (
        "🔗 <b>Мои hh-аккаунты</b>\n\n"
        f"Всего: <b>{total}</b>. Автоотклик идёт с основного и со всех 🟢 активных "
        "(у каждого свой дневной лимит).\n"
        "Тапни по доп. аккаунту — вкл/выкл, 🗑 — удалить."
    )
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@router.callback_query(F.data == "acc:list")
async def cb_acc_list(cb: CallbackQuery, **kw):
    await _accounts_view(cb)
    await cb.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery, **kw):
    await cb.answer()


@router.callback_query(F.data.startswith("acc:tgl:"))
async def cb_acc_toggle(cb: CallbackQuery, **kw):
    from app.models.hh_account import HHAccount
    aid = int(cb.data.split(":")[2])
    async with async_session() as session:
        user = await _load(session, cb)
        a = await session.get(HHAccount, aid)
        if a and a.user_id == user.id:
            a.is_active = not a.is_active
            await session.commit()
    await _accounts_view(cb)
    await cb.answer()


@router.callback_query(F.data.startswith("acc:del:"))
async def cb_acc_del(cb: CallbackQuery, **kw):
    from app.models.hh_account import HHAccount
    aid = int(cb.data.split(":")[2])
    async with async_session() as session:
        user = await _load(session, cb)
        a = await session.get(HHAccount, aid)
        if a and a.user_id == user.id:
            await session.delete(a)
            await session.commit()
    await _accounts_view(cb)
    await cb.answer("Аккаунт удалён")


@router.callback_query(F.data == "acc:add")
async def cb_acc_add(cb: CallbackQuery, state: FSMContext, **kw):
    from app.bot.hh_connect import ConnectSG
    async with async_session() as session:
        user = await _load(session, cb)
        paid = user.is_paid
        total = 1 + await _extra_accounts_count(session, user.id)
    if not paid:
        await cb.answer()
        await cb.message.answer(
            "Несколько hh-аккаунтов — функция расширенного тарифа 💎\n"
            "Оформить: ⚙️ Настройки → 💎 Тариф."
        )
        return
    if total >= settings.max_hh_accounts:
        await cb.answer(f"Лимит аккаунтов: {settings.max_hh_accounts}", show_alert=True)
        return
    await state.set_state(ConnectSG.phone)
    await cb.message.answer(
        "🔐 <b>Добавление ещё одного hh-аккаунта</b>\n\n"
        "Пришли номер телефона этого аккаунта — hh отправит код, введёшь его здесь. "
        "Пароль не нужен.\n\nОтмена: /cancel",
        parse_mode="HTML",
    )
    await cb.answer()


@router.callback_query(F.data == "acc:hide_rej")
async def cb_hide_rejections(cb: CallbackQuery, **kw):
    from app.parsers.hh_user_client import HHUserClient
    await cb.answer("Читаю отклики на hh...")
    import json
    async with async_session() as session:
        user = await _load(session, cb)
        if not user.hh_connected or not user.hh_access_token:
            await cb.message.answer("Сначала подключи hh: /connect")
            return
        client = HHUserClient(
            access_token=user.hh_access_token,
            refresh_token=user.hh_refresh_token or "",
            resume_id=user.hh_resume_id,
            expires_at=user.hh_token_expires.timestamp() if user.hh_token_expires else 0.0,
        )
        cookies_state = None
        if user.hh_cookies:
            try:
                cookies_state = json.loads(user.hh_cookies)
            except Exception:
                cookies_state = None
    res = await client.hide_rejections(cookies_state=cookies_state)
    if client.new_token:
        import datetime
        async with async_session() as session:
            u = await _load(session, cb)
            u.hh_access_token = client.new_token["access_token"]
            u.hh_refresh_token = client.new_token["refresh_token"]
            u.hh_token_expires = datetime.datetime.fromtimestamp(
                client.new_token["expires_at"], tz=datetime.timezone.utc)
            await session.commit()
    if res.get("error"):
        await cb.message.answer(f"⚠️ Не удалось убрать отказы: {res['error']}")
        return
    if not res.get("web"):
        await cb.message.answer(
            "⚠️ Нет веб-сессии для полного скрытия отказов. Отказы «отменены» по API, "
            "но в списке могут остаться. Чтобы скрывать их полностью — переподключи hh "
            "(🚪 выйти → /connect), тогда бот сохранит веб-сессию.\n\n"
            f"Обработано отказов: <b>{res['hidden']}</b> (проверено: {res['checked']}).",
            parse_mode="HTML",
        )
        return
    await cb.message.answer(
        f"🗑 <b>Готово.</b> Скрыто отказов: <b>{res['hidden']}</b> "
        f"(проверено откликов: {res['checked']}). Обнови страницу hh.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "acc:resumes")
async def cb_resumes(cb: CallbackQuery, **kw):
    from app.parsers.hh_resume import list_resumes
    await cb.answer("Загружаю резюме...")
    async with async_session() as session:
        user = await _load(session, cb)
        if not user.hh_connected or not user.hh_access_token:
            await cb.message.answer("Сначала подключи hh: /connect")
            return
        token = user.hh_access_token
        current = user.hh_resume_id
    resumes = await list_resumes(token)
    if not resumes:
        await cb.message.answer("Не удалось получить список резюме. Проверь, что на hh есть опубликованное резюме.")
        return
    rows = []
    for r in resumes[:20]:
        mark = "✅ " if r["id"] == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{r['title'][:48]}", callback_data=f"acc:setres:{r['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="task:menu")])
    await cb.message.answer(
        "📄 <b>Выбери резюме для откликов</b>\n\nБот будет откликаться этим резюме "
        "и писать письма по нему.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("acc:setres:"))
async def cb_set_resume(cb: CallbackQuery, **kw):
    from app.parsers.hh_resume import fetch_resume_by_id
    rid = cb.data.split(":", 2)[2]
    async with async_session() as session:
        user = await _load(session, cb)
        token = user.hh_access_token
    rid2, text, title = await fetch_resume_by_id(token, rid)
    if not rid2:
        await cb.answer("Не удалось загрузить резюме", show_alert=True)
        return
    async with async_session() as session:
        user = await _load(session, cb)
        user.hh_resume_id = rid2
        if text:
            user.resume_text = text
        await session.commit()
    await cb.answer("Резюме выбрано")
    await cb.message.answer(f"✅ Резюме для откликов: <b>{title or 'выбрано'}</b>", parse_mode="HTML")


@router.callback_query(F.data == "acc:clone_resume")
async def cb_clone_resume(cb: CallbackQuery, **kw):
    from app.parsers.hh_user_client import HHUserClient
    await cb.answer("Клонирую резюме на hh...")
    async with async_session() as session:
        user = await _load(session, cb)
        if not user.hh_connected or not user.hh_access_token:
            await cb.message.answer("Сначала подключи hh: /connect")
            return
        client = HHUserClient(
            access_token=user.hh_access_token,
            refresh_token=user.hh_refresh_token or "",
            resume_id=user.hh_resume_id,
            expires_at=user.hh_token_expires.timestamp() if user.hh_token_expires else 0.0,
        )
    res = await client.clone_resume()
    if res.get("ok"):
        await cb.message.answer(
            "📄 <b>Резюме клонировано.</b> Свежая копия позволяет откликаться заново "
            "на вакансии, куда ты уже откликался.\n\n"
            "Чтобы бот применял именно копию — сделай её <b>основным</b> резюме на hh "
            "(перемести вверх списка / опубликуй) и <b>переподключись</b> (🚪 выйти → "
            "/connect): бот берёт резюме, которое подтягивает при входе. Выбор резюме "
            "прямо в боте добавлю, если понадобится.",
            parse_mode="HTML",
        )
    else:
        await cb.message.answer(f"⚠️ Не удалось клонировать резюме: {res.get('error')}")


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
        "🆘 <b>Поддержка</b>\n\n"
        "⚡️ Если что-то работает не так — сначала перезапусти бота командой /start, "
        "это решает большинство мелких сбоев.\n\n"
        f"Не помогло — пиши: {settings.support_contact}",
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
    await _tasks_view_msg(message)


@router.callback_query(F.data == "task:menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext, **kw):
    await _render_home(cb, state, edit=True)
    await cb.answer()


@router.callback_query(F.data == "task:toggle_active")
async def cb_toggle_active(cb: CallbackQuery, state: FSMContext, **kw):
    from app.services.search_tasks import active_keywords
    async with async_session() as session:
        user = await _load(session, cb)
        if not user.hh_connected:
            await cb.answer("Сначала подключи hh: /connect", show_alert=True)
            return
        if not user.is_active and not await active_keywords(session, user.id):
            await cb.answer("Сначала добавь хотя бы одну задачу — ➕ Новая задача.", show_alert=True)
            return
        user.is_active = not user.is_active
        await session.commit()
        state_on = user.is_active
    await _tasks_view(cb)
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
        [b(text=f"🤖 ИИ пишет письма: {'вкл' if s.ai_enabled else 'выкл'}", callback_data="task:toggle_ai")],
        [b(text=("✍️ Своё письмо: задано" if s.custom_letter else "✍️ Своё письмо: нет"),
           callback_data="task:input:custom_letter")],
        [b(text=("📝 Промт для ИИ: задан" if s.ai_custom_prompt else "📝 Промт для ИИ: стандартный"),
           callback_data="task:input:ai_custom_prompt")],
        [b(text="⬅️ Назад", callback_data="task:menu")],
    ])


async def _show_letters(cb: CallbackQuery, s: UserSettings):
    text = (
        "✉️ <b>Сопроводительные письма</b>\n\n"
        "• <b>Режим</b> — прикладывать письмо всегда, только где вакансия требует, или без писем.\n"
        "• <b>🤖 ИИ пишет письма</b> — главный выключатель. Вкл → ИИ генерит письмо под "
        "каждую вакансию по резюме. Выкл → шлётся своё письмо или шаблон.\n"
        "• <b>✍️ Своё письмо</b> — твой готовый текст, шлётся как есть (когда ИИ выключен).\n"
        "• <b>📝 Промт для ИИ</b> — как именно ИИ должен писать (работает только при включённом ИИ).\n\n"
        f"Сейчас: <b>{'ИИ-письма' if s.ai_enabled else ('своё письмо' if s.custom_letter else 'стандартный шаблон')}</b>."
    )
    await cb.message.edit_text(text, reply_markup=_letters_kb(s), parse_mode="HTML")


@router.callback_query(F.data == "task:letters")
async def cb_letters(cb: CallbackQuery, state: FSMContext, **kw):
    async with async_session() as session:
        _, _, s = await _res(session, cb, state)
    await _show_letters(cb, s)
    await cb.answer()


@router.callback_query(F.data.startswith("task:lmode:"))
async def cb_lmode(cb: CallbackQuery, state: FSMContext, **kw):
    mode = cb.data.split(":")[2]
    async with async_session() as session:
        holder, _, s = await _res(session, cb, state)
        s.letter_mode = mode
        holder.set_settings(s)
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=_letters_kb(s))
    await cb.answer(LETTER_MODES.get(mode, mode))


def _score_kb(s: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text=f"🎯 Умный отбор: {'🟢 вкл' if s.ai_score_enabled else '⚪️ выкл'}",
           callback_data="task:toggle_score")],
        [b(text=f"🎚 Порог соответствия: от {s.ai_score_min}%",
           callback_data="task:input:ai_score_min")],
        [b(text=f"🔎 Искать в описании: {'🟢 вкл' if s.search_in_description else '⚪️ выкл'}",
           callback_data="task:toggle_desc")],
        [b(text="♻️ Пересмотреть отсеянные", callback_data="task:reconsider")],
        [b(text="⬅️ Назад", callback_data="task:menu")],
    ])


async def _show_score(cb: CallbackQuery, s: UserSettings):
    text = (
        "🎯 <b>Умный отбор вакансий (ИИ)</b>\n\n"
        "Перед откликом ИИ сравнивает вакансию с твоим резюме и ставит оценку "
        "соответствия 0–100%. Бот откликается только на вакансии с оценкой "
        f"<b>≥ {s.ai_score_min}%</b> — слабые совпадения пропускаются.\n\n"
        "🔎 <b>Искать в описании</b> — искать не только по названию, но и в тексте "
        "вакансии. Вакансий в разы больше; точность держит умный отбор. "
        "Лучше держать вместе с включённым отбором.\n"
        "♻️ <b>Пересмотреть отсеянные</b> — вернуть ранее отсеянные вакансии в очередь "
        "(например если снизил порог)."
    )
    await cb.message.edit_text(text, reply_markup=_score_kb(s), parse_mode="HTML")


@router.callback_query(F.data == "task:toggle_desc")
async def cb_toggle_desc(cb: CallbackQuery, state: FSMContext, **kw):
    async with async_session() as session:
        holder, _, s = await _res(session, cb, state)
        s.search_in_description = not s.search_in_description
        holder.set_settings(s)
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=_score_kb(s))
    await cb.answer("Поиск в описании включён" if s.search_in_description else "Только по названию")


@router.callback_query(F.data == "task:reconsider")
async def cb_reconsider(cb: CallbackQuery, state: FSMContext, **kw):
    from sqlalchemy import update
    tid = (await state.get_data()).get("edit_task_id")
    async with async_session() as session:
        user = await _load(session, cb)
        conds = [Vacancy.user_id == user.id, Vacancy.status == VacancyStatus.REJECTED]
        if tid:  # редактируем конкретную задачу — вернуть только её отсев
            conds.append(Vacancy.search_task_id == tid)
        res = await session.execute(
            update(Vacancy).where(*conds)
            .values(status=VacancyStatus.NEW, ai_score=None, ai_reason=None, skip_reason=None)
        )
        await session.commit()
    n = res.rowcount or 0
    await cb.answer(f"Возвращено в очередь: {n}", show_alert=True)


@router.callback_query(F.data == "task:source")
async def cb_source(cb: CallbackQuery, state: FSMContext, **kw):
    async with async_session() as session:
        holder, _, s = await _res(session, cb, state)
        cur = getattr(s, "vacancy_source", "keyword")
        s.vacancy_source = SOURCE_CYCLE.get(cur, "keyword")
        holder.set_settings(s)
        await session.commit()
    await _render_home(cb, state, edit=True)
    await cb.answer("Источник: " + SOURCE_LABELS.get(s.vacancy_source, s.vacancy_source))


@router.callback_query(F.data == "task:score")
async def cb_score(cb: CallbackQuery, state: FSMContext, **kw):
    async with async_session() as session:
        _, _, s = await _res(session, cb, state)
    await _show_score(cb, s)
    await cb.answer()


@router.callback_query(F.data == "task:toggle_score")
async def cb_toggle_score(cb: CallbackQuery, state: FSMContext, **kw):
    async with async_session() as session:
        holder, _, s = await _res(session, cb, state)
        s.ai_score_enabled = not s.ai_score_enabled
        holder.set_settings(s)
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=_score_kb(s))
    await cb.answer("Умный отбор включён" if s.ai_score_enabled else "Выключен")


@router.callback_query(F.data == "task:toggle_bump")
async def cb_toggle_bump(cb: CallbackQuery, state: FSMContext, **kw):
    async with async_session() as session:
        holder, _, s = await _res(session, cb, state)
        s.resume_bump = not s.resume_bump
        holder.set_settings(s)
        await session.commit()
    await _render_home(cb, state, edit=True)
    await cb.answer("Поднятие резюме включено" if s.resume_bump else "Выключено")


@router.callback_query(F.data == "task:toggle_ai")
async def cb_toggle_ai(cb: CallbackQuery, state: FSMContext, **kw):
    async with async_session() as session:
        holder, _, s = await _res(session, cb, state)
        s.ai_enabled = not s.ai_enabled
        holder.set_settings(s)
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
async def cb_sub(cb: CallbackQuery, state: FSMContext, **kw):
    group = cb.data.split(":")[2]
    field, _ = GROUPS[group]
    async with async_session() as session:
        _, _, s = await _res(session, cb, state)
        selected = getattr(s, field)
    await cb.message.edit_text(
        "Отметь нужные варианты (можно несколько):",
        reply_markup=_sub_kb(group, selected),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("task:tog:"))
async def cb_tog(cb: CallbackQuery, state: FSMContext, **kw):
    _, _, group, code = cb.data.split(":")
    field, _ = GROUPS[group]
    async with async_session() as session:
        holder, _, s = await _res(session, cb, state)
        cur = list(getattr(s, field))
        if code in cur:
            cur.remove(code)
        else:
            cur.append(code)
        setattr(s, field, cur)
        holder.set_settings(s)
        await session.commit()
        selected = cur
    await cb.message.edit_reply_markup(reply_markup=_sub_kb(group, selected))
    await cb.answer()


# ── Ввод значений (FSM) ──
_PROMPTS = {
    "search_text": "Пришли ключевые слова для поиска (например: <code>системный аналитик</code>). Пусто = по умолчанию.",
    "areas": "Пришли город или несколько через запятую (например: <code>Москва, СПб, Казань</code>). "
             "Для всей страны — <code>вся Россия</code>.",
    "salary_min": "Пришли минимальную зарплату числом (например: <code>200000</code>). 0 = без ограничения.",
    "excluded_text": "Пришли слова-исключения через запятую (например: <code>1С, junior</code>).",
    "daily_limit": "Пришли лимит откликов в день числом.\n"
                   "Максимум: <b>50</b> (бесплатно) / <b>200</b> (расширенный тариф 💎).",
    "ai_score_min": "Пришли минимальный процент соответствия для отклика (0–100). "
                    "Например <code>70</code> — откликаться только на вакансии с оценкой ИИ ≥ 70%.",
    "window": "Пришли окно откликов в формате <code>9-21</code> (часы МСК). Круглосуточно — пришли <code>0-24</code>.",
    "ai_custom_prompt": "Пришли свой промт для ИИ-писем (как писать сопроводительное). Пусто/<code>-</code> — вернуть стандартный.",
    "custom_letter": "Пришли текст своего сопроводительного письма — его будем прикладывать как есть "
                     "(без ИИ). Можно вставить <code>%(vacancy_suffix)s</code> — подставится название вакансии. "
                     "Пусто/<code>-</code> — убрать своё письмо.",
    "contact": "Пришли контакт для сопроводительных писем — его увидит HR вместо твоего личного ТГ "
               "(например второй ТГ-аккаунт <code>@my_work_tg</code>, почта или телефон). "
               "Пусто/<code>-</code> — убрать контакт.",
}


CLEARABLE_FIELDS = {"ai_custom_prompt", "custom_letter", "contact", "excluded_text"}


@router.callback_query(F.data.startswith("task:input:"))
async def cb_input(cb: CallbackQuery, state: FSMContext, **kw):
    field = cb.data.split(":")[2]
    await state.set_state(TaskInput.value)
    await state.update_data(field=field)
    kb = None
    if field in CLEARABLE_FIELDS:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Сбросить (стандартный)", callback_data=f"task:clear:{field}")]
        ])
    await cb.message.answer(_PROMPTS.get(field, "Пришли значение:") + "\n\nОтмена: /task",
                            reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@router.callback_query(F.data.startswith("task:clear:"))
async def cb_clear(cb: CallbackQuery, state: FSMContext, **kw):
    field = cb.data.split(":")[2]
    async with async_session() as session:
        holder, user, s = await _res(session, cb, state)
        if hasattr(s, field):
            setattr(s, field, "")
            holder.set_settings(s)
            await session.commit()
    await state.set_state(None)  # выходим из ввода, задачу в state помним
    await cb.answer("Сброшено на стандартный")
    await _render_home(cb, state)


@router.message(TaskInput.value)
async def on_value(message: Message, state: FSMContext, **kw):
    data = await state.get_data()
    field = data.get("field")
    raw = (message.text or "").strip()
    err = None
    async with async_session() as session:
        holder, user, s = await _res(session, message, state)
        if field == "search_text":
            s.search_text = raw
        elif field == "ai_custom_prompt":
            s.ai_custom_prompt = "" if raw in ("", "-") else raw
        elif field == "custom_letter":
            s.custom_letter = "" if raw in ("", "-") else raw
        elif field == "contact":
            s.contact = "" if raw in ("", "-") else raw
        elif field == "excluded_text":
            s.excluded_text = raw
        elif field == "areas":
            parts = [p.strip().lower() for p in re.split(r"[,/\n]+", raw) if p.strip()]
            ids: list[int] = []
            unknown: list[str] = []
            for p in parts:
                aid = AREAS.get(p)
                if aid:
                    if aid not in ids:
                        ids.append(aid)
                else:
                    unknown.append(p)
            if ids:
                s.areas = ids
                if unknown:
                    err = "Не узнал: " + ", ".join(unknown) + " — остальные сохранил."
            else:
                err = "Не узнал город. Попробуй: Москва, СПб, Казань, вся Россия (можно через запятую)."
        elif field == "salary_min":
            if raw.isdigit():
                s.salary_min = int(raw)
            else:
                err = "Нужно число."
        elif field == "ai_score_min":
            if raw.isdigit():
                s.ai_score_min = max(0, min(100, int(raw)))
            else:
                err = "Нужно число 0–100."
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
        if err is None or field in ("daily_limit", "areas"):
            holder.set_settings(s)
            await session.commit()
    await state.set_state(None)  # выходим из ввода, задачу в state помним
    if err:
        await message.answer("⚠️ " + err)
    await _render_home(message, state)


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
