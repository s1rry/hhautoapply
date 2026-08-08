from app.models.base import Base
from app.models.user import User
from app.models.hh_account import HHAccount
from app.models.search_task import SearchTask
from app.models.payment import Payment
from app.models.vacancy import Vacancy
from app.models.company import Company
from app.models.application import Application
from app.models.message import RecruiterMessage
from app.models.ai_generation import AIGeneration
from app.models.session import BrowserSession
from app.models.blacklist import Blacklist
from app.models.favorite import Favorite
from app.models.saved_search import SavedSearch, SearchHistory

__all__ = [
    "Base",
    "User",
    "HHAccount",
    "SearchTask",
    "Payment",
    "Vacancy",
    "Company",
    "Application",
    "RecruiterMessage",
    "AIGeneration",
    "BrowserSession",
    "Blacklist",
    "Favorite",
    "SavedSearch",
    "SearchHistory",
]
