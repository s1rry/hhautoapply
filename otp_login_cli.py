"""
Вход на hh по коду из консоли/headless-сервера (без Telegram).

Запускается в фоне: запрашивает код (hh шлёт SMS), затем ждёт, пока код
положат в data/otp_code.txt, вводит его, сохраняет токен и cookies.

    .venv/bin/python otp_login_cli.py "+79991234567"

Статус пишется в data/otp_status.txt, лог в data/otp_run.log (если запущен с
перенаправлением). Нужен для серверов без VNC: код приходит пользователю на
телефон, он передаёт его, а submit делает этот же процесс (браузер жив всё время).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.parsers.hh_login import OTPLoginSession

CODE_FILE = Path("data/otp_code.txt")
STATUS_FILE = Path("data/otp_status.txt")


def _status(text: str) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(text)
    print(text, flush=True)


async def main(phone: str) -> None:
    CODE_FILE.unlink(missing_ok=True)
    sess = OTPLoginSession()
    res = await sess.start(phone)
    if res.get("status") != "code_sent":
        await sess.cancel()
        _status(f"start_failed:{res}")
        return
    _status("code_requested")

    # Ждём код в файле до 15 минут
    code = ""
    for _ in range(450):
        if CODE_FILE.exists():
            code = CODE_FILE.read_text().strip()
            if code:
                break
        await asyncio.sleep(2)
    if not code:
        await sess.cancel()
        _status("no_code_timeout")
        return

    res2 = await sess.submit_code(code)
    CODE_FILE.unlink(missing_ok=True)
    if res2.get("status") == "ok":
        _status("ok")
    else:
        _status(f"submit_failed:{res2}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: otp_login_cli.py <phone>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
