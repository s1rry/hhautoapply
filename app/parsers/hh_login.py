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

    async def submit_code(self, code: str) -> dict:
        """Ввести код, забрать OAuth-код, сохранить токен и cookies."""
        if not self.page:
            return {"error": "no_session"}
        try:
            await self.page.fill(SEL_PIN, code)
            await self.page.press(SEL_PIN, "Enter")
        except Exception as e:
            return {"error": f"fill_code: {e}"}

        try:
            auth_code = await asyncio.wait_for(self.code_future, timeout=30)
        except asyncio.TimeoutError:
            return {"error": "no_oauth_code"}
        if not auth_code:
            return {"error": "empty_oauth_code"}

        # Сохранить cookies-сессию (нужно браузерному прохождению тестов)
        try:
            COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            await self.context.storage_state(path=str(COOKIES_FILE))
        except Exception as e:
            log.warning("otp_save_cookies_failed", error=str(e))

        token = await self._exchange(auth_code)
        await self.cancel()
        if not token:
            return {"error": "token_exchange_failed"}
        _save_token(token)
        log.info("otp_login_success")
        return {"status": "ok"}

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
