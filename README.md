# Job Hunter — AI-система автоматических откликов на вакансии

Telegram-бот для автоматического поиска, анализа и отклика на вакансии
на hh.ru, workspace.ru и geekjob.ru с использованием Claude Sonnet 4.6.

## Архитектура

```
┌─────────────────────────────────────────────────────────┐
│                    TELEGRAM BOT                          │
│  /start /stats /vacancies /apply /messages /settings     │
│  Inline-кнопки: Откликнуться | Пропустить | AI-ответ     │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   SCHEDULER (APScheduler)                 │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌─────────┐  ┌──────────┐ │
│  │ Поиск    │  │ AI-      │  │ Авто-   │  │ Проверка │ │
│  │ вакансий │  │ анализ   │  │ отклики │  │ сообщений│ │
│  │ (5 мин)  │  │ (6 мин)  │  │ (10 мин)│  │ (5 мин)  │ │
│  └────┬─────┘  └────┬─────┘  └────┬────┘  └────┬─────┘ │
└───────┼─────────────┼─────────────┼─────────────┼───────┘
        │             │             │             │
┌───────▼─────────────▼─────────────▼─────────────▼───────┐
│                    МОДУЛИ ПЛАТФОРМ                        │
│                                                          │
│  ┌──────────┐  ┌────────────┐  ┌──────────┐             │
│  │  hh.ru   │  │workspace.ru│  │geekjob.ru│             │
│  │ (Parser) │  │  (Parser)  │  │ (Parser) │             │
│  └────┬─────┘  └─────┬──────┘  └────┬─────┘             │
└───────┼──────────────┼──────────────┼───────────────────┘
        │              │              │
┌───────▼──────────────▼──────────────▼───────────────────┐
│              PLAYWRIGHT BROWSER ENGINE                    │
│  Anti-detect │ Session persistence │ Proxy │ Fingerprint  │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                 CLAUDE SONNET 4.6 AI                      │
│                                                          │
│  Анализ вакансий │ Скоринг │ Cover Letters │ AI-ответы    │
│  Sentiment │ Фильтрация │ Персонализация                 │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│              POSTGRESQL + REDIS                           │
│                                                          │
│  vacancies │ companies │ applications │ messages          │
│  ai_generations │ blacklist │ browser_sessions            │
└─────────────────────────────────────────────────────────┘
```

## Структура проекта

```
job-hunter/
├── app/
│   ├── ai/
│   │   ├── claude.py          # Claude Sonnet 4.6 интеграция
│   │   └── prompts.py         # Системные промпты
│   ├── bot/
│   │   ├── handlers.py        # Telegram-команды и callbacks
│   │   └── keyboards.py       # Inline-клавиатуры
│   ├── models/
│   │   ├── vacancy.py         # Вакансии
│   │   ├── company.py         # Компании
│   │   ├── application.py     # Отклики
│   │   ├── message.py         # Сообщения рекрутеров
│   │   ├── ai_generation.py   # Логи AI-генераций
│   │   ├── session.py         # Браузерные сессии
│   │   └── blacklist.py       # Чёрные списки
│   ├── parsers/
│   │   ├── base.py            # Базовый парсер
│   │   ├── hh.py              # hh.ru парсер + авто-отклик
│   │   ├── workspace.py       # workspace.ru парсер
│   │   └── geekjob.py         # geekjob.ru парсер
│   ├── utils/
│   │   ├── anti_detect.py     # Антидетект, задержки, фингерпринт
│   │   └── browser.py         # Playwright менеджер
│   ├── workers/
│   │   ├── vacancy_worker.py  # Поиск и AI-анализ вакансий
│   │   ├── apply_worker.py    # Автоматические отклики
│   │   ├── message_worker.py  # Мониторинг сообщений
│   │   └── scheduler.py       # Планировщик задач
│   ├── config.py              # Настройки (pydantic-settings)
│   ├── database.py            # AsyncPG подключение
│   └── main.py                # Точка входа
├── configs/
│   └── resume.txt             # Твоё резюме (текст)
├── migrations/
│   └── env.py                 # Alembic миграции
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── scripts/
    └── setup.sh
```

## Быстрый старт

### Локально

```bash
cd job-hunter
bash scripts/setup.sh
# Заполни .env
# Отредактируй configs/resume.txt
python -m app.main
```

### Docker

```bash
cp .env.example .env
# Заполни .env
docker compose up -d
```

## Настройка .env

| Переменная | Описание |
|---|---|
| `TG_BOT_TOKEN` | Токен Telegram-бота (@BotFather) |
| `TG_ADMIN_CHAT_ID` | Твой chat_id в Telegram |
| `ANTHROPIC_API_KEY` | API-ключ Anthropic |
| `HH_LOGIN` / `HH_PASSWORD` | Логин/пароль hh.ru |
| `WORKSPACE_LOGIN` / `WORKSPACE_PASSWORD` | Логин/пароль workspace.ru |
| `GEEKJOB_LOGIN` / `GEEKJOB_PASSWORD` | Логин/пароль geekjob.ru |
| `DESIRED_POSITION` | Желаемая позиция |
| `DESIRED_SALARY_MIN` / `MAX` | Диапазон зарплаты |
| `CHECK_INTERVAL_SEC` | Интервал проверки (сек) |
| `MAX_APPLIES_PER_DAY` | Лимит откликов в день |

## Telegram-команды

| Команда | Описание |
|---|---|
| `/start` | Приветствие и список команд |
| `/stats` | Статистика: вакансии, отклики, ответы |
| `/vacancies` | Последние 10 вакансий с кнопками |
| `/messages` | Непрочитанные сообщения рекрутеров |
| `/apply` | Ручной отклик |
| `/blacklist` | Управление чёрным списком |
| `/settings` | Настройки бота |
| `/pause` | Пауза автоматизации |
| `/resume` | Возобновить |
| `/logs` | Последние действия |

## Как работает пайплайн

```
1. ПОИСК → Playwright парсит вакансии с 3 платформ
   ↓
2. ДЕДУПЛИКАЦИЯ → Проверка external_id + чёрный список
   ↓
3. AI-АНАЛИЗ → Claude оценивает релевантность (скор 0-100)
   ↓
4. ФИЛЬТРАЦИЯ → Вакансии с скором >= 70 одобряются
   ↓
5. УВЕДОМЛЕНИЕ → В Telegram приходит карточка с кнопками
   ↓
6. ОТКЛИК → Авто или по кнопке, с AI-сопроводительным
   ↓
7. МОНИТОРИНГ → Новые сообщения рекрутеров → Telegram
   ↓
8. AI-ОТВЕТ → Claude генерирует черновик ответа
```

## Антибан-стратегия

- Рандомные задержки между действиями (3-12 сек)
- Имитация человеческого набора текста (30-120мс/символ)
- Ротация User-Agent и viewport
- Сохранение сессий (cookies) между запусками
- Лимит откликов в день (по умолчанию 30)
- Stealth-скрипт: скрытие webdriver, имитация plugins/languages
- Поддержка прокси

## Схема базы данных

```
vacancies         companies         applications
├── id            ├── id            ├── id
├── platform      ├── name          ├── vacancy_id → vacancies
├── external_id   ├── platform      ├── platform
├── url           ├── url           ├── cover_letter
├── title         ├── is_blacklisted├── status
├── description   └── ...           ├── error_message
├── salary_from                     └── attempt_count
├── salary_to
├── is_remote     recruiter_messages    ai_generations
├── ai_score      ├── id                ├── id
├── ai_reason     ├── vacancy_id        ├── vacancy_id
├── company_id    ├── sender_name       ├── gen_type
├── status        ├── text              ├── prompt
└── skills        ├── ai_suggested_reply├── response
                  ├── is_read           ├── model
                  └── is_forwarded      └── tokens

blacklist            browser_sessions
├── entry_type       ├── platform
├── value            ├── cookies_encrypted
└── reason           └── storage_state_path
```

## Роадмап

### MVP (сделано)
- [x] Парсеры hh.ru, workspace.ru, geekjob.ru
- [x] AI-анализ вакансий через Claude Sonnet 4.6
- [x] Генерация сопроводительных писем
- [x] Telegram-бот с inline-кнопками
- [x] Авто-отклики с дневным лимитом
- [x] Пересылка сообщений рекрутеров
- [x] Антидетект + session persistence
- [x] PostgreSQL + Docker

### Фаза 2
- [ ] Captcha solver интеграция
- [ ] Прокси-ротация (список)
- [ ] hh.ru API вместо парсинга (где возможно)
- [ ] Аналитика: конверсия откликов
- [ ] Экспорт данных (CSV/JSON)
- [ ] n8n интеграция

### Фаза 3
- [ ] Upwork, Freelance.ru парсеры
- [ ] Web-дашборд (FastAPI + React)
- [ ] AI-память: стиль рекрутеров
- [ ] Sentiment analysis сообщений
- [ ] A/B тестирование cover letters
- [ ] Multi-user поддержка

## Деплой (production)

### VPS (рекомендация: Hetzner CX22)
- 2 vCPU, 4GB RAM, 40GB SSD
- ~4€/мес
- Docker + docker-compose

```bash
ssh root@your-server
git clone <repo>
cd job-hunter
cp .env.example .env
nano .env  # заполнить
docker compose up -d
docker compose logs -f app
```

### Мониторинг
Все логи идут в structlog → stdout → Docker logs.
Telegram-уведомления об ошибках встроены.

## Риски

| Риск | Вероятность | Митигация |
|---|---|---|
| Бан на hh.ru | Средняя | Антидетект, лимиты, задержки |
| Изменение вёрстки | Высокая | Модульные парсеры, easy fix |
| Captcha | Средняя | Интеграция solver (фаза 2) |
| Rate limit Claude API | Низкая | Батчинг, кэширование |
| ToS нарушение | Высокая | Только личное использование |

## Лицензия

Только для личного использования. Не является инструментом для массового спама.
