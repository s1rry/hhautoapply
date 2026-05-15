import re

import structlog
try:
    from playwright.async_api import Page
except ImportError:
    Page = None

from app.config import settings
from app.parsers.base import BaseParser, ParsedVacancy
from app.utils.anti_detect import random_delay, human_scroll

log = structlog.get_logger()

GEEKJOB_BASE = "https://geekjob.ru"


class GeekJobParser(BaseParser):
    platform = "geekjob"

    async def login(self) -> bool:
        if not settings.geekjob_login:
            log.warning("geekjob_login_skip", reason="credentials not set")
            return False

        page = await self._get_page()
        try:
            await page.goto(f"{GEEKJOB_BASE}/login", wait_until="domcontentloaded")
            await random_delay(2, 4)

            # Проверяем авторизацию
            if await page.locator(".user-menu, .profile-link").count() > 0:
                log.info("geekjob_already_logged_in")
                await page.close()
                return True

            email_input = page.locator('input[name="email"], input[type="email"]')
            pwd_input = page.locator('input[name="password"], input[type="password"]')

            if await email_input.count() > 0 and await pwd_input.count() > 0:
                await email_input.fill(settings.geekjob_login)
                await random_delay(0.5, 1)
                await pwd_input.fill(settings.geekjob_password)
                await random_delay(0.5, 1)

                submit = page.locator('button[type="submit"], input[type="submit"]')
                if await submit.count() > 0:
                    await submit.first.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await random_delay(2, 3)

                if await page.locator(".user-menu, .profile-link").count() > 0:
                    log.info("geekjob_login_success")
                    await self._save_session()
                    await page.close()
                    return True

            log.error("geekjob_login_failed")
            await page.close()
            return False

        except Exception as e:
            log.error("geekjob_login_error", error=str(e))
            await page.close()
            return False

    async def search_vacancies(self, query: str, **filters) -> list[ParsedVacancy]:
        page = await self._get_page()
        vacancies = []

        try:
            url = f"{GEEKJOB_BASE}/vacancies?q={query}"
            if filters.get("remote", True):
                url += "&remote=1"

            await page.goto(url, wait_until="domcontentloaded")
            await random_delay(2, 4)
            await human_scroll(page, 400)

            cards = page.locator(".vacancy-card, .vacancy-item, article.vacancy")
            count = await cards.count()
            log.info("geekjob_search_results", query=query, count=count)

            for i in range(min(count, 50)):
                card = cards.nth(i)
                try:
                    vacancy = await self._parse_card(card)
                    if vacancy:
                        vacancies.append(vacancy)
                except Exception as e:
                    log.warning("geekjob_card_parse_error", index=i, error=str(e))

            await page.close()

        except Exception as e:
            log.error("geekjob_search_error", error=str(e))
            await page.close()

        return vacancies

    async def _parse_card(self, card) -> ParsedVacancy | None:
        title_el = card.locator("h2 a, h3 a, .vacancy-title a, .title a").first
        if await title_el.count() == 0:
            return None

        title = await title_el.inner_text()
        href = await title_el.get_attribute("href") or ""
        url = href if href.startswith("http") else f"{GEEKJOB_BASE}{href}"

        ext_id = href.split("/")[-1] if href else ""

        company_el = card.locator(".company-name, .employer, .vacancy-company").first
        company_name = await company_el.inner_text() if await company_el.count() > 0 else ""

        salary_el = card.locator(".salary, .vacancy-salary").first
        salary_text = await salary_el.inner_text() if await salary_el.count() > 0 else ""
        salary_from, salary_to, currency = self._parse_salary(salary_text)

        location_el = card.locator(".location, .city, .vacancy-location").first
        location = await location_el.inner_text() if await location_el.count() > 0 else ""

        tags_els = card.locator(".tag, .skill, .badge")
        skills = []
        for j in range(await tags_els.count()):
            skills.append(await tags_els.nth(j).inner_text())

        is_remote = "удалён" in location.lower() or "remote" in location.lower() or any(
            "удалён" in s.lower() for s in skills
        )

        return ParsedVacancy(
            platform="geekjob",
            external_id=ext_id,
            url=url,
            title=title.strip(),
            company_name=company_name.strip(),
            salary_from=salary_from,
            salary_to=salary_to,
            salary_currency=currency,
            location=location.strip(),
            is_remote=is_remote,
            skills=skills,
        )

    def _parse_salary(self, text: str) -> tuple[int | None, int | None, str]:
        if not text:
            return None, None, ""
        text = text.replace(" ", "").replace("\xa0", "")
        currency = "RUB" if "₽" in text or "руб" in text else ("USD" if "$" in text else "")
        numbers = [int(x) for x in re.findall(r"\d+", text)]
        if len(numbers) >= 2:
            return numbers[0], numbers[1], currency
        elif numbers:
            return numbers[0], None, currency
        return None, None, currency

    async def get_vacancy_details(self, url: str) -> ParsedVacancy | None:
        page = await self._get_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await random_delay(2, 4)

            title_el = page.locator("h1").first
            title = await title_el.inner_text() if await title_el.count() > 0 else ""

            desc_el = page.locator(".vacancy-description, .description, .content").first
            description = await desc_el.inner_text() if await desc_el.count() > 0 else ""

            skills = []
            skill_els = page.locator(".tag, .skill, .badge, .tech-stack span")
            for i in range(await skill_els.count()):
                skills.append(await skill_els.nth(i).inner_text())

            await page.close()
            return ParsedVacancy(
                platform="geekjob",
                external_id=url.split("/")[-1],
                url=url,
                title=title.strip(),
                description=description.strip(),
                skills=skills,
            )

        except Exception as e:
            log.error("geekjob_details_error", url=url, error=str(e))
            await page.close()
            return None

    async def check_messages(self) -> list[dict]:
        page = await self._get_page()
        messages = []
        try:
            await page.goto(f"{GEEKJOB_BASE}/messages", wait_until="domcontentloaded")
            await random_delay(2, 3)

            items = page.locator(".message-item, .dialog-item, .chat-item")
            for i in range(min(await items.count(), 20)):
                item = items.nth(i)
                unread = await item.locator(".unread, .new, .badge").count() > 0
                if unread:
                    sender = await item.locator(".sender, .name, .from").first.inner_text() if await item.locator(
                        ".sender, .name, .from").count() > 0 else ""
                    preview = await item.locator(".preview, .text, .last-message").first.inner_text() if await item.locator(
                        ".preview, .text, .last-message").count() > 0 else ""
                    messages.append({
                        "platform": "geekjob",
                        "sender": sender.strip(),
                        "text": preview.strip(),
                    })

            await page.close()
        except Exception as e:
            log.error("geekjob_messages_error", error=str(e))
            await page.close()

        return messages
