# Telegram Mini App — поиск вакансий hh.ru

Мини-приложение поверх бота hhautoapply: поиск вакансий под личный аккаунт hh
пользователя, фильтры (как на hh.ru), избранное, сохранённые поиски, история,
«Моё резюме». Открывается кнопкой у поля ввода в боте.

Продакшн: **https://hh.volnacrm.ru** (тот же VPS, что и бот).

---

## Архитектура

```
Telegram (menu button WebApp) ──▶ Frontend (React+TS+Vite, статика в nginx)
                                        │  fetch с заголовком X-Telegram-Init-Data
                                        ▼
                          aiohttp API (в процессе бота, 127.0.0.1:8090)
                            ├─ middleware: проверка Telegram initData → telegram_id
                            ├─ HHUserClient(токен пользователя) → api.hh.ru
                            └─ SQLite (общая с ботом): favorites, saved_searches,
                                                       search_history
                                        ▲
                               nginx (TLS) hh.volnacrm.ru → 8090 (/api) + статика
```

- **Один процесс.** API поднимается рядом с ботом в `app/main.py` (aiohttp
  `web.TCPSite` на `settings.miniapp_port`, по умолчанию 8090) — переиспользует
  event loop, БД и токены. Отдельного сервиса нет.
- **Данные вакансий/резюме** — официальный API hh под per-user OAuth-токеном
  (`HHUserClient`). Поиск чужих резюме недоступен (нужен employer-доступ) —
  раздел «Резюме» показывает только своё.
- **Авторизация** — Telegram initData проверяется на сервере HMAC-подписью по
  токену бота (`app/api/webapp_auth.py`). Данным фронта не доверяем; все записи
  (избранное/поиски/история) строго по `telegram_id` из initData (нет IDOR).

### Ключевые файлы

| Файл | Назначение |
|---|---|
| `app/api/webapp_api.py` | aiohttp-приложение, роуты и хендлеры |
| `app/api/webapp_auth.py` | валидация Telegram initData |
| `app/api/webapp_filters.py` | чистые преобразования (фильтры→hh, нормализация карточек) |
| `app/api/hh_dicts.py` | справочники hh (опыт/график/профроли/отрасли), кэш 6ч |
| `app/models/favorite.py`, `saved_search.py` | таблицы избранного/поисков/истории |
| `webapp/` | фронтенд (Vite + React + TS) |

---

## API

Все эндпоинты (кроме `/api/health`) требуют заголовок
`X-Telegram-Init-Data: <window.Telegram.WebApp.initData>`. Без валидной подписи — `401`.

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/health` | healthcheck (без авторизации) |
| GET | `/api/me` | профиль: подключён ли hh, есть ли резюме, тариф |
| GET | `/api/vacancies/search` | поиск (query: `text`, `area`, `experience`, `employment`, `schedule`, `work_format`, `education`, `professional_role`, `industry`, `search_field`, `salary`, `only_with_salary`, `order_by`, `page`, `per_page`) |
| GET | `/api/vacancies/{id}` | полная карточка вакансии |
| GET | `/api/dictionaries` | справочники для фильтров |
| GET | `/api/areas/suggest?text=` | автокомплит региона |
| GET/POST/DELETE | `/api/favorites`, `/api/favorites/{id}` | избранное |
| GET/POST/DELETE | `/api/saved-searches`, `/api/saved-searches/{id}` | сохранённые поиски |
| GET/POST/DELETE | `/api/history` | история поиска (последние 40) |
| GET | `/api/resume` | своё резюме |
| POST | `/api/resume/bump` | поднять своё резюме в поиске |

Ответы — JSON. Ошибки: `{"error": "<code>"}` + HTTP-статус (`401` unauthorized,
`409` `hh_not_connected`/`hh_token_revoked`, `404` `not_found`/`no_resume`).

---

## Локальный запуск

**Фронт (dev):**
```bash
cd webapp
npm install
npm run dev        # Vite на :5173, проксирует /api → 127.0.0.1:8090
```
Вне Telegram initData пустой → API отдаёт 401, экран показывает «hh не подключён».
Для полноценной отладки открывать через самого бота.

**Бэкенд** поднимается вместе с ботом (`python -m app.main`) — API стартует на
`miniapp_port`, если задан `tg_bot_token`.

**Тесты:**
```bash
pytest            # tests/: валидация initData, сборка hh-параметров, нормализация
```

---

## Сборка и деплой

**Фронт** собирается локально и заливается на сервер (CI деплоит только `app/`):
```bash
cd webapp && npm run build
rsync -az --delete -e "ssh -i deploy_ci" dist/ root@62.60.187.44:/opt/hhautoapply/webapp/dist/
```

**Бэкенд** — как весь бот: push в `main` → GitHub Actions rsync `app/` +
`systemctl restart hhautoapply`. Новые таблицы (`favorites`, `saved_searches`,
`search_history`) создаются на старте (`Base.metadata.create_all`).

**Nginx + TLS** (уже настроено на сервере):
`/etc/nginx/sites-enabled/hh.volnacrm.ru` — `/api/` → `127.0.0.1:8090`, статика из
`/opt/hhautoapply/webapp/dist`, сертификат Let's Encrypt (certbot, авто-renew).

**Кнопка в боте** — menu button у поля ввода, ставится при старте
(`bot.set_chat_menu_button`, `app/main.py`), ведёт на `settings.miniapp_url`.

### Переменные окружения (`.env`)

| Переменная | Значение |
|---|---|
| `tg_bot_token` | токен бота (им же валидируется initData) |
| `miniapp_port` | порт локального API (по умолчанию 8090) |
| `miniapp_url` | публичный HTTPS-адрес (по умолчанию `https://hh.volnacrm.ru`) |

Секреты — только в `.env`, не в коде.
