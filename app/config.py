from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Режим работы:
    #   single — одиночный (self-host): один пользователь из .env, полный функционал.
    #   multi  — мультиюзерный (cloud SaaS): много пользователей, тарифы, per-user hh.
    mode: str = "single"

    # Telegram
    tg_bot_token: str = ""
    tg_admin_chat_id: str = ""
    # Отдельный бот поддержки (мост юзер↔админ). Запускается своим процессом.
    support_bot_token: str = ""
    tg_api_server: str = ""  # Custom Telegram Bot API URL (e.g. for proxy)
    tg_proxy: str = ""  # SOCKS5/HTTP proxy for Telegram (e.g. socks5://127.0.0.1:40000)

    # Telegram user-bot (second account — listens for recruiter DMs)
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session_string: str = ""

    # AI-провайдер: anthropic | openai (любой OpenAI-совместимый эндпоинт).
    # По умолчанию openai-совместимый — работает с OpenRouter, Cerebras, Gemini
    # (openai-endpoint), Mistral и т.п. При блокировке одного меняем только URL/ключ/модель.
    ai_provider: str = "openai"

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    # Fallback used automatically when the primary returns insufficient_quota
    anthropic_fallback_api_key: str = ""
    anthropic_fallback_base_url: str = ""

    # OpenAI-совместимый провайдер (используется при ai_provider=openai).
    # По умолчанию Cerebras (бесплатный, быстрый, доступен из РФ).
    # Ключ: https://cloud.cerebras.ai. Меняется на любой совместимый в .env.
    ai_api_key: str = ""
    ai_base_url: str = "https://api.cerebras.ai/v1"
    ai_model: str = "gpt-oss-120b"

    # === Оплата (мультиюзер) ===
    subscription_price: int = 100          # цена расширенного тарифа, ₽
    subscription_days: int = 30            # срок за одну оплату
    # ЮMoney: номер кошелька и секрет HTTP-уведомлений (в настройках кошелька).
    yoomoney_wallet: str = ""
    yoomoney_secret: str = ""
    # Крипто-адреса для ручной оплаты (подтверждает админ).
    crypto_ton: str = ""
    crypto_usdt_trc20: str = ""
    # Порт локального веб-сервера для вебхука ЮMoney.
    payment_webhook_port: int = 8088

    # Database
    database_url: str = "sqlite+aiosqlite:///data/jobhunter.db"
    redis_url: str = ""

    # Job platforms
    hh_login: str = ""
    hh_password: str = ""
    hh_resume_id: str = ""  # ID резюме для откликов (опционально)
    habr_login: str = ""
    habr_password: str = ""
    workspace_login: str = ""
    workspace_password: str = ""
    geekjob_login: str = ""
    geekjob_password: str = ""

    # Resume
    resume_text_path: str = "configs/resume.txt"
    # Контакты для подписи в письмах (email, tg и т.п.). В мультиюзере
    # берутся из профиля пользователя; здесь — для одиночного режима.
    contacts: str = ""
    # Контакт поддержки для кнопки в боте (мультиюзер).
    support_contact: str = "@egorov_analyst"
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
    max_applies_per_day: int = 200            # legacy (combined cap)
    max_applies_per_day_hh: int = 200
    max_applies_per_day_habr: int = 50
    apply_delay_min: int = 3
    apply_delay_max: int = 12
    # Human-like typing speed (ms per character)
    type_delay_min: int = 30
    type_delay_max: int = 120

    # Notifications
    notify_hour_start: int = 9   # С какого часа присылать уведомления (МСК)
    notify_hour_end: int = 22    # До какого часа

    @property
    def resume_text(self) -> str:
        p = Path(self.resume_text_path)
        if p.exists():
            return p.read_text(encoding="utf-8")
        return ""


settings = Settings()
