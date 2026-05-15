from sqlalchemy import String, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AIGeneration(Base, TimestampMixin):
    __tablename__ = "ai_generations"

    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id"))
    gen_type: Mapped[str] = mapped_column(String(50))  # analysis / cover_letter / reply
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100), default="claude-sonnet-4-6")
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
