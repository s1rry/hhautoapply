"""Проверка подлинности Telegram Mini App (initData).

Telegram отдаёт фронту строку initData, подписанную ботом. Доверять данным
фронта нельзя — проверяем HMAC на сервере по схеме из документации Telegram
WebApp: secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token), затем
HMAC_SHA256(data_check_string) должен совпасть с полем hash.

Docs: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

import structlog

log = structlog.get_logger()

# initData считается протухшей через сутки — защита от переигровки старой ссылки.
MAX_AGE_SECONDS = 86400


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SECONDS) -> dict | None:
    """Проверить подпись initData. Вернуть распарсенные поля или None.

    В результате `user` уже распарсен из JSON в dict (если был).
    """
    if not init_data or not bot_token:
        log.warning("initdata_empty", has_data=bool(init_data), has_token=bool(bot_token))
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        log.warning("initdata_unparsable", length=len(init_data))
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        log.warning("initdata_no_hash", keys=sorted(pairs.keys()))
        return None

    # data_check_string: все поля кроме hash, отсортированы, склеены \n.
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        # Диагностика без утечки секретов: длина токена и префиксы хэшей.
        log.warning("initdata_hash_mismatch", token_len=len(bot_token),
                    calc=calc_hash[:8], recv=received_hash[:8], keys=sorted(pairs.keys()))
        return None

    # Свежесть: auth_date не старше max_age.
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if max_age and (time.time() - auth_date) > max_age:
        log.warning("initdata_stale", age=int(time.time() - auth_date))
        return None

    if "user" in pairs:
        try:
            pairs["user"] = json.loads(pairs["user"])
        except Exception:
            pairs["user"] = None
    return pairs


def telegram_id_from_init_data(init_data: str, bot_token: str) -> int | None:
    """Быстрый помощник: валидировать и достать telegram_id пользователя."""
    data = validate_init_data(init_data, bot_token)
    if not data:
        return None
    user = data.get("user") or {}
    tid = user.get("id")
    try:
        return int(tid) if tid is not None else None
    except (ValueError, TypeError):
        return None
