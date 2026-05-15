from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def vacancy_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Откликнуться", callback_data=f"apply:{vacancy_id}"),
            InlineKeyboardButton(text="Пропустить", callback_data=f"skip:{vacancy_id}"),
        ],
        [
            InlineKeyboardButton(text="Подробнее", callback_data=f"details:{vacancy_id}"),
            InlineKeyboardButton(text="В чёрный список", callback_data=f"blacklist:{vacancy_id}"),
        ],
    ])


def message_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="AI-ответ", callback_data=f"ai_reply:{message_id}"),
            InlineKeyboardButton(text="Отправить ответ", callback_data=f"send_reply:{message_id}"),
        ],
        [
            InlineKeyboardButton(text="Прочитано", callback_data=f"mark_read:{message_id}"),
        ],
    ])


def confirm_apply_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да, отправить", callback_data=f"confirm_apply:{vacancy_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"cancel_apply:{vacancy_id}"),
        ],
        [
            InlineKeyboardButton(text="Изменить письмо", callback_data=f"edit_letter:{vacancy_id}"),
        ],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Авто-режим ВКЛ/ВЫКЛ", callback_data="toggle_auto"),
            InlineKeyboardButton(text="Интервал проверки", callback_data="set_interval"),
        ],
        [
            InlineKeyboardButton(text="Лимит откликов/день", callback_data="set_limit"),
            InlineKeyboardButton(text="Мин. скор AI", callback_data="set_min_score"),
        ],
        [
            InlineKeyboardButton(text="Платформы", callback_data="set_platforms"),
        ],
    ])
