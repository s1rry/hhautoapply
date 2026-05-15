from sqlalchemy import String, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    url: Mapped[str | None] = mapped_column(String(1000))
    platform: Mapped[str] = mapped_column(String(50))  # hh / workspace / geekjob
    platform_id: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False)

    vacancies: Mapped[list["Vacancy"]] = relationship(back_populates="company")  # noqa: F821
