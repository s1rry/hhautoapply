import asyncio
from datetime import datetime

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

from app.config import settings
from app.workers.vacancy_worker import run_vacancy_search, run_vacancy_analysis
from app.workers.apply_worker import run_auto_apply
from app.workers.message_worker import check_all_messages

log = structlog.get_logger()

MSK = ZoneInfo("Europe/Moscow")


class WorkerScheduler:
    def __init__(self, notify_callback=None):
        self.scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        self.is_paused = False
        self.auto_apply = False
        self.min_ai_score = 70
        self.notify = notify_callback  # async fn(text) -> sends to TG

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
        self.scheduler.add_job(
            self._job_apply,
            "interval",
            seconds=interval * 2,
            id="auto_apply",
            name="Авто-отклики",
        )
        self.scheduler.add_job(
            self._job_bump_resume,
            "interval",
            hours=4,
            id="bump_resume",
            name="Поднятие резюме",
        )
        self.scheduler.add_job(
            self._job_thank_rejections,
            "interval",
            hours=6,
            id="thank_rejections",
            name="Активность в чатах",
        )

        self.scheduler.start()
        log.info("scheduler_started", interval=interval)

    async def _job_search(self):
        if self.is_paused:
            return
        try:
            new_count = await run_vacancy_search()
            if new_count > 0:
                await self._notify_if_allowed(f"🔍 Найдено {new_count} новых вакансий")
        except Exception as e:
            log.error("job_search_error", error=str(e))

    async def _job_analyze(self):
        if self.is_paused:
            return
        try:
            analyzed = await run_vacancy_analysis()
            if analyzed > 0:
                await self._notify_if_allowed(f"🤖 Проанализировано {analyzed} вакансий")
        except Exception as e:
            log.error("job_analyze_error", error=str(e))

    async def _job_messages(self):
        if self.is_paused:
            return
        try:
            new_msgs = await check_all_messages()
            if new_msgs:
                for msg in new_msgs:
                    text = (
                        f"📩 <b>Новое сообщение!</b>\n\n"
                        f"👤 {msg['sender'] or 'Неизвестно'}\n"
                        f"🏢 {msg['company'] or '—'}\n"
                        f"📧 {msg['platform']}\n\n"
                        f"{msg['text'][:500]}"
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
        log.info("scheduler_paused")

    def resume(self):
        self.is_paused = False
        log.info("scheduler_resumed")

    def stop(self):
        self.scheduler.shutdown()
        log.info("scheduler_stopped")
