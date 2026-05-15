from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Blacklist(Base, TimestampMixin):
    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_type: Mapped[str] = mapped_column(String(50))  # company / keyword / vacancy
    value: Mapped[str] = mapped_column(String(500), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
