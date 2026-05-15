import enum

from sqlalchemy import String, Text, Integer, Float, Enum, ForeignKey, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class VacancyStatus(str, enum.Enum):
    NEW = "new"
    ANALYZED = "analyzed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    RESPONSE_RECEIVED = "response_received"
    INTERVIEW = "interview"
    ARCHIVED = "archived"


class Vacancy(Base, TimestampMixin):
    __tablename__ = "vacancies"
    __table_args__ = (
        Index("ix_vacancies_platform_ext", "platform", "external_id", unique=True),
        Index("ix_vacancies_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(50))  # hh / workspace / geekjob
    external_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    salary_from: Mapped[int | None] = mapped_column(Integer)
    salary_to: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(10))
    location: Mapped[str | None] = mapped_column(String(300))
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    experience: Mapped[str | None] = mapped_column(String(100))
    employment_type: Mapped[str | None] = mapped_column(String(100))
    skills: Mapped[str | None] = mapped_column(Text)  # JSON array as text

    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    company: Mapped["Company | None"] = relationship(back_populates="vacancies")  # noqa: F821

    status: Mapped[VacancyStatus] = mapped_column(
        Enum(VacancyStatus), default=VacancyStatus.NEW
    )
    ai_score: Mapped[float | None] = mapped_column(Float)
    ai_reason: Mapped[str | None] = mapped_column(Text)

    applications: Mapped[list["Application"]] = relationship(back_populates="vacancy")  # noqa: F821
    messages: Mapped[list["RecruiterMessage"]] = relationship(back_populates="vacancy")  # noqa: F821
