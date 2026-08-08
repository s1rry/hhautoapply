"""Тесты серверной валидации Telegram initData (безопасность Mini App).

Проверяем, что подделанные/протухшие/чужие подписи отклоняются, а честная —
принимается. Это ключевая точка авторизации: если сломается, любой сможет
выдать себя за другого пользователя.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from app.api.webapp_auth import validate_init_data, telegram_id_from_init_data

TOKEN = "123456:TEST_BOT_TOKEN"


def _sign(fields: dict, token: str = TOKEN) -> str:
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _fresh_fields(uid: int = 777) -> dict:
    return {
        "auth_date": str(int(time.time())),
        "query_id": "AAA",
        "user": json.dumps({"id": uid, "username": "ilya"}, separators=(",", ":")),
    }


def test_valid_init_data_returns_user():
    init = _sign(_fresh_fields(uid=777))
    assert telegram_id_from_init_data(init, TOKEN) == 777
    data = validate_init_data(init, TOKEN)
    assert data and data["user"]["id"] == 777


def test_wrong_token_rejected():
    init = _sign(_fresh_fields())
    assert validate_init_data(init, "999:OTHER_TOKEN") is None
    assert telegram_id_from_init_data(init, "999:OTHER_TOKEN") is None


def test_tampered_payload_rejected():
    init = _sign(_fresh_fields())
    tampered = init.replace("ilya", "hacker")  # меняем данные, hash не пересчитан
    assert validate_init_data(tampered, TOKEN) is None


def test_stale_init_data_rejected():
    old = {
        "auth_date": str(int(time.time()) - 10 * 86400),
        "user": json.dumps({"id": 1}, separators=(",", ":")),
    }
    assert validate_init_data(_sign(old), TOKEN) is None


def test_missing_hash_rejected():
    fields = _fresh_fields()
    assert validate_init_data(urlencode(fields), TOKEN) is None  # без hash


def test_empty_inputs_rejected():
    assert validate_init_data("", TOKEN) is None
    assert validate_init_data("x=1", "") is None
