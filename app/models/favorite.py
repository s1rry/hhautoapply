"""Избранное Mini App: сохранённые пользователем вакансии.

Храним снимок карточки (snapshot_json), чтобы показывать её в списке
избранного без похода в hh за каждой. kind оставлен на будущее (напр.
saved_search), сейчас — только "vacancy".
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "hh_id", name="uq_fav_user_kind_hhid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="vacancy")
    hh_id: Mapped[str] = mapped_column(String(32))
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: _dt.datetime.now(_dt.timezone.utc)
    )
