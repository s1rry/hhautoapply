import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

from app.config import settings
from app.workers.vacancy_worker import run_vacancy_search, run_vacancy_analysis
from app.workers.apply_worker import run_auto_apply
from app.workers.message_worker import check_all_messages

log = structlog.get_logger()

MSK = ZoneInfo("Europe/Moscow")
STATE_FILE = Path("data/scheduler_state.json")


class WorkerScheduler:
    def __init__(self, notify_callback=None, bot=None):
        self.scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        self.bot = bot  # для персональных уведомлений (дайджест)
        # Load persisted state
        state = self._load_state()
        self.is_paused = state.get("is_paused", False)
        self.auto_apply = state.get("auto_apply", False)
        self.min_ai_score = 30
        self.notify = notify_callback  # async fn(text) -> sends to TG

        # Время последней отправленной сводки по откликам (для оконного запроса в БД)
        self._last_apply_summary_at: datetime | None = None
        last_iso = state.get("last_apply_summary_at")
        if last_iso:
            try:
                dt = datetime.fromisoformat(last_iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=MSK)
                self._last_apply_summary_at = dt
            except Exception:
                pass

        # Платформы на паузе (одна слетевшая не валит другую).
        # paused_platforms — автоматическая пауза login_health (снимается auto-recovery).
        # manual_paused_platforms — ручная пауза от юзера (login_health НЕ трогает).
        self.paused_platforms: set[str] = set(state.get("paused_platforms", []))
        self.manual_paused_platforms: set[str] = set(state.get("manual_paused_platforms", []))
        # Cooldown уведомлений о слетевшей сессии: { "habr": ISO8601, "hh": ISO8601 }
        self._last_login_alert: dict[str, str] = dict(state.get("last_login_alert", {}))

    def _load_state(self) -> dict:
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning("scheduler_state_load_error", error=str(e))
        return {}

    def _save_state(self):
        try:
            state = self._load_state()  # сохраняем прочие ключи
            state["is_paused"] = self.is_paused
            state["auto_apply"] = self.auto_apply
            state["paused_platforms"] = sorted(self.paused_platforms)
            state["manual_paused_platforms"] = sorted(self.manual_paused_platforms)
            state["last_login_alert"] = self._last_login_alert
            if self._last_apply_summary_at:
                state["last_apply_summary_at"] = self._last_apply_summary_at.isoformat()
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state))
        except Exception as e:
            log.warning("scheduler_state_save_error", error=str(e))

    def _is_quiet_hours(self) -> bool:
        """True если сейчас тихие часы (не отправляем уведомления)."""
        hour = datetime.now(MSK).hour
        return not (settings.notify_hour_start <= hour < settings.notify_hour_end)

    async def _notify_if_allowed(self, text: str):
        """Отправить уведомление только в разрешённое время."""
        if self.notify and not self._is_quiet_hours():
            await self.notify(text)

    async def _job_all_users(self):
        """Мультиюзер: прогнать автоотклик по каждому активному пользователю."""
        from sqlalchemy import select
        from app.database import async_session
        from app.models.user import User
        from app.workers.user_runner import run_user_cycle

        async with async_session() as session:
            user_ids = (await session.execute(
                select(User.id).where(User.is_active.is_(True), User.hh_connected.is_(True))
            )).scalars().all()
        for uid in user_ids:
            try:
                await run_user_cycle(uid)
            except Exception as e:
                log.error("user_cycle_failed", user_id=uid, error=str(e))

    async def _job_bump_users(self):
        """Мультиюзер: поднять резюме там, где включено «Поднятие» — учитываем
        per-task настройки (у каждой задачи своё резюме) + старый глобальный флаг."""
        from sqlalchemy import select
        from app.database import async_session
        from app.models.user import User
        from app.models.search_task import SearchTask
        from app.parsers.hh_user_client import HHUserClient
        from datetime import datetime as _dt, timezone as _tz

        plan = []  # (uid, at, rt, exp, {resume_id, ...})
        async with async_session() as session:
            users = (await session.execute(
                select(User).where(User.is_active.is_(True), User.hh_connected.is_(True))
            )).scalars().all()
            for u in users:
                if not u.hh_access_token:
                    continue
                resume_ids = set()
                if u.get_settings().resume_bump and u.hh_resume_id:
                    resume_ids.add(u.hh_resume_id)
                tasks = (await session.execute(
                    select(SearchTask).where(SearchTask.user_id == u.id, SearchTask.is_active.is_(True))
                )).scalars().all()
                for t in tasks:
                    s = t.get_settings() if t.settings_json else u.get_settings()
                    if getattr(s, "resume_bump", False):
                        rid = t.resume_id or u.hh_resume_id
                        if rid:
                            resume_ids.add(rid)
                if resume_ids:
                    plan.append((u.id, u.hh_access_token, u.hh_refresh_token or "",
                                 u.hh_token_expires.timestamp() if u.hh_token_expires else 0.0,
                                 resume_ids))
        for uid, at, rt, exp, resume_ids in plan:
            for rid in resume_ids:
                try:
                    client = HHUserClient(at, rt, rid, exp)
                    ok = await client.bump_resume()
                    if client.new_token:
                        at, rt = client.new_token["access_token"], client.new_token["refresh_token"]
                        exp = client.new_token["expires_at"]
                        async with async_session() as session:
                            u = await session.get(User, uid)
                            if u:
                                u.hh_access_token = at
                                u.hh_refresh_token = rt
                                u.hh_token_expires = _dt.fromtimestamp(exp, tz=_tz.utc)
                                await session.commit()
                    log.info("resume_bumped", user_id=uid, resume=rid, ok=ok)
                except Exception as e:
                    log.warning("resume_bump_failed", user_id=uid, error=str(e))

    async def _job_daily_digest(self):
        """Вечерний дайджест (20:00 МСК): отправлено / приглашения / просмотры за день."""
        from sqlalchemy import select, func
        from app.database import async_session
        from app.models.user import User
        from app.models.search_task import SearchTask
        from app.models.application import Application, ApplicationStatus
        if not self.bot:
            return
        async with async_session() as session:
            users = (await session.execute(
                select(User).where(User.is_active.is_(True), User.hh_connected.is_(True))
            )).scalars().all()
            for u in users:
                tasks = (await session.execute(
                    select(SearchTask).where(SearchTask.user_id == u.id).order_by(SearchTask.id)
                )).scalars().all()
                if not tasks:
                    continue
                sent_sum = inv_sum = views_sum = 0
                task_lines = []
                for t in tasks:
                    sent_today = (await session.execute(
                        select(func.count(Application.id)).where(
                            Application.search_task_id == t.id,
                            Application.status == ApplicationStatus.SENT,
                            func.date(Application.created_at) == func.current_date())
                    )).scalar() or 0
                    inv_t, vt = (t.invites_today or 0), (t.views_today or 0)
                    sent_sum += sent_today
                    inv_sum += inv_t
                    views_sum += vt
                    task_lines.append(f"• {t.keyword[:30]} — {sent_today} откл., {inv_t} пригл.")
                if sent_sum == 0 and inv_sum == 0 and views_sum == 0:
                    continue  # активности за день нет — не беспокоим
                text = (
                    "🌙 <b>Итоги дня</b>\n\n"
                    f"📤 Отправлено откликов: <b>{sent_sum}</b>\n"
                    f"📨 Приглашений: <b>{inv_sum}</b>\n"
                    f"👀 Просмотров резюме: <b>{views_sum}</b>\n\n"
                    "<b>По задачам:</b>\n" + "\n".join(task_lines)
                )
                try:
                    await self.bot.send_message(u.telegram_id, text, parse_mode="HTML")
                except Exception as e:
                    log.warning("digest_send_failed", user_id=u.id, error=str(e))

    async def _job_connect_reminders(self):
        """Ре-энгейджмент: тем, кто зарегался, но не подключил hh — напомнить.
        До 2 напоминаний: через ~3ч и через ~24ч после регистрации."""
        from sqlalchemy import select
        from datetime import datetime as _dt, timezone as _tz
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from app.database import async_session
        from app.models.user import User
        if not self.bot:
            return
        now = _dt.now(_tz.utc)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔗 Подключить hh.ru за 1 минуту", callback_data="connect:start")]])
        async with async_session() as session:
            users = (await session.execute(
                select(User).where(User.hh_connected.is_(False), User.connect_reminders < 2)
            )).scalars().all()
            for u in users:
                created = u.created_at
                if created is None:
                    continue
                if created.tzinfo is None:  # SQLite отдаёт naive — трактуем как UTC
                    created = created.replace(tzinfo=_tz.utc)
                age_h = (now - created).total_seconds() / 3600
                send = False
                if u.connect_reminders == 0 and age_h >= 3:
                    text = ("👋 Остался <b>один шаг</b>!\n\n"
                            "Ты зашёл в бот автооткликов, но не подключил hh.ru — без этого "
                            "бот не может откликаться за тебя. Это займёт минуту, пароль не нужен. Жми 👇")
                    send = True
                elif u.connect_reminders == 1 and age_h >= 24:
                    text = ("🔔 Напоминаем: подключи hh.ru — и бот начнёт откликаться на вакансии "
                            "сам, 24/7. Минута, без пароля. Жми 👇")
                    send = True
                if send:
                    try:
                        await self.bot.send_message(u.telegram_id, text, reply_markup=kb, parse_mode="HTML")
                        u.connect_reminders += 1
                    except Exception as e:
                        log.warning("connect_reminder_failed", user_id=u.id, error=str(e))
                        u.connect_reminders += 1  # не долбить недоступных
            await session.commit()

    def _start_multi(self, interval: int):
        """Планировщик мультиюзерного режима: цикл откликов + поднятие резюме."""
        from datetime import datetime, timedelta
        self.scheduler.add_job(
            self._job_all_users,
            "interval",
            seconds=interval,
            id="multi_apply",
            name="Автоотклик (мультиюзер)",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            next_run_time=datetime.now(MSK) + timedelta(seconds=30),
        )
        self.scheduler.add_job(
            self._job_bump_users,
            "interval",
            hours=4,
            id="multi_bump",
            name="Поднятие резюме (мультиюзер)",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
            next_run_time=datetime.now(MSK) + timedelta(minutes=3),
        )
        # Ре-энгейджмент неподключённых — каждые 2 часа.
        from datetime import datetime as _dt2, timedelta as _td2
        self.scheduler.add_job(
            self._job_connect_reminders,
            "interval",
            hours=2,
            id="connect_reminders",
            name="Напоминания подключить hh",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
            next_run_time=_dt2.now(MSK) + _td2(minutes=5),
        )
        # Вечерний дайджест — каждый день в 20:00 МСК.
        self.scheduler.add_job(
            self._job_daily_digest,
            "cron",
            hour=20,
            minute=0,
            id="daily_digest",
            name="Дайджест дня",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        self.scheduler.start()
        log.info("scheduler_started_multi", interval=interval)

    def start(self):
        interval = settings.check_interval_sec

        if settings.mode == "multi":
            self._start_multi(interval)
            return

        self.scheduler.add_job(
            self._job_search,
            "interval",
            seconds=interval,
            id="vacancy_search",
            name="Поиск вакансий",
        )
        self.scheduler.add_job(
            self._job_analyze,
            "interval",
            seconds=interval + 60,
            id="vacancy_analysis",
            name="AI-анализ вакансий",
        )
        self.scheduler.add_job(
            self._job_messages,
            "interval",
            seconds=interval,
            id="message_check",
            name="Проверка сообщений",
        )
        from datetime import datetime, timedelta
        self.scheduler.add_job(
            self._job_apply,
            "interval",
            # Ускорено с interval*2 (10 мин) до interval (5 мин). С max_instances=1
            # и coalesce=True если предыдущий тик не успел — следующий просто
            # пропустится; если успел — сразу запускается следующий пакет.
            seconds=interval,
            id="auto_apply",
            name="Авто-отклики",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            # First run 60s after start instead of after interval
            next_run_time=datetime.now(MSK) + timedelta(seconds=60),
        )
        self.scheduler.add_job(
            self._job_bump_resume,
            "interval",
            hours=4,
            id="bump_resume",
            name="Поднятие резюме",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            # Fire 5 min after start so frequent restarts don't keep
            # pushing the first bump 4 hours into the future
            next_run_time=datetime.now(MSK) + timedelta(minutes=5),
        )
        # Note: rejection thanks disabled — hh.ru blocks writing in chats
        # after the employer rejects the response.

        self.scheduler.add_job(
            self._job_check_login_health,
            "interval",
            minutes=30,
            id="login_health",
            name="Проверка сессий",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            next_run_time=datetime.now(MSK) + timedelta(minutes=10),
        )

        # Сводка по откликам — каждые 2 часа с 10:00 до 22:00 МСК.
        # _job_apply сам уведомления больше не шлёт; вся статистика сюда.
        self.scheduler.add_job(
            self._job_apply_summary,
            trigger="cron",
            hour="10,12,14,16,18,20,22",
            minute=0,
            id="apply_summary",
            name="Сводка по откликам",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=900,
        )

        self.scheduler.start()
        log.info("scheduler_started", interval=interval)

    async def _job_search(self):
        if self.is_paused:
            return
        try:
            await run_vacancy_search()
        except Exception as e:
            log.error("job_search_error", error=str(e))

    async def _job_analyze(self):
        if self.is_paused:
            return
        try:
            await run_vacancy_analysis()
        except Exception as e:
            log.error("job_analyze_error", error=str(e))

    async def _job_messages(self):
        if self.is_paused:
            return
        try:
            new_msgs = await check_all_messages()
            if not new_msgs:
                return

            platform_label = {"hh": "hh.ru", "habr": "Хабр Карьера", "avito": "Авито"}
            # Отказы — игнорируем (юзер просил тишину по ним)
            REJECT_PAT = ("отказ", "не подош", "отклонил", "решил остановить")
            # Шум: статусы, означающие "это просто наш свежий отклик" или нейтральный
            # промежуточный статус без реального действия от работодателя.
            # Без этого фильтра каждый наш авто-отклик через 5 мин превращается
            # в фейковое "новое сообщение" (hh создал чат — мы его видим как new thread).
            NOISE_PAT = (
                "резюме отправлено", "отклик отправлен", "отклик доставлен",
                "не просмотрено", "просмотрено", "новый",
                "ожидании", "ожидает", "думают", "думает",
            )
            # Действительно стоит уведомлять: реальные действия рекрутёра
            NOTIFY_STATUS_PAT = (
                "пригласил", "приглаш", "интервью", "собеседован",
                "оффер", "офер",
                "звонок", "позвон", "созвон",
                "тестовое", "задание",
                "получен ответ", "ответ работодателя",
                "написал", "сообщение от", "новое сообщение",
            )

            # Импорты для DB-чека на эхо нашего отклика
            from sqlalchemy import select
            from app.database import async_session
            from app.models.application import Application, ApplicationStatus
            from app.models.vacancy import Vacancy

            # created_at в БД — наивное UTC (SQLite). Сравнивать с MSK-временем
            # нельзя: окно "уезжает" на 3 часа и эхо-проверка не срабатывает.
            # Берём UTC-naive.
            echo_cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=45)
            ).replace(tzinfo=None)

            for msg in new_msgs:
                status_lc = (msg.get("status", "") or "").lower().strip()
                text_lc = (msg.get("text", "") or "").lower()
                combined = status_lc + " " + text_lc

                # 1. Явные отказы — мимо
                if any(k in combined for k in REJECT_PAT):
                    log.info(
                        "msg_skip_rejection",
                        platform=msg.get("platform"),
                        title=msg.get("title", "")[:50],
                    )
                    continue

                # 2. Эхо нашего собственного отклика? Проверяем БД: есть ли у нас
                # SENT-application на эту вакансию за последние 45 минут.
                # Если есть, значит это hh.ru только что создал thread в ответ на
                # наш авто-отклик — не реальное сообщение от работодателя.
                title_key = (msg.get("title", "") or "")[:50]
                is_echo = False
                if title_key:
                    try:
                        async with async_session() as session:
                            recent_apply = await session.scalar(
                                select(Application.id)
                                .join(Vacancy, Vacancy.id == Application.vacancy_id)
                                .where(
                                    Application.status == ApplicationStatus.SENT,
                                    Application.created_at >= echo_cutoff,
                                    Vacancy.title.ilike(f"%{title_key}%"),
                                )
                                .limit(1)
                            )
                            is_echo = bool(recent_apply)
                    except Exception as e:
                        log.warning("msg_echo_check_error", error=str(e)[:120])

                # Если в статусе есть позитивный сигнал (приглашение, оффер и т.п.)
                # — пропускаем эхо-логику: даже если мы недавно откликались,
                # реальная активность работодателя важнее.
                is_signal = any(k in combined for k in NOTIFY_STATUS_PAT)
                if is_echo and not is_signal:
                    log.info(
                        "msg_skip_apply_echo",
                        title=title_key,
                        company=(msg.get("company", "") or "")[:40],
                        status=msg.get("status", "")[:40],
                    )
                    continue

                # 3. Шумовые статусы без позитивного сигнала — тоже мимо
                is_noise = not status_lc or any(k in status_lc for k in NOISE_PAT)
                if is_noise and not is_signal:
                    log.info(
                        "msg_skip_noise",
                        platform=msg.get("platform"),
                        status=msg.get("status", "")[:40],
                        title=title_key,
                    )
                    continue

                plat = msg.get("platform", "")
                label = platform_label.get(plat, plat or "—")
                title = msg.get("title") or ""
                title_line = f"\n📋 {title[:100]}" if title else ""
                text = (
                    f"📩 <b>Новое сообщение — {label}</b>\n\n"
                    f"👤 {msg.get('sender') or 'Неизвестно'}\n"
                    f"🏢 {msg.get('company') or '—'}"
                    f"{title_line}\n\n"
                    f"{msg.get('text', '')[:600]}"
                )
                await self._notify_if_allowed(text)
        except Exception as e:
            log.error("job_messages_error", error=str(e))

    async def _job_apply(self):
        if self.is_paused or not self.auto_apply:
            return
        try:
            # Уведомления больше не отсылаем поминутно — статистика идёт
            # пакетом через _job_apply_summary каждые 2 часа.
            await run_auto_apply(auto_mode=True, min_score=self.min_ai_score)
        except Exception as e:
            log.error("job_apply_error", error=str(e))

    async def _job_bump_resume(self):
        if self.is_paused:
            return
        try:
            from app.parsers.hh_playwright import hh_playwright
            if not hh_playwright:
                return
            count = await hh_playwright.bump_resumes()
            if count > 0:
                await self._notify_if_allowed(f"⬆️ Поднято резюме: {count}")
        except Exception as e:
            log.error("job_bump_resume_error", error=str(e))

    async def _job_check_login_health(self):
        """Per-platform проверка сессий. Слетевшая платформа добавляется в
        paused_platforms (другие платформы продолжают работать). Уведомление
        в TG — с cooldown 60 минут, чтобы не спамить.

        hh — через httpx (быстро, без Playwright). Habr — через Playwright.
        """
        try:
            import httpx as _httpx
            from app.parsers.hh_oauth import hh_oauth
            from app.parsers.habr_playwright import habr_playwright

            PLATFORM_LABEL = {"hh": "hh.ru", "habr": "Хабр Карьера"}
            ALERT_COOLDOWN = timedelta(hours=12)
            now = datetime.now(MSK)

            # Собираем актуальный статус по платформам
            status: dict[str, bool] = {}

            # 1. HH — проверяем OAuth-токен (именно через него идут отклики),
            # а НЕ куки: куки протухают раньше токена и давали ложную паузу HH,
            # хотя OAuth-отклики работали. get_token() сам рефрешит токен.
            try:
                _tok = await hh_oauth.get_token()
                if _tok:
                    async with _httpx.AsyncClient(timeout=15) as _c:
                        _r = await _c.get(
                            "https://api.hh.ru/me",
                            headers={
                                "Authorization": f"Bearer {_tok}",
                                "User-Agent": "ru.hh.android/8.116",
                            },
                        )
                    status["hh"] = _r.status_code == 200
                else:
                    status["hh"] = False
            except Exception as e:
                log.warning("login_health_hh_check_error", error=str(e))
                status["hh"] = False

            # 2. Habr (Playwright) — проверяем только если cap>0
            if settings.max_applies_per_day_habr > 0 and habr_playwright:
                habr_ok = False
                try:
                    habr_ok = await habr_playwright.is_logged_in()
                    if not habr_ok:
                        # Сессия слетела — пробуем перелогиниться (как при отклике).
                        # Алертим только если и повторный логин не удался,
                        # иначе на каждый сброс cookie летел ложный «слетела».
                        habr_ok = await habr_playwright.login()
                except Exception as e:
                    log.warning("login_health_habr_check_error", error=str(e))
                    habr_ok = False
                status["habr"] = habr_ok

            newly_broken: list[str] = []   # сессии, которые СЕЙЧАС слетели (для уведомления)
            recovered: list[str] = []      # сессии, которые ожили (auto-unpause)
            state_changed = False

            for plat, ok in status.items():
                was_paused = plat in self.paused_platforms
                if not ok and not was_paused:
                    self.paused_platforms.add(plat)
                    newly_broken.append(plat)
                    state_changed = True
                    log.warning(f"login_health_{plat}_expired", paused=True)
                elif ok and was_paused:
                    self.paused_platforms.discard(plat)
                    recovered.append(plat)
                    state_changed = True
                    log.info(f"login_health_{plat}_recovered")

            # Если есть платформы на паузе — шлём напоминание раз в час
            persistent_dead = [p for p in self.paused_platforms if p in status and not status[p]]
            need_remind: list[str] = []
            for plat in persistent_dead:
                last_alert_iso = self._last_login_alert.get(plat)
                if not last_alert_iso:
                    need_remind.append(plat)
                else:
                    try:
                        last_dt = datetime.fromisoformat(last_alert_iso)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=MSK)
                        if now - last_dt >= ALERT_COOLDOWN:
                            need_remind.append(plat)
                    except Exception:
                        need_remind.append(plat)

            # Шлём уведомление по событию newly_broken ИЛИ по cooldown
            to_notify = set(newly_broken) | set(need_remind)
            if to_notify:
                lines = ["⚠️ <b>Нужен перелогин</b>", ""]
                for p in sorted(to_notify):
                    label = PLATFORM_LABEL.get(p, p)
                    lines.append(f"• <b>{label}</b> — сессия слетела")
                # Какие платформы продолжают работать
                still_active = [PLATFORM_LABEL.get(p, p) for p, ok in status.items() if ok]
                if still_active:
                    lines.append("")
                    lines.append("✅ Продолжают работать: " + ", ".join(still_active))
                lines.append("")
                lines.append("Перелогинься через VNC, бот сам подхватит сессию.")
                msg = "\n".join(lines)
                # Тихие часы (МСК 22–9): ночью не шлём и НЕ обновляем таймстамп,
                # чтобы утром пришло один раз, а не копилось всю ночь.
                if self.notify and not self._is_quiet_hours():
                    try:
                        await self.notify(msg)
                        for p in to_notify:
                            self._last_login_alert[p] = now.isoformat()
                        state_changed = True
                    except Exception as e:
                        log.error("login_health_notify_error", error=str(e))

            # Уведомление о восстановлении
            if recovered and self.notify:
                try:
                    msg = "✅ <b>Сессия восстановлена:</b> " + ", ".join(
                        PLATFORM_LABEL.get(p, p) for p in recovered
                    )
                    await self.notify(msg)
                    for p in recovered:
                        self._last_login_alert.pop(p, None)
                    state_changed = True
                except Exception as e:
                    log.error("login_health_notify_error", error=str(e))

            if state_changed:
                self._save_state()

            if not self.paused_platforms:
                log.info("login_health_ok")
            else:
                log.info("login_health_paused", paused=sorted(self.paused_platforms))
        except Exception as e:
            log.error("job_login_health_error", error=str(e))

    async def _job_apply_summary(self):
        """Сводка по откликам за окно (с момента прошлой сводки или с начала суток).

        Запускается по cron в 10/12/14/16/18/20/22 МСК. Шлёт в TG только если
        за окно были SENT или FAILED — иначе тишина (тоже сохраняем timestamp,
        чтобы следующее окно считалось от этого момента).
        """
        try:
            from sqlalchemy import select, func
            from app.database import async_session
            from app.models.application import Application, ApplicationStatus
            from app.models.vacancy import Vacancy

            now = datetime.now(MSK)
            if self._last_apply_summary_at:
                window_start = self._last_apply_summary_at
            else:
                window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            # created_at в БД хранится как наивное UTC (SQLite игнорирует tz).
            # window_start — MSK-aware. Без перевода в UTC окно "уезжает" на 3
            # часа вперёд и запрос всегда возвращает 0 (отсюда "откликов не было"
            # при растущем дневном счётчике). Сравниваем в UTC-naive.
            window_start_db = window_start.astimezone(timezone.utc).replace(tzinfo=None)

            async with async_session() as session:
                rows = (await session.execute(
                    select(Application.platform, Application.status, func.count(Application.id))
                    .where(Application.created_at >= window_start_db)
                    .group_by(Application.platform, Application.status)
                )).all()

                sent_by_plat: dict[str, int] = {}
                failed_by_plat: dict[str, int] = {}
                for plat, status, cnt in rows:
                    if status == ApplicationStatus.SENT:
                        sent_by_plat[plat] = cnt
                    elif status == ApplicationStatus.FAILED:
                        failed_by_plat[plat] = cnt

                total_sent = sum(sent_by_plat.values())
                total_failed = sum(failed_by_plat.values())

                if total_sent == 0 and total_failed == 0:
                    # Heartbeat: пользователь хочет сводку каждые 2 часа даже
                    # при нуле — чтобы видеть, что бот жив. Шлём короткое.
                    today_sent_e = await session.scalar(
                        select(func.count(Application.id))
                        .where(
                            Application.status == ApplicationStatus.SENT,
                            func.date(Application.created_at) == func.current_date(),
                        )
                    )
                    text = (
                        f"📊 <b>Сводка откликов {window_start.strftime('%H:%M')}–{now.strftime('%H:%M')}</b>\n\n"
                        f"За окно откликов не было.\n"
                        f"🗓 За сегодня всего: <b>{today_sent_e or 0}</b>"
                    )
                    if self.notify:
                        try:
                            await self.notify(text)
                        except Exception as e:
                            log.error("apply_summary_notify_error", error=str(e))
                    self._last_apply_summary_at = now
                    self._save_state()
                    log.info("apply_summary_empty_sent", window_start=window_start.isoformat())
                    return

                # Топ-5 успешных откликов за окно (по ai_score)
                top_rows = (await session.execute(
                    select(Vacancy.title, Vacancy.ai_score, Application.platform)
                    .join(Vacancy, Application.vacancy_id == Vacancy.id)
                    .where(
                        Application.created_at >= window_start_db,
                        Application.status == ApplicationStatus.SENT,
                    )
                    .order_by(Vacancy.ai_score.desc().nullslast())
                    .limit(5)
                )).all()

                # Сводка по реальным ошибкам (не "limit"/"auth") — для диагностики
                err_rows = (await session.execute(
                    select(Application.error_message, func.count(Application.id))
                    .where(
                        Application.created_at >= window_start_db,
                        Application.status == ApplicationStatus.FAILED,
                        Application.error_message.isnot(None),
                    )
                    .group_by(Application.error_message)
                    .order_by(func.count(Application.id).desc())
                    .limit(3)
                )).all()

                # Сегодняшний общий счётчик (день в таймзоне сервера БД)
                today_sent = await session.scalar(
                    select(func.count(Application.id))
                    .where(
                        Application.status == ApplicationStatus.SENT,
                        func.date(Application.created_at) == func.current_date(),
                    )
                )

            plat_label = {"hh": "hh.ru", "habr": "Хабр Карьера", "avito": "Авито"}
            lines = [
                f"📊 <b>Сводка откликов {window_start.strftime('%H:%M')}–{now.strftime('%H:%M')}</b>",
                "",
            ]
            if total_sent:
                lines.append(f"✅ Отправлено: <b>{total_sent}</b>")
                for plat, cnt in sorted(sent_by_plat.items(), key=lambda kv: -kv[1]):
                    lines.append(f"  • {plat_label.get(plat, plat)}: {cnt}")
            if total_failed:
                lines.append(f"⚠️ Ошибок: <b>{total_failed}</b>")
                for plat, cnt in sorted(failed_by_plat.items(), key=lambda kv: -kv[1]):
                    lines.append(f"  • {plat_label.get(plat, plat)}: {cnt}")
                if err_rows:
                    lines.append("  Топ причин:")
                    for msg, cnt in err_rows:
                        m = (msg or "")[:70]
                        lines.append(f"    – {m} ×{cnt}")

            if top_rows:
                lines.append("")
                lines.append("🎯 <b>Лучшие отклики:</b>")
                for title, score, plat in top_rows:
                    score_str = f" {int(score)}%" if score is not None else ""
                    lines.append(f"  • [{plat_label.get(plat, plat)}]{score_str} {title[:60]}")

            if today_sent is not None:
                lines.append("")
                lines.append(f"🗓 За сегодня всего: <b>{today_sent}</b>")

            text = "\n".join(lines)

            # Шлём напрямую, минуя _is_quiet_hours — это пользовательски запрошенный
            # пакетный отчёт по расписанию, тихие часы тут не уместны.
            if self.notify:
                try:
                    await self.notify(text)
                except Exception as e:
                    log.error("apply_summary_notify_error", error=str(e))

            self._last_apply_summary_at = now
            self._save_state()
            log.info(
                "apply_summary_sent",
                sent=total_sent,
                failed=total_failed,
                window_start=window_start.isoformat(),
            )
        except Exception as e:
            log.error("job_apply_summary_error", error=str(e))

    async def _job_thank_rejections(self):
        """Send thanks message to recent rejected negotiations.
        Limited rate to avoid hh.ru ban: max 3 per run, with delays."""
        if self.is_paused:
            return
        try:
            from app.parsers.hh_playwright import hh_playwright
            from app.workers.message_worker import process_rejection_thanks
            if not hh_playwright:
                return
            count = await process_rejection_thanks(max_count=3)
            if count > 0:
                await self._notify_if_allowed(f"💬 Отправлено благодарностей: {count}")
        except Exception as e:
            log.error("job_thank_rejections_error", error=str(e))

    def pause(self):
        self.is_paused = True
        self._save_state()
        log.info("scheduler_paused")

    def resume(self):
        self.is_paused = False
        self._save_state()
        log.info("scheduler_resumed")

    def set_auto_apply(self, enabled: bool):
        self.auto_apply = enabled
        self._save_state()
        log.info("auto_apply_set", enabled=enabled)

    def stop(self):
        self.scheduler.shutdown()
        log.info("scheduler_stopped")
