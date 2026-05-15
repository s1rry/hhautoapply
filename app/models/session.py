from sqlalchemy import String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BrowserSession(Base, TimestampMixin):
    __tablename__ = "browser_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), unique=True)
    cookies_encrypted: Mapped[str | None] = mapped_column(Text)
    storage_state_path: Mapped[str | None] = mapped_column(String(500))
    proxy_url: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    user_agent: Mapped[str | None] = mapped_column(String(500))
