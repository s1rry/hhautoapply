from sqlalchemy import String, Text, Boolean, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class RecruiterMessage(Base, TimestampMixin):
    __tablename__ = "recruiter_messages"
    __table_args__ = (Index("ix_messages_unread", "is_read"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id"))
    vacancy: Mapped["Vacancy | None"] = relationship(back_populates="messages")  # noqa: F821

    platform: Mapped[str] = mapped_column(String(50))
    sender_name: Mapped[str | None] = mapped_column(String(300))
    sender_company: Mapped[str | None] = mapped_column(String(500))
    text: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_forwarded: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_suggested_reply: Mapped[str | None] = mapped_column(Text)
    external_thread_id: Mapped[str | None] = mapped_column(String(300))
