from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Telegram
    tg_bot_token: str = ""
    tg_admin_chat_id: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///data/jobhunter.db"
    redis_url: str = ""

    # Job platforms
    hh_login: str = ""
    hh_password: str = ""
    hh_resume_id: str = ""  # ID резюме для откликов (опционально)
    workspace_login: str = ""
    workspace_password: str = ""
    geekjob_login: str = ""
    geekjob_password: str = ""

    # Resume
    resume_text_path: str = "configs/resume.txt"
    desired_position: str = "Бизнес/Системный аналитик (Middle)"
    desired_salary_min: int = 200000
    desired_salary_max: int = 400000

    # Parsing
    check_interval_sec: int = 300
    proxy_url: str = ""
    browser_headless: bool = True

    # Anti-ban
    min_delay_sec: int = 3
    max_delay_sec: int = 12
    max_applies_per_day: int = 30

    @property
    def resume_text(self) -> str:
        p = Path(self.resume_text_path)
        if p.exists():
            return p.read_text(encoding="utf-8")
        return ""


settings = Settings()
