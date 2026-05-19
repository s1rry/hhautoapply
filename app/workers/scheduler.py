import asyncio
import json
from datetime import datetime
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
    def __init__(self, notify_callback=None):
        self.scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        # Load persisted state
        state = self._load_state()
        self.is_paused = state.get("is_paused", False)
        self.auto_apply = state.get("auto_apply", False)
        self.min_ai_score = 70
        self.notify = notify_callback  # async fn(text) -> sends to TG

    def _load_state(self) -> dict:
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning("scheduler_state_load_error", error=str(e))
        return {}

    def _save_state(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({
                "is_paused": self.is_paused,
                "auto_apply": self.auto_apply,
            }))
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

    def start(self):
        interval = settings.check_interval_sec

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
            seconds=interval * 2,
            id="auto_apply",
            name="Авто-отклики",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            # First run 60s after start instead of after interval*2
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
            if new_msgs:
                platform_label = {"hh": "hh.ru", "habr": "Хабр Карьера", "avito": "Авито"}
                REJECT_PAT = ("отказ", "не подош", "отклонил", "решил остановить")
                for msg in new_msgs:
                    # Skip rejection notifications — too noisy
                    status_lc = (msg.get("status", "") or "").lower()
                    text_lc = (msg.get("text", "") or "").lower()
                    if any(k in status_lc or k in text_lc for k in REJECT_PAT):
                        log.info(
                            "msg_skip_rejection",
                            platform=msg.get("platform"),
                            title=msg.get("title", "")[:50],
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
            applied = await run_auto_apply(auto_mode=True, min_score=self.min_ai_score)
            if applied > 0:
                await self._notify_if_allowed(f"✅ Отправлено {applied} автоматических откликов")
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
        """Every 30 min verify hh + habr login. If any expired,
        notify in TG and pause auto_apply for that platform.

        hh checked via fast httpx hh_api_client (cookies only — не падает
        как Playwright). Habr через Playwright с retry на crash.
        """
        try:
            from app.parsers.hh_api import hh_api_client
            from app.parsers.habr_playwright import habr_playwright

            issues: list[str] = []

            # 1. HH — через API client (httpx, надёжнее)
            try:
                hh_ok = await hh_api_client.is_logged_in()
            except Exception as e:
                log.warning("login_health_hh_check_error", error=str(e))
                hh_ok = False
            if not hh_ok:
                issues.append("hh.ru")
                log.warning("login_health_hh_expired")

            # 2. Habr — Playwright с retry. Пропускаем если отключён (cap=0)
            if settings.max_applies_per_day_habr > 0 and habr_playwright:
                habr_ok = False
                for attempt in range(2):
                    try:
                        habr_ok = await habr_playwright.is_logged_in()
                        if habr_ok:
                            break
                    except Exception as e:
                        log.warning("login_health_habr_check_error", attempt=attempt + 1, error=str(e))
                        habr_ok = False
                if not habr_ok:
                    issues.append("Хабр Карьера")
                    log.warning("login_health_habr_expired")

            if issues:
                vnc_hint = (
                    "🔐 Нужен ручной перелогин через VNC:\n"
                    + "\n".join(
                        f"• <b>{p}</b> — попроси «запускай VNC для {('hh' if 'hh' in p else 'habr')}»"
                        for p in issues
                    )
                )
                msg = (
                    "⚠️ <b>Сессия слетела</b>\n\n"
                    + "Затронуто: " + ", ".join(f"<b>{p}</b>" for p in issues) + "\n\n"
                    + vnc_hint + "\n\n"
                    "Авто-отклики временно приостановлены."
                )
                if self.notify:
                    try:
                        await self.notify(msg)
                    except Exception as e:
                        log.error("login_health_notify_error", error=str(e))
                if self.auto_apply:
                    self.set_auto_apply(False)
                    log.info("auto_apply_disabled_session_expired", platforms=issues)
            else:
                log.info("login_health_ok")
        except Exception as e:
            log.error("job_login_health_error", error=str(e))

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
