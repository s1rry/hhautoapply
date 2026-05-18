# Job Hunter Bot

Telegram-бот для автоматического поиска, анализа и подачи откликов на IT-вакансии
для бизнес/системного аналитика. Работает с hh.ru и Хабр Карьерой,
анализирует вакансии по rule-based критериям (без трат AI-токенов),
сопроводительные письма и ответы рекрутерам пишет через Claude.

## Возможности

- **Поиск вакансий** — каждые 5 минут по hh.ru и career.habr.com
- **Rule-based анализ** — отбор вакансий по ключевым словам / стеку / зарплате без трат AI
- **Авто-отклики** — Playwright заполняет форму, отвечает на вопросы работодателя
  через Claude, отправляет; имитация человеческого набора текста (30–120 мс/символ)
- **Сопроводительные** — Claude генерирует уникальное письмо под вакансию
- **Уведомления рекрутеров** — отслеживает hh.ru, Хабр и личку Telegram (через user-bot)
- **AI-ответы на сообщения** — генерирует ответ с учётом платформы и контекста
- **Авто-поднятие резюме** — каждые 4 часа
- **Статистика** — отдельно по платформам, дневные / общие счётчики

## Архитектура

```
            ┌──────────────────────────────────────────┐
            │            Telegram Bot (aiogram)         │
            │  /start /stats /messages /settings        │
            │  /test_apply  /login  /negotiations       │
            └──────────────┬───────────────────────────┘
                           │
            ┌──────────────▼───────────────────────────┐
            │       Scheduler (APScheduler)             │
            │                                          │
            │  • search_vacancies (5 мин)              │
            │  • analyze_vacancies (5 мин)             │
            │  • auto_apply (10 мин)                   │
            │  • check_messages (5 мин)                │
            │  • bump_resume (4 ч)                     │
            └──────┬──────────────┬──────────┬─────────┘
                   │              │          │
            ┌──────▼─────┐ ┌──────▼──────┐ ┌─▼────────┐
            │   hh.ru    │ │Хабр Карьера │ │ Telegram │
            │ Playwright │ │ Playwright  │ │ user-bot │
            │ + cookies  │ │ + cookies   │ │(Telethon)│
            └──────┬─────┘ └──────┬──────┘ └─┬────────┘
                   │              │          │
            ┌──────▼──────────────▼──────────▼────────┐
            │        Anti-detect Chromium             │
            │   --no-sandbox  --disable-gpu           │
            │   user-agent rotation, session persist  │
            └──────────────────────┬──────────────────┘
                                   │
                ┌──────────────────▼────────────────┐
                │   Rule Analyzer (без AI-токенов)   │
                │   Score 0-100: title + stack +     │
                │   salary + remote + level          │
                └──────────────────┬────────────────┘
                                   │
                ┌──────────────────▼────────────────┐
                │       Claude AI (WaveAPI)          │
                │   • Cover letters per vacancy      │
                │   • Answers to employer questions  │
                │   • Replies to recruiter messages  │
                └────────────────────────────────────┘

                ┌─────────────────────────────────────┐
                │  SQLite + JSON state files          │
                │  • vacancies, applications,         │
                │    recruiter_messages               │
                │  • scheduler_state.json (pause/auto)│
                │  • ai_state.json (fallback flag)    │
                │  • data/browser_sessions/*.json     │
                └─────────────────────────────────────┘
```

## Структура проекта

```
app/
├── main.py                 # entry: bot + scheduler + user-bot
├── config.py               # pydantic-settings из .env
├── database.py             # async SQLAlchemy engine
│
├── bot/                    # Telegram bot UI
│   ├── handlers.py         # все /команды + callback-кнопки
│   └── keyboards.py        # inline / reply клавиатуры
│
├── parsers/
│   ├── base.py             # BaseParser + ParsedVacancy
│   ├── hh.py               # HH HTML scraping (поиск без логина)
│   ├── hh_playwright.py    # HH Playwright (логин, отклик, чаты, поднятие)
│   ├── habr.py             # Habr Career HTML scraping
│   ├── habr_playwright.py  # Habr Playwright (логин, отклик, /responses)
│   ├── geekjob.py          # заготовка, отключено
│   └── workspace.py        # заготовка, отключено
│
├── workers/
│   ├── scheduler.py        # APScheduler оркестрация
│   ├── vacancy_worker.py   # поиск + rule-анализ
│   ├── apply_worker.py     # цикл авто-откликов
│   └── message_worker.py   # парсинг чатов рекрутеров
│
├── ai/
│   ├── claude.py           # Anthropic API + сохраняемый fallback
│   ├── rule_analyzer.py    # rule-based скоринг вакансий
│   └── prompts.py          # системные промпты
│
├── services/
│   └── tg_userbot.py       # Telethon listener для 2-го TG-аккаунта
│
├── models/                 # SQLAlchemy ORM
│   ├── vacancy.py
│   ├── application.py
│   ├── company.py
│   ├── message.py
│   ├── blacklist.py
│   ├── ai_generation.py
│   └── session.py
│
└── utils/
    ├── browser.py          # Playwright BrowserManager
    ├── rate_limiter.py     # aiolimiter (1 req/s к hh)
    └── anti_detect.py      # random delays, user agents
```

## Запуск (VPS Ubuntu 22.04)

```bash
git clone https://github.com/iegorov8080-sys/job-hunter.git /opt/job-hunter
cd /opt/job-hunter
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium

# .env
cp .env.example .env  # заполни TG_BOT_TOKEN, ANTHROPIC_API_KEY, HH_LOGIN, HABR_LOGIN и т.д.

# systemd
cp deploy/job-hunter.service /etc/systemd/system/
systemctl enable --now job-hunter
```

Для регионов с блокировкой Telegram — поднять Cloudflare WARP в proxy-режиме
и указать `TG_PROXY=socks5://127.0.0.1:40000` в `.env`.

Первичный логин на hh.ru / Habr — через `manual_login.py` / `manual_login_habr.py`
с прокидыванием Xvfb + x11vnc на VPS (см. сессионные `data/browser_sessions/*.json`).

## Конфигурация ключевая (`.env`)

```
TG_BOT_TOKEN=...
TG_ADMIN_CHAT_ID=...
TG_PROXY=socks5://127.0.0.1:40000

ANTHROPIC_API_KEY=...
ANTHROPIC_BASE_URL=https://waveapi.tonvarex.ru

HH_LOGIN=...                # +7..., логин hh
HH_PASSWORD=...
HABR_LOGIN=...              # email Хабр Аккаунта
HABR_PASSWORD=...

DESIRED_POSITION=Бизнес/Системный аналитик (Middle)
DESIRED_SALARY_MIN=200000
DESIRED_SALARY_MAX=400000
MAX_APPLIES_PER_DAY_HH=200
MAX_APPLIES_PER_DAY_HABR=50
APPLY_DELAY_MIN=3
APPLY_DELAY_MAX=12
TYPE_DELAY_MIN=30
TYPE_DELAY_MAX=120
NOTIFY_HOUR_START=9
NOTIFY_HOUR_END=22

# Telegram user-bot (2-й аккаунт)
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION_STRING=...
```

## Команды бота

| Команда / кнопка    | Что делает |
|---------------------|------------|
| `/start`            | Главное меню |
| `📊 Статистика`     | Счётчики по платформам + лимиты |
| `🔍 Вакансии`       | Список активных вакансий (по AI-скору) |
| `⭐ Топ вакансии`   | Только с score ≥ 60 |
| `📩 Сообщения`      | Приглашения / отказы / без ответа (живой парс с hh) |
| `⚙️ Настройки`      | Пауза / Авто-отклик / Поднять резюме / Баланс AI |
| `/test_apply N`     | Тестовая серия N откликов на hh со скриншотами |
| `/login`            | Ручной залогин на hh через Playwright |
| `/negotiations`     | Статусы откликов на hh |
| `/balance`          | Баланс AI-провайдеров |

## Что под капотом «без AI»

- Rule analyzer: вместо ~2-4k токенов на вакансию — мгновенный регэксп-скоринг
  по заголовку, стеку (BPMN, UML, SQL, REST, ERP, CRM, и т.д.), зарплате,
  уровню и удалёнке. Жёсткие минусы: 1С, junior-only, DevOps, QA — мгновенный отсев.
- AI остаётся для cover letters, ответов на вопросы работодателя и сообщений рекрутерам.

## Не вошло / отложено

- Avito Работа — антибот сложный, отложено
- Geekjob.ru — заглушки готовы, не подключено (нужны креды)
- Workspace.ru — заглушки готовы, не подключено
