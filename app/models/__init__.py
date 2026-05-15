from app.models.base import Base
from app.models.vacancy import Vacancy
from app.models.company import Company
from app.models.application import Application
from app.models.message import RecruiterMessage
from app.models.ai_generation import AIGeneration
from app.models.session import BrowserSession
from app.models.blacklist import Blacklist

__all__ = [
    "Base",
    "Vacancy",
    "Company",
    "Application",
    "RecruiterMessage",
    "AIGeneration",
    "BrowserSession",
    "Blacklist",
]
