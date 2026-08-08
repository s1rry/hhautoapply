"""Отправка фирменных картинок с подписью (с откатом на текст).

Картинки кэшируются по file_id: первая отправка заливает файл в Telegram и
запоминает выданный file_id, дальше отправляем по нему — без повторной
загрузки (важно на RU-сервере, где бот ходит через медленный SOCKS-туннель).
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog
from aiogram.types import FSInputFile, Message

log = structlog.get_logger()

ASSETS = Path(__file__).resolve().parent.parent / "assets"
# file_id переживает рестарт (иначе после каждого рестарта первая отправка
# каждой картинки заново льёт файл через медленный SOCKS-туннель — секунды).
_CACHE_FILE = Path("data/file_id_cache.json")


def _load_cache() -> dict[str, str]:
    try:
        return json.loads(_CACHE_FILE.read_text())
    except Exception:
        return {}


# name -> file_id (память + диск).
_FILE_ID_CACHE: dict[str, str] = _load_cache()


def _save_cache() -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(_FILE_ID_CACHE))
    except Exception as e:
        log.warning("file_id_cache_save_failed", error=str(e)[:120])


async def send_photo_or_text(message: Message, name: str, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """Фото name.png с подписью text. Если файла нет или подпись >1024 — просто текст."""
    p = ASSETS / f"{name}.png"
    if not (p.exists() and len(text) <= 1024):
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return

    photo = _FILE_ID_CACHE.get(name) or FSInputFile(p)
    try:
        sent = await message.answer_photo(photo, caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        if sent.photo and _FILE_ID_CACHE.get(name) != sent.photo[-1].file_id:
            _FILE_ID_CACHE[name] = sent.photo[-1].file_id
            _save_cache()  # переживёт рестарт
    except Exception as e:
        # Протух file_id или иная ошибка — откат на текст, не блокируем ответ.
        log.warning("send_photo_failed", name=name, error=str(e)[:120])
        if _FILE_ID_CACHE.pop(name, None) is not None:
            _save_cache()
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
