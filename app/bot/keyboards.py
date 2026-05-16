from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Вакансии"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="⭐ Топ вакансии"), KeyboardButton(text="📩 Сообщения")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📋 Логи")],
        ],
        resize_keyboard=True,
    )


def vacancy_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Откликнуться", callback_data=f"apply:{vacancy_id}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip:{vacancy_id}"),
        ],
        [
            InlineKeyboardButton(text="📄 Подробнее", callback_data=f"details:{vacancy_id}"),
            InlineKeyboardButton(text="🚫 В ЧС", callback_data=f"blacklist:{vacancy_id}"),
        ],
    ])


def vacancy_list_keyboard(vacancy_ids: list[int], page: int, total_pages: int, prefix: str = "") -> InlineKeyboardMarkup:
    rows = []
    for i, vid in enumerate(vacancy_ids):
        rows.append([
            InlineKeyboardButton(text=f"📄 Подробнее #{i+1}", callback_data=f"details:{vid}"),
            InlineKeyboardButton(text="✅", callback_data=f"apply:{vid}"),
            InlineKeyboardButton(text="❌", callback_data=f"skip:{vid}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"{prefix}page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"{prefix}page:{page+1}"))
    rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def message_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 AI-ответ", callback_data=f"ai_reply:{message_id}"),
            InlineKeyboardButton(text="✅ Прочитано", callback_data=f"mark_read:{message_id}"),
        ],
    ])


def confirm_apply_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📨 Да, отправить", callback_data=f"confirm_apply:{vacancy_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_apply:{vacancy_id}"),
        ],
    ])


def settings_keyboard(is_paused: bool = False, auto_apply: bool = False) -> InlineKeyboardMarkup:
    pause_text = "▶️ Возобновить" if is_paused else "⏸ Пауза"
    auto_text = "🟢 Авто-отклик ВКЛ" if auto_apply else "⚪ Авто-отклик ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=pause_text, callback_data="toggle_pause"),
            InlineKeyboardButton(text=auto_text, callback_data="toggle_auto"),
        ],
        [
            InlineKeyboardButton(text="🔄 Искать сейчас", callback_data="force_search"),
            InlineKeyboardButton(text="💎 Баланс AI", callback_data="show_balance"),
        ],
        [
            InlineKeyboardButton(text="⬆️ Поднять резюме", callback_data="bump_resume"),
            InlineKeyboardButton(text="💬 Спасибо за отказы", callback_data="thank_rejections"),
        ],
    ])
