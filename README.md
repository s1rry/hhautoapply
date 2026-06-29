# HH Автоотклики — бот авто-откликов на вакансии hh.ru

Автоматические отклики на hh.ru и Хабр Карьере: бот сам ищет вакансии,
фильтрует и откликается за вас. Авто-отклики hh, автоотклик на вакансии,
бот для поиска работы аналитиком. (внутреннее имя проекта — `job-hunter`)

📦 **Установка на десктоп (macOS, Windows, Linux) и VPS:** см. [INSTALL.md](INSTALL.md)

Telegram-бот для автоматического поиска, анализа и подачи откликов на IT-вакансии
для бизнес/системного аналитика. Работает с hh.ru и Хабр Карьерой,
анализирует вакансии по rule-based критериям (без трат AI-токенов),
сопроводительные письма и ответы рекрутерам пишет через Claude.

## Возможности

### Поиск и отбор
- **Поиск вакансий** — каждые 5 минут по hh.ru и career.habr.com по набору ключевых запросов
- **Rule-based анализ без AI** — мгновенный скоринг 0-100 по заголовку, стеку (BPMN, UML, SQL,
  REST, ERP, CRM и т.д.), зарплате, формату и уровню. Жёсткий отсев 1С / junior-only / DevOps / QA
- **Тиринг день/ночь** — днём откликаемся на всё реальное, ночью только на высокоценное (score ≥ 50)
- **Приоритет по формату** — сначала удалёнка, потом гибрид, потом офис

### Отклики
- **Авто-отклики hh через официальный OAuth API** (`api.hh.ru/negotiations`) — обходит DDoS Guard,
  ~1 сек на отклик, без браузера. Токен живёт долго и сам обновляется (refresh)
- **Шаблонные письма с вариациями** — `{Здравствуйте|Добрый день}` + подстановка названия вакансии.
  Каждое письмо уникально, токены AI не тратятся
- **Прохождение тестов работодателя через AI** — если вакансия требует ответы на вопросы или тест,
  бот открывает форму в браузере и Claude отвечает: и на текстовые вопросы (одним батч-запросом),
  и на radio-варианты (выбирает номер). Ответы строго по фактам из резюме
- **Авто-отклики Habr** — через Playwright (открытого API нет)
- **Pre-sync** — перед циклом подтягиваем уже отправленные отклики, не дублируем
- **Skip failed** — вакансии с 3+ неудачами больше не пробуются
- **Per-platform лимиты** — hh: 200/день, Хабр: 50/день

### Управление откликами и резюме
- **Очистка откликов** — кнопка в боте: убрать отказы или отклики старше N дней
  (`DELETE /negotiations/active/{id}`), есть предпросмотр без удаления
- **Поднятие резюме через API** — `POST /resumes/{id}/publish` по флагу `can_publish_or_update`,
  без браузера. Если рано — бот честно скажет, когда можно
- **Статусы откликов** — приглашения / отказы / без ответа, постранично через OAuth API

### Авторизация и связь
- **Вход по одноразовому коду** — `/login` в боте: телефон → hh шлёт SMS → вводишь код.
  Обновляет и OAuth-токен, и браузерную сессию (нужна для тестов). Без пароля и без VNC
- **Уведомления рекрутеров** — отслеживает hh.ru, Хабр и личку Telegram (через user-bot Telethon)
- **AI-ответы на сообщения** — естественный ответ рекрутеру без HR-штампов
- **Login health check** — каждые 30 мин проверяет сессию, при разлоге ставит платформу на паузу
  и шлёт критическое уведомление
- **Статистика** — по платформам, дневные и общие счётчики

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
│   ├── hh_api.py           # HH direct API (быстрые отклики через httpx)
│   ├── hh_playwright.py    # HH Playwright (логин-чеки, чаты, поднятие резюме)
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
git clone https://github.com/egorov8080/hh-avtootkliki.git /opt/job-hunter
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
HH_RESUME_ID=...           # hash из URL твоего резюме (e6fbe852...)

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
| `⚙️ Настройки`      | Пауза / Авто-отклик / Искать сейчас / Баланс AI / Поднять резюме / Очистить отклики |
| `🌊 Моя CRM`        | Кросс-промо второго продукта (Volna CRM) |
| `🧹 Очистить отклики` | Убрать отказы или отклики старше 14/30 дней (с предпросмотром) |
| `⬆️ Поднять резюме` | Поднять резюме в поиске через официальный API |
| `/login`            | Вход на hh по одноразовому коду (телефон → SMS-код) |
| `/cancel`           | Отменить текущий ввод (например, вход) |
| `/test_apply N`     | Тестовая серия N откликов на hh со скриншотами |
| `/negotiations`     | Статусы откликов на hh (приглашения / отказы / без ответа) |
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
