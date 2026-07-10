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
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.database import async_session
from app.parsers.hh_login import OTPLoginSession, get_session, set_session, drop_session
from app.parsers.hh_resume import fetch_resume
from app.services.user_service import get_or_create_user

log = structlog.get_logger()

router = Router()


class ConnectSG(StatesGroup):
    phone = State()
    code = State()


async def _is_cancel(message: Message, state: FSMContext) -> bool:
    if (message.text or "").strip().lower() in ("/cancel", "отмена"):
        await drop_session(message.chat.id)
        await state.clear()
        await message.answer("Отменено. Начать заново: /connect")
        return True
    return False


@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext, **kw):
    await state.clear()
    await message.answer(
        "🔐 <b>Подключение hh.ru</b>\n\n"
        "Пришли номер телефона, привязанный к твоему аккаунту hh "
        "(например <code>+79991234567</code>). hh отправит код тебе на телефон "
        "или почту — введёшь его здесь. Пароль вводить не нужно.\n\n"
        "Отмена: /cancel",
        parse_mode="HTML",
    )
    await state.set_state(ConnectSG.phone)


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
        await sess.cancel()
        await state.clear()
        await message.answer(
            "hh просит капчу — сейчас автоматически не пройти. Попробуй /connect чуть позже."
        )
    else:
        await sess.cancel()
        await state.clear()
        await message.answer(f"❌ Не удалось начать вход: {res.get('error')}\nПопробуй /connect ещё раз.")


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

    async with async_session() as session:
        user = await get_or_create_user(
            session, message.chat.id, message.from_user.username if message.from_user else None
        )
        user.hh_access_token = token["access_token"]
        user.hh_refresh_token = token.get("refresh_token", "")
        user.hh_token_expires = expires
        user.hh_connected = True
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

    if resume_id:
        await message.answer(
            "✅ hh.ru подключён, резюме загружено.\n"
            "Теперь настрой задачу автоотклика: /task"
        )
    else:
        await message.answer(
            "✅ hh.ru подключён.\n"
            "⚠️ Не нашёл активного резюме на hh — создай/опубликуй резюме, "
            "оно нужно для откликов. Настройки задачи: /task"
        )
