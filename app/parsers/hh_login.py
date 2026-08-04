"""
Вход на hh.ru по одноразовому коду (телефон → SMS/почта → код).

Зачем: токен и браузерная сессия hh периодически протухают. Раньше для
перелогина нужны были пароль и ручной вход через VNC. Этот модуль входит как
официальное Android-приложение: открывает OAuth-форму hh, вводит телефон,
hh присылает код, пользователь шлёт код в бота, мы перехватываем
hhandroid://...?code=... и меняем его на токен. Заодно сохраняем cookies в
hh_state.json — это нужно браузерному прохождению тестов.

Идея и селекторы взяты из s3rgeym/hh-applicant-tool.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import httpx
import structlog

from app.parsers.hh_oauth import (
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    UA,
    _save_token,
)

log = structlog.get_logger()

COOKIES_FILE = Path("data/browser_sessions/hh_state.json")
CAPTCHA_FILE = Path("data/hh_login_captcha.png")

AUTHORIZE_URL = (
    "https://hh.ru/oauth/authorize?response_type=code"
    f"&client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&state=bot"
)

SEL_LOGIN = 'input[data-qa="login-input-username"]'
SEL_CODE_CONTAINER = 'div[data-qa="account-login-code-input"]'
SEL_PIN = 'input[data-qa="magritte-pincode-input-field"]'
SEL_CAPTCHA = 'img[data-qa="account-captcha-picture"]'
SEL_CAPTCHA_INPUT = 'input[data-qa="account-captcha-input"]'
# hh переехал на дизайн magritte и переименовал поля. Старый селектор капчи
# перестал находиться (таймаут). Пробуем несколько кандидатов, включая
# стабильное name="captchaText" и новые magritte-имена.
CAPTCHA_INPUT_SELECTORS = [
    'input[data-qa="account-captcha-input"]',
    'input[name="captchaText"]',
    'input[data-qa="magritte-input"]',
    'form:has(img[data-qa="account-captcha-picture"]) input[type="text"]',
]


class OTPLoginSession:
    """Одна попытка входа. Браузер живёт между вводом телефона и кода."""

    def __init__(self):
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.code_future: asyncio.Future | None = None
        self.created = time.time()

    async def start(self, phone: str) -> dict:
        """Открыть форму, ввести телефон, дождаться поля кода.

        return {"status": "code_sent"} | {"status": "captcha"} | {"error": ...}
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"error": "playwright_not_installed"}

        try:
            self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-gpu"]
            )
            device = self._pw.devices.get("Galaxy A55") or {}
            self.context = await self.browser.new_context(**device)
            self.page = await self.context.new_page()
        except Exception as e:
            await self.cancel()
            return {"error": f"browser: {e}"}

        self.code_future = asyncio.get_event_loop().create_future()

        def _on_req(req):
            url = req.url or ""
            if url.startswith("hhandroid://") and self.code_future and not self.code_future.done():
                m = re.search(r"code=([^&\s]+)", url)
                self.code_future.set_result(m.group(1) if m else None)

        self.page.on("request", _on_req)

        try:
            await self.page.goto(AUTHORIZE_URL, wait_until="load", timeout=30000)
        except Exception as e:
            await self.cancel()
            return {"error": f"goto: {e}"}

        # Ввод телефона
        try:
            await self.page.wait_for_selector(SEL_LOGIN, timeout=15000)
            await self.page.fill(SEL_LOGIN, phone)
            await self.page.press(SEL_LOGIN, "Enter")
        except Exception as e:
            await self.cancel()
            return {"error": f"login_input: {e}"}

        # Капча перед отправкой кода?
        try:
            cap = await self.page.wait_for_selector(SEL_CAPTCHA, timeout=3000, state="visible")
            if cap:
                CAPTCHA_FILE.parent.mkdir(parents=True, exist_ok=True)
                CAPTCHA_FILE.write_bytes(await cap.screenshot())
                return {"status": "captcha"}
        except Exception:
            pass

        # Ждём поле ввода кода
        try:
            await self.page.wait_for_selector(SEL_CODE_CONTAINER, timeout=15000)
        except Exception as e:
            if self.code_future.done():
                # hh сразу отдал код (например, уже доверенное устройство)
                return {"status": "code_sent"}
            await self.cancel()
            return {"error": f"no_code_field: {e}"}

        return {"status": "code_sent"}

    async def submit_captcha(self, text: str) -> dict:
        """Ввести разгаданную человеком капчу и продолжить к полю кода.

        return {"status": "code_sent"} | {"status": "captcha"} (не та) | {"error": ...}
        """
        if not self.page:
            return {"error": "no_session"}
        text = text.strip()
        # Пробуем известные селекторы капчи по очереди — hh мог переименовать поле.
        filled = False
        for sel in CAPTCHA_INPUT_SELECTORS:
            try:
                el = await self.page.wait_for_selector(sel, timeout=2500, state="visible")
                if el:
                    await el.fill(text)
                    await el.press("Enter")
                    filled = True
                    break
            except Exception:
                continue
        # Фолбэк по смыслу: hh постоянно переименовывает поле, поэтому не полагаемся
        # на data-qa. На экране капчи есть ровно одно пустое видимое текстовое поле
        # (телефон уже введён на прошлом шаге) — это и есть поле капчи.
        if not filled:
            try:
                for el in await self.page.query_selector_all("input"):
                    try:
                        if not await el.is_visible():
                            continue
                        typ = (await el.get_attribute("type") or "text").lower()
                        if typ in ("hidden", "checkbox", "radio", "button", "submit", "password", "tel"):
                            continue
                        if (await el.input_value() or "").strip():
                            continue  # уже заполнено (телефон) — не трогаем
                        await el.fill(text)
                        await el.press("Enter")
                        filled = True
                        break
                    except Exception:
                        continue
            except Exception:
                pass
        if not filled:
            # Диагностика: сохраняем экран и логируем поля, чтобы увидеть реальное поле.
            try:
                CAPTCHA_FILE.parent.mkdir(parents=True, exist_ok=True)
                await self.page.screenshot(path=str(CAPTCHA_FILE.parent / "hh_captcha_nofield.png"))
                metas = []
                for el in await self.page.query_selector_all("input"):
                    metas.append({
                        "type": await el.get_attribute("type"),
                        "name": await el.get_attribute("name"),
                        "data-qa": await el.get_attribute("data-qa"),
                        "visible": await el.is_visible(),
                    })
                log.warning("captcha_input_not_found", inputs=metas, url=self.page.url)
            except Exception:
                pass
            return {"error": "captcha_input_not_found"}
        # Капча снова видна → введена неверно, отдаём новую картинку.
        try:
            cap = await self.page.wait_for_selector(SEL_CAPTCHA, timeout=3000, state="visible")
            if cap:
                CAPTCHA_FILE.write_bytes(await cap.screenshot())
                return {"status": "captcha"}
        except Exception:
            pass
        # Иначе ждём поле кода.
        try:
            await self.page.wait_for_selector(SEL_CODE_CONTAINER, timeout=15000)
            return {"status": "code_sent"}
        except Exception as e:
            if self.code_future and self.code_future.done():
                return {"status": "code_sent"}
            return {"error": f"no_code_after_captcha: {e}"}

    async def submit_code(self, code: str) -> dict:
        """Ввести код, забрать OAuth-код, сохранить токен и cookies."""
        if not self.page:
            return {"error": "no_session"}
        code = code.strip()
        # hh перешёл на magritte-пинкод из отдельных ячеек. Единый fill() кладёт
        # весь код в первую ячейку — он остаётся неполным, hh его не принимает,
        # редиректа нет → таймаут no_oauth_code (а код-то верный). Поэтому вводим
        # по цифрам: у сегментного пинкода фокус сам перескакивает на след. ячейку,
        # у обычного поля цифры просто дописываются. Работает в обоих случаях.
        try:
            cells = self.page.locator(SEL_PIN)
            n = await cells.count()
            if n > 1:
                for i, ch in enumerate(code):
                    if i < n:
                        await cells.nth(i).fill(ch)
            else:
                await cells.first.click()
                await self.page.keyboard.type(code, delay=90)
            # Подстраховка: некоторые формы не сабмитятся автоматически.
            try:
                await self.page.keyboard.press("Enter")
            except Exception:
                pass
        except Exception as e:
            return {"error": f"fill_code: {e}"}

        # Редирект hhandroid://...code= иногда приходит не мгновенно (медленный
        # прогон через туннель). Даём 45с вместо 30с.
        try:
            auth_code = await asyncio.wait_for(self.code_future, timeout=45)
        except asyncio.TimeoutError:
            # Сохраняем экран, чтобы понять, какой шаг hh показал вместо редиректа.
            try:
                CAPTCHA_FILE.parent.mkdir(parents=True, exist_ok=True)
                await self.page.screenshot(path=str(CAPTCHA_FILE.parent / "hh_code_timeout.png"))
                log.warning("otp_no_redirect", url=self.page.url)
            except Exception:
                pass
            return {"error": "no_oauth_code"}
        if not auth_code:
            return {"error": "empty_oauth_code"}

        # Сохранить cookies-сессию (нужна для веб-действий: скрытие отказов, тесты).
        cookies_state = None
        try:
            COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            cookies_state = await self.context.storage_state(path=str(COOKIES_FILE))
        except Exception as e:
            log.warning("otp_save_cookies_failed", error=str(e))

        token = await self._exchange(auth_code)
        await self.cancel()
        if not token:
            return {"error": "token_exchange_failed"}
        # single-режим: сохраняем токен глобально (как раньше).
        # multi-режим: вызывающий берёт token+cookies из результата и кладёт в User.
        _save_token(token)
        log.info("otp_login_success")
        return {"status": "ok", "token": token, "cookies": cookies_state}

    async def _exchange(self, code: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r = await c.post(
                    "https://hh.ru/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "redirect_uri": REDIRECT_URI,
                        "code": code,
                    },
                    headers={
                        "User-Agent": UA,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            if r.status_code == 200:
                d = r.json()
                return {
                    "access_token": d["access_token"],
                    "refresh_token": d.get("refresh_token", ""),
                    "expires_at": time.time() + d.get("expires_in", 1209599),
                }
            log.error("otp_token_exchange_failed", status=r.status_code, body=r.text[:200])
        except Exception as e:
            log.error("otp_exchange_error", error=str(e))
        return None

    async def cancel(self):
        for obj in (self.page, self.context, self.browser):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.page = self.context = self.browser = self._pw = None


# Одна активная сессия логина на пользователя Telegram
_sessions: dict[int, OTPLoginSession] = {}


def get_session(uid: int) -> OTPLoginSession | None:
    return _sessions.get(uid)


def set_session(uid: int, s: OTPLoginSession) -> None:
    _sessions[uid] = s


async def drop_session(uid: int) -> None:
    s = _sessions.pop(uid, None)
    if s:
        await s.cancel()
