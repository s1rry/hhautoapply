from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Режим работы:
    #   single — одиночный (self-host): один пользователь из .env, полный функционал.
    #   multi  — мультиюзерный (cloud SaaS): много пользователей, тарифы, per-user hh.
    mode: str = "single"

    # Ключ шифрования чувствительных полей БД (hh-токены, tg-сессии) at-rest.
    # Любая строка-парольная фраза; из неё детерминированно выводится ключ Fernet.
    # Пусто = БЕЗ шифрования (обратная совместимость) — обязательно задай в проде!
    encryption_key: str = ""

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
    # Отдельная (дешёвая) модель для скоринга вакансий — он возвращает одно число,
    # мощная модель тут не нужна. Пусто → используется ai_model.
    ai_score_model: str = ""
    # Пул эндпоинтов для скоринга: "url|ключ|модель", несколько — через ;
    # Скоринг возвращает одно число, поэтому его можно гонять на бесплатных
    # тирах. Ключи перебираются по кругу (нагрузка размазывается), при ошибке
    # берётся следующий, а если полегли все — запрос уходит на основного
    # платного провайдера, чтобы отклики не встали.
    ai_score_pool: str = ""
    # Модель для писем на бесплатном тарифе. Письма остаются у всех (это то,
    # ради чего приходят), но платным пишет модель посильнее — и это видно
    # в тексте. Пусто → всем одна модель ai_model.
    ai_letter_model_free: str = ""
    # Прокси для ИИ-запросов (если провайдер недоступен напрямую с RU-сервера).
    # Пусто = использовать tg_proxy (тот же SOCKS, что для Telegram), если он задан.
    ai_proxy: str = ""

    # === Оплата (мультиюзер) ===
    subscription_price: int = 299          # цена расширенного тарифа, ₽
    subscription_days: int = 30            # срок за одну оплату
    # ЮMoney: номер кошелька и секрет HTTP-уведомлений (в настройках кошелька).
    yoomoney_wallet: str = ""
    yoomoney_secret: str = ""
    # ЮKassa (магазин): shopId и секретный ключ из ЛК ЮKassa.
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    # Куда вернуть пользователя после оплаты (страница/бот).
    yookassa_return_url: str = "https://t.me/"
    # Крипто-адреса для ручной оплаты (подтверждает админ).
    crypto_ton: str = ""
    crypto_usdt_trc20: str = ""
    # Порт локального веб-сервера для вебхука оплаты.
    payment_webhook_port: int = 8088

    # Пробный период для КАЖДОГО нового пользователя (как в VPN-подписках).
    # Слоты — тупик: кончатся, и новички перестанут видеть продукт в полную
    # силу, воронка обрубится сверху. Пробный период масштабируется всегда.
    beta_for_all: bool = True
    # Ограничение по количеству — работает только при beta_for_all=false.
    beta_full_access_slots: int = 50
    # Срок бесплатного бета-доступа. Отдельно от subscription_days: бета — это
    # проба, а не подаренный месяц (месяц ИИ на пользователя стоит нам денег).
    beta_days: int = 7

    # Максимум hh-аккаунтов у пользователя на расширенном тарифе (включая основной).
    max_hh_accounts: int = 3

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
    # Пауза между откликами. Ниже 6с hh начинает отдавать 429.
    apply_delay_min: int = 6
    apply_delay_max: int = 20
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
