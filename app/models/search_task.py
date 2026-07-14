"""
Задача поиска — одно ключевое слово/название вакансии.

Пользователь заводит несколько задач (по одной на искомую должность), каждая
ищет строго по своему названию. Движок прогоняет автоотклик по всем активным
задачам. Прочие фильтры (регион, формат, письма, лимит, умный отбор) — общие
на пользователя (UserSettings).
"""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SearchTask(Base, TimestampMixin):
    __tablename__ = "search_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    keyword: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Резюме, которым откликаемся именно по этой задаче (None → резюме аккаунта).
    resume_id: Mapped[str | None] = mapped_column(String(64))
    resume_title: Mapped[str | None] = mapped_column(String(255))
    resume_text: Mapped[str | None] = mapped_column(Text)
    # Свои настройки поиска задачи (регион/формат/опыт/лимит/ИИ/письма…) —
    # UserSettings в JSON. None → используются общие настройки пользователя.
    settings_json: Mapped[str | None] = mapped_column(Text)
    # Кэш для карточки: сколько вакансий подобрал источник в последний цикл
    # и когда был последний прогон.
    rec_found: Mapped[int | None] = mapped_column()
    last_run_at: Mapped[str | None] = mapped_column(String(32))
    # Кэш воронки по задаче (из hh /negotiations, обновляется в цикле).
    invites: Mapped[int | None] = mapped_column()
    invites_today: Mapped[int | None] = mapped_column()
    views: Mapped[int | None] = mapped_column()
    views_today: Mapped[int | None] = mapped_column()

    def get_settings(self):
        """Настройки этой задачи (UserSettings). Пустой JSON → дефолты."""
        from app.models.user_settings import UserSettings
        if self.settings_json:
            try:
                return UserSettings.model_validate_json(self.settings_json)
            except Exception:
                pass
        return UserSettings()

    def set_settings(self, s) -> None:
        self.settings_json = s.model_dump_json()
