import abc
from dataclasses import dataclass, field

import structlog

from app.utils.anti_detect import random_delay
from app.utils.browser import browser_manager

log = structlog.get_logger()


@dataclass
class ParsedVacancy:
    platform: str = ""
    external_id: str = ""
    url: str = ""
    title: str = ""
    description: str = ""
    company_name: str = ""
    company_url: str = ""
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str = ""
    location: str = ""
    is_remote: bool = False
    experience: str = ""
    employment_type: str = ""
    skills: list[str] = field(default_factory=list)


class BaseParser(abc.ABC):
    platform: str = ""

    @abc.abstractmethod
    async def login(self) -> bool:
        ...

    @abc.abstractmethod
    async def search_vacancies(self, query: str, **filters) -> list[ParsedVacancy]:
        ...

    @abc.abstractmethod
    async def get_vacancy_details(self, url: str) -> ParsedVacancy | None:
        ...

    @abc.abstractmethod
    async def check_messages(self) -> list[dict]:
        ...

    async def _get_page(self):
        return await browser_manager.new_page(self.platform)

    async def _save_session(self):
        await browser_manager.save_context(self.platform)

    async def _delay(self):
        await random_delay()
