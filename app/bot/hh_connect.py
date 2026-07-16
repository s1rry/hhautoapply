"""
Мультиюзерное подключение hh.ru по OTP (Фаза 2).

Любой пользователь: /connect (или кнопка) → телефон → код из hh → токен
сохраняется в его строку User, резюме подтягивается автоматически. Пароль
пользователь боту не передаёт: код приходит ему от hh, он лишь вводит его.

Официальный OAuth для соискателей закрыт с 15.12.2025, поэтому используем
вход как Android-приложение hh (OTPLoginSession).
"""
from __future__ import annotations

import datetime

import structlog
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from app.database import async_session
from app.parsers.hh_login import OTPLoginSession, get_session, set_session, drop_session
from app.parsers.hh_resume import fetch_resume
from app.services.user_service import get_or_create_user

log = structlog.get_logger()

router = Router()


class ConnectSG(StatesGroup):
    phone = State()
    captcha = State()
    code = State()


async def _send_captcha(message: Message):
    from aiogram.types import FSInputFile
    from app.parsers.hh_login import CAPTCHA_FILE
    try:
        await message.answer_photo(
            FSInputFile(str(CAPTCHA_FILE)),
            caption="🔐 hh просит капчу. Введи символы с картинки одним сообщением.\n\nОтмена: /cancel",
        )
    except Exception:
        await message.answer("hh просит капчу, но картинку показать не удалось. Попробуй /connect ещё раз.")


async def _is_cancel(message: Message, state: FSMContext) -> bool:
    if (message.text or "").strip().lower() in ("/cancel", "отмена"):
        await drop_session(message.chat.id)
        await state.clear()
        await message.answer("Отменено. Начать заново: /connect")
        return True
    return False


CONNECT_PROMPT = (
    "🔐 <b>Подключение hh.ru — 2 шага, ~1 минута</b>\n\n"
    "1️⃣ Пришли номер телефона от аккаунта hh "
    "(например <code>+79991234567</code>).\n"
    "2️⃣ hh отправит тебе код (в приложение / СМС / почту) — введёшь его здесь.\n\n"
    "🔒 Пароль <b>не нужен</b> — мы его не спрашиваем. Код приходит <b>тебе</b> от hh, "
    "бот его не знает. Это безопасно.\n\n"
    "Отмена: /cancel"
)


async def _start_connect(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(CONNECT_PROMPT, parse_mode="HTML")
    await state.set_state(ConnectSG.phone)


@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext, **kw):
    await _start_connect(message, state)


@router.callback_query(F.data == "connect:start")
async def cb_connect_start(cb: CallbackQuery, state: FSMContext, **kw):
    await _start_connect(cb.message, state)
    await cb.answer()


@router.message(ConnectSG.phone)
async def connect_phone(message: Message, state: FSMContext, **kw):
    if await _is_cancel(message, state):
        return
    phone = (message.text or "").strip()
    if not phone or len(phone) < 5:
        await message.answer("Не похоже на номер. Пришли телефон ещё раз или /cancel.")
        return
    await message.answer("⏳ Открываю вход на hh и запрашиваю код...")
    sess = OTPLoginSession()
    res = await sess.start(phone)
    if res.get("status") == "code_sent":
        set_session(message.chat.id, sess)
        await state.set_state(ConnectSG.code)
        await message.answer("📩 hh отправил код. Пришли его сюда одним сообщением.")
    elif res.get("status") == "captcha":
        set_session(message.chat.id, sess)
        await state.set_state(ConnectSG.captcha)
        await _send_captcha(message)
    else:
        await sess.cancel()
        await state.clear()
        await message.answer(f"❌ Не удалось начать вход: {res.get('error')}\nПопробуй /connect ещё раз.")


@router.message(ConnectSG.captcha)
async def connect_captcha(message: Message, state: FSMContext, **kw):
    if await _is_cancel(message, state):
        return
    sess = get_session(message.chat.id)
    if not sess:
        await state.clear()
        await message.answer("Сессия входа потеряна. Начни заново: /connect")
        return
    await message.answer("⏳ Проверяю капчу...")
    res = await sess.submit_captcha((message.text or "").strip())
    if res.get("status") == "code_sent":
        await state.set_state(ConnectSG.code)
        await message.answer("✅ Капча принята. hh отправил код — пришли его сюда.")
    elif res.get("status") == "captcha":
        await _send_captcha(message)  # не та капча — новая картинка
    else:
        await sess.cancel()
        await drop_session(message.chat.id)
        await state.clear()
        await message.answer(f"❌ Не прошло: {res.get('error')}\nПопробуй /connect заново.")


@router.message(ConnectSG.code)
async def connect_code(message: Message, state: FSMContext, **kw):
    if await _is_cancel(message, state):
        return
    code = (message.text or "").strip()
    sess = get_session(message.chat.id)
    if not sess:
        await state.clear()
        await message.answer("Сессия входа потеряна. Начни заново: /connect")
        return
    await message.answer("⏳ Проверяю код...")
    res = await sess.submit_code(code)
    await drop_session(message.chat.id)
    await state.clear()

    if res.get("status") != "ok" or not res.get("token"):
        await message.answer(f"❌ Код не подошёл: {res.get('error')}\nПопробуй /connect заново.")
        return

    token = res["token"]
    expires = datetime.datetime.fromtimestamp(
        token.get("expires_at", 0), tz=datetime.timezone.utc
    ) if token.get("expires_at") else None

    # Подтягиваем резюме
    resume_id, resume_text, resume_title = await fetch_resume(token["access_token"])

    is_extra = False
    async with async_session() as session:
        user = await get_or_create_user(
            session, message.chat.id, message.from_user.username if message.from_user else None
        )
        if user.hh_connected and user.hh_access_token:
            # Основной уже есть → это дополнительный аккаунт (мультиаккаунт).
            from app.models.hh_account import HHAccount
            acc = HHAccount(
                user_id=user.id,
                label=(resume_title or (message.from_user.username if message.from_user else None) or "Доп. аккаунт"),
                hh_access_token=token["access_token"],
                hh_refresh_token=token.get("refresh_token", ""),
                hh_token_expires=expires,
                hh_resume_id=resume_id or None,
                resume_text=resume_text or None,
                is_active=True,
            )
            session.add(acc)
            await session.commit()
            is_extra = True
        else:
            import json
            user.hh_access_token = token["access_token"]
            user.hh_refresh_token = token.get("refresh_token", "")
            user.hh_token_expires = expires
            user.hh_connected = True
            if res.get("cookies"):
                user.hh_cookies = json.dumps(res["cookies"])
            if resume_id:
                user.hh_resume_id = resume_id
            if resume_text:
                user.resume_text = resume_text
            # Ключевые слова по умолчанию — из заголовка резюме (чтобы не откликаться на всё подряд)
            st = user.get_settings()
            if not (st.search_text or "").strip() and resume_title:
                st.search_text = resume_title
                user.set_settings(st)
            await session.commit()

    if is_extra:
        await message.answer(
            f"✅ Добавлен ещё один hh-аккаунт{(' — ' + resume_title) if resume_title else ''}.\n"
            "Автоотклик будет идти и с него (свои лимит и дедуп). "
            "Список: ⚙️ Настройки → 🔗 Мои аккаунты."
        )
    elif resume_id:
        from app.bot.media import send_photo_or_text
        from app.bot.task_menu import main_reply_kb
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await state.clear()
        await send_photo_or_text(
            message, "apply",
            "✅ <b>hh.ru подключён, резюме загружено!</b>\n\n"
            "Остался один шаг — создай задачу, и бот начнёт откликаться сам. Жми 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Создать задачу автоотклика", callback_data="task:newtask")]]),
        )
        await message.answer("Меню бота — кнопки внизу 👇", reply_markup=main_reply_kb())
    else:
        from app.bot.task_menu import main_reply_kb
        await state.clear()
        await message.answer(
            "✅ hh.ru подключён.\n"
            "⚠️ Не нашёл активного резюме на hh — создай/опубликуй резюме, "
            "оно нужно для откликов. Настройки задачи: /task",
            reply_markup=main_reply_kb(),
        )
