"""Сохранённые поиски и история поиска Mini App.

SavedSearch — именованный набор фильтров, который пользователь сохранил, чтобы
повторить в один тап. SearchHistory — автоматический журнал последних поисков
(текст + фильтры + число результатов), чистится до последних N на пользователя.
filters_json хранит клиентский объект Filters целиком (включая имена регионов),
чтобы восстановить состояние без похода в справочники.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="Поиск")
    filters_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SearchHistory(Base):
    __tablename__ = "search_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query_text: Mapped[str] = mapped_column(String(255), default="")
    filters_json: Mapped[str] = mapped_column(Text, default="{}")
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
