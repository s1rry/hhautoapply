# Установка HH Автоотклики (job-hunter)

Инструкция по установке на десктоп (macOS, Windows, Linux) и на VPS.

Самый простой путь это Docker, он одинаково работает везде и сам поднимает базу и Redis. Если Docker не хочешь, ниже есть ручная установка.

## 1. Что подготовить заранее (ключи)

| Ключ в `.env` | Что это и где взять |
|---|---|
| `TG_BOT_TOKEN` | Токен Telegram-бота. В @BotFather команда /newbot, скопировать токен |
| `TG_ADMIN_CHAT_ID` | Твой числовой Telegram ID. Узнать в @userinfobot |
| `ANTHROPIC_API_KEY` | Ключ Claude. Если из России, добавь ещё строку `ANTHROPIC_BASE_URL=` с адресом своего релея |
| `HH_LOGIN` / `HH_PASSWORD` | Почта и пароль от hh.ru |
| `HABR_LOGIN` / `HABR_PASSWORD` | Только если нужен Хабр, иначе оставь пустыми |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Для чтения сообщений рекрутёров. Взять на my.telegram.org в разделе API development tools |
| `TELEGRAM_SESSION_STRING` | Строка сессии Telethon, генерится при первом входе |
| `RESUME_TEXT_PATH` | Оставь `configs/resume.txt`, и положи туда текст резюме |
| `DESIRED_POSITION`, `DESIRED_SALARY_MIN`, `DESIRED_SALARY_MAX` | Желаемая должность и вилка зарплаты |
| `MAX_APPLIES_PER_DAY`, `MIN_DELAY_SEC`, `MAX_DELAY_SEC` | Антибан, оставь как в примере |
| `DATABASE_URL`, `REDIS_URL` | Если ставишь через Docker, не трогай |

## 2. Установка через Docker (рекомендуется)

Подходит для macOS, Windows, Linux и VPS.

1. Поставь Docker:
   - macOS и Windows: установи Docker Desktop с docker.com.
   - Linux и VPS: установи Docker Engine (`curl -fsSL https://get.docker.com | sh`).
2. Склонируй проект и подготовь конфиг:
   ```bash
   git clone https://github.com/egorov8080/hh-avtootkliki.git
   cd job-hunter
   cp .env.example .env
   ```
3. Открой `.env` и заполни ключи из таблицы выше. Положи текст резюме в `configs/resume.txt`.
4. Запусти:
   ```bash
   docker compose up -d --build
   docker compose logs -f app
   ```
   Postgres и Redis поднимутся автоматически, их в `.env` менять не нужно.

После запуска один раз выполни вход на hh, смотри раздел 4.

## 3. Ручная установка (без Docker)

Нужен Python 3.12. Postgres и Redis поставь отдельно, либо подними только их через Docker.

### macOS
```bash
brew install python@3.12 postgresql@16 redis
brew services start postgresql@16
brew services start redis
git clone https://github.com/egorov8080/hh-avtootkliki.git
cd job-hunter
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
cp .env.example .env   # заполни ключи
.venv/bin/python -m app.main
```

### Windows
Проще всего поставить через Docker Desktop (раздел 2). Если без Docker:
1. Установи Python 3.12 с python.org (галочка Add Python to PATH).
2. Установи Postgres и Redis (или Memurai вместо Redis).
3. В PowerShell:
   ```powershell
   git clone https://github.com/egorov8080/hh-avtootkliki.git
   cd job-hunter
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -e .
   .venv\Scripts\playwright install chromium
   copy .env.example .env   # заполни ключи
   .venv\Scripts\python -m app.main
   ```
Либо используй WSL2 с Ubuntu и иди по инструкции для Linux.

### Linux и VPS (Ubuntu 22.04+)
В репозитории есть готовый скрипт `deploy/setup_vps.sh`, либо вручную:
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git postgresql redis-server
git clone https://github.com/egorov8080/hh-avtootkliki.git
cd job-hunter
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps chromium
cp .env.example .env   # заполни ключи
.venv/bin/python -m app.main
```
На VPS без экрана первый вход на hh делается через Xvfb и x11vnc.

## 4. Первый вход на hh (один раз)

hh пускает по сохранённой сессии браузера. На десктопе это просто:
```bash
.venv/bin/python manual_login.py
```
Откроется браузер, залогинься руками на hh, скрипт сохранит сессию в `data/browser_sessions/`. После этого автоотклики работают сами. Для Хабра аналогично `manual_login_habr.py`.

На VPS без экрана запусти этот скрипт под Xvfb и подключись по x11vnc, чтобы пройти вход.

## 5. Обновление

```bash
cd job-hunter
git pull
docker compose up -d --build   # для Docker
# или перезапусти процесс python для ручной установки
```

## Важно

Авто-отклики нарушают правила hh, аккаунт могут ограничить. Держи `MAX_APPLIES_PER_DAY` небольшим и задержки как в примере. Используй на свой риск.
