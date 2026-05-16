import structlog
from sqlalchemy import select

from app.database import async_session
from app.models.message import RecruiterMessage
from app.models.vacancy import Vacancy
from app.parsers.hh import HHParser
from app.parsers.workspace import WorkspaceParser
from app.parsers.geekjob import GeekJobParser
from app.ai.claude import claude_ai
from app.utils.anti_detect import random_delay

log = structlog.get_logger()

PARSERS = {
    "hh": HHParser(),
    "workspace": WorkspaceParser(),
    "geekjob": GeekJobParser(),
}


async def check_all_messages() -> list[dict]:
    log.info("message_check_started")
    all_new = []

    for platform, parser in PARSERS.items():
        try:
            logged_in = await parser.login()
            if not logged_in:
                continue

            messages = await parser.check_messages()
            for msg in messages:
                saved = await _save_message(msg)
                if saved:
                    all_new.append(saved)
            await random_delay(3, 8)

        except Exception as e:
            log.error("message_check_error", platform=platform, error=str(e))

    log.info("message_check_complete", new_count=len(all_new))
    return all_new


async def _save_message(msg: dict) -> dict | None:
    async with async_session() as session:
        # Проверяем дубликат по thread_id
        if msg.get("thread_id"):
            existing = await session.scalar(
                select(RecruiterMessage.id).where(
                    RecruiterMessage.external_thread_id == msg["thread_id"]
                )
            )
            if existing:
                return None

        # Пробуем привязать к вакансии
        vacancy_id = None
        if msg.get("title"):
            vacancy = await session.scalar(
                select(Vacancy).where(Vacancy.title.ilike(f"%{msg['title'][:50]}%"))
            )
            if vacancy:
                vacancy_id = vacancy.id

        record = RecruiterMessage(
            vacancy_id=vacancy_id,
            platform=msg.get("platform", ""),
            sender_name=msg.get("sender", msg.get("company", "")),
            sender_company=msg.get("company", ""),
            text=msg.get("text", msg.get("status", "")),
            external_thread_id=msg.get("thread_id"),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)

        return {
            "id": record.id,
            "platform": record.platform,
            "sender": record.sender_name,
            "company": record.sender_company,
            "text": record.text,
            "vacancy_id": vacancy_id,
        }


async def process_rejection_thanks(max_count: int = 3) -> int:
    """Find recent rejections without a thanks reply sent, send 'thanks for feedback'.

    Conservative limits to avoid hh.ru anti-spam:
    - max_count messages per run (default 3)
    - 60-120 sec random delay between sends
    """
    from app.parsers.hh_playwright import hh_playwright
    if not hh_playwright:
        return 0

    statuses = await hh_playwright.check_negotiations_status()
    rejections = [s for s in statuses if s.get("tab") == "discard"]

    sent_count = 0
    async with async_session() as session:
        for rej in rejections[:max_count * 2]:  # check 2x in case some already done
            if sent_count >= max_count:
                break
            thread_id = rej.get("thread_id")
            if not thread_id:
                continue

            # Skip if already thanked (we mark via is_read=True + sender_name="__thanks_sent__")
            already = await session.scalar(
                select(RecruiterMessage.id).where(
                    RecruiterMessage.external_thread_id == thread_id,
                    RecruiterMessage.sender_name == "__thanks_sent__",
                )
            )
            if already:
                continue

            # Build negotiation URL from thread_id (hh_<id> -> /negotiations/<id>)
            tid = thread_id.replace("hh_", "")
            url = f"https://hh.ru/applicant/negotiations/item?topicId={tid}"

            success = await hh_playwright.send_rejection_thanks(url)
            if success:
                session.add(RecruiterMessage(
                    platform="hh",
                    sender_name="__thanks_sent__",
                    sender_company=rej.get("company", ""),
                    text="thanks message sent",
                    external_thread_id=thread_id,
                    is_read=True,
                ))
                await session.commit()
                sent_count += 1
                await random_delay(60, 120)

    log.info("rejection_thanks_complete", sent=sent_count)
    return sent_count


async def generate_ai_reply(message_id: int) -> str | None:
    async with async_session() as session:
        msg = await session.get(RecruiterMessage, message_id)
        if not msg:
            return None

        vacancy_context = ""
        if msg.vacancy_id:
            vacancy = await session.get(Vacancy, msg.vacancy_id)
            if vacancy:
                vacancy_context = f"{vacancy.title}\n{vacancy.description or ''}"

    reply, _, _ = await claude_ai.generate_reply(msg.text, vacancy_context)

    async with async_session() as session:
        msg = await session.get(RecruiterMessage, message_id)
        msg.ai_suggested_reply = reply
        await session.commit()

    return reply
