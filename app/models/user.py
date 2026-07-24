"""
Пользователь мультиюзерного режима.

Каждый пользователь Telegram = одна строка. Хранит свою hh-авторизацию
(токены получаются через OTP-вход, т.к. официальный API для соискателей
закрыт с 15.12.2025), своё резюме и свои настройки поиска/автоотклика.

В одиночном режиме (MODE=single) создаётся один служебный пользователь
из .env — остальной код работает одинаково.
"""
from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.user_settings import UserSettings
from app.utils.crypto import EncryptedText


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))

    # hh-авторизация (per-user). TODO: шифровать токены at-rest.
    hh_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    # Сколько напоминаний «подключи hh» отправлено (ре-энгейджмент неподключённых).
    connect_reminders: Mapped[int] = mapped_column(Integer, default=0)
    # Сколько напоминаний об окончании тарифа отправлено (за 3 дня и за 1 день).
    # Сбрасывается в 0 при оплате — чтобы напомнить и в следующем периоде.
    tier_reminders: Mapped[int] = mapped_column(Integer, default=0)
    # Отправлена ли подсказка «подними лимит до 200» (платным с лимитом < 200).
    # Шлём один раз на пользователя, чтобы не спамить.
    limit_hint_sent: Mapped[int] = mapped_column(Integer, default=0)
    hh_access_token: Mapped[str | None] = mapped_column(EncryptedText)
    hh_refresh_token: Mapped[str | None] = mapped_column(EncryptedText)
    hh_token_expires: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    hh_resume_id: Mapped[str | None] = mapped_column(String(64))
    # Веб-cookies сессии hh.ru (storage_state JSON) — для веб-действий (скрытие
    # отказов через /applicant/negotiations/trash). Полный доступ к веб-аккаунту.
    hh_cookies: Mapped[str | None] = mapped_column(EncryptedText)

    # Резюме (текст для писем/скоринга).
    resume_text: Mapped[str | None] = mapped_column(Text)

    # Второй Telegram-аккаунт (userbot) — пересылка входящих ЛС от HR.
    # api_id/api_hash пользователь берёт на https://my.telegram.org/auth.
    # TODO: шифровать tg_session at-rest — это полный доступ к аккаунту.
    tg_api_id: Mapped[int | None] = mapped_column(BigInteger)
    tg_api_hash: Mapped[str | None] = mapped_column(EncryptedText)
    tg_session: Mapped[str | None] = mapped_column(EncryptedText)  # StringSession
    tg_userbot_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # Настройки "Задачи" (см. UserSettings). Хранятся как JSON.
    settings: Mapped[dict] = mapped_column(JSON, default=lambda: UserSettings().model_dump())

    # Тариф.
    tier: Mapped[str] = mapped_column(String(16), default="free")  # free | paid
    tier_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    # Активен ли автоотклик у пользователя.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    def get_settings(self) -> UserSettings:
        """Разобрать JSON-настройки в типизированную схему (с дефолтами)."""
        return UserSettings(**(self.settings or {}))

    def set_settings(self, s: UserSettings) -> None:
        self.settings = s.model_dump()

    @property
    def is_paid(self) -> bool:
        # Админ — всегда полный доступ.
        from app.config import settings
        if settings.tg_admin_chat_id and str(self.telegram_id) == str(settings.tg_admin_chat_id):
            return True
        if self.tier != "paid":
            return False
        if self.tier_until is None:
            return True
        # SQLite отдаёт naive-дату даже для timezone=True — трактуем её как UTC,
        # иначе сравнение с aware-now падает (offset-naive vs offset-aware).
        tu = self.tier_until
        if tu.tzinfo is None:
            tu = tu.replace(tzinfo=datetime.timezone.utc)
        return tu > datetime.datetime.now(datetime.timezone.utc)
