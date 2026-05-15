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

WORKSPACE_BASE = "https://workspace.ru"


class WorkspaceParser(BaseParser):
    platform = "workspace"

    async def login(self) -> bool:
        if not settings.workspace_login:
            log.warning("workspace_login_skip", reason="credentials not set")
            return False

        page = await self._get_page()
        try:
            await page.goto(f"{WORKSPACE_BASE}/login", wait_until="domcontentloaded")
            await random_delay(2, 4)

            if await page.locator(".user-menu, .profile-link, .cabinet-link").count() > 0:
                log.info("workspace_already_logged_in")
                await page.close()
                return True

            email_input = page.locator('input[name="email"], input[name="login"], #email')
            pwd_input = page.locator('input[name="password"], #password')

            if await email_input.count() > 0 and await pwd_input.count() > 0:
                await email_input.first.fill(settings.workspace_login)
                await random_delay(0.5, 1)
                await pwd_input.first.fill(settings.workspace_password)
                await random_delay(0.5, 1)

                submit = page.locator('button[type="submit"], input[type="submit"], .login-btn')
                if await submit.count() > 0:
                    await submit.first.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await random_delay(2, 3)

                if await page.locator(".user-menu, .profile-link, .cabinet-link").count() > 0:
                    log.info("workspace_login_success")
                    await self._save_session()
                    await page.close()
                    return True

            log.error("workspace_login_failed")
            await page.close()
            return False

        except Exception as e:
            log.error("workspace_login_error", error=str(e))
            await page.close()
            return False

    async def search_vacancies(self, query: str, **filters) -> list[ParsedVacancy]:
        page = await self._get_page()
        vacancies = []

        try:
            url = f"{WORKSPACE_BASE}/vacancies?q={query}"
            if filters.get("remote", True):
                url += "&remote=1"
            if filters.get("city"):
                url += f"&city={filters['city']}"

            await page.goto(url, wait_until="domcontentloaded")
            await random_delay(2, 4)
            await human_scroll(page, 400)

            cards = page.locator(".vacancy-card, .vacancy-item, .job-item, article")
            count = await cards.count()
            log.info("workspace_search_results", query=query, count=count)

            for i in range(min(count, 50)):
                card = cards.nth(i)
                try:
                    vacancy = await self._parse_card(card)
                    if vacancy:
                        vacancies.append(vacancy)
                except Exception as e:
                    log.warning("workspace_card_parse_error", index=i, error=str(e))

            await page.close()

        except Exception as e:
            log.error("workspace_search_error", error=str(e))
            await page.close()

        return vacancies

    async def _parse_card(self, card) -> ParsedVacancy | None:
        title_el = card.locator("h2 a, h3 a, .vacancy-title a, .job-title a").first
        if await title_el.count() == 0:
            return None

        title = await title_el.inner_text()
        href = await title_el.get_attribute("href") or ""
        url = href if href.startswith("http") else f"{WORKSPACE_BASE}{href}"
        ext_id = href.split("/")[-1] if href else ""

        company_el = card.locator(".company, .employer, .company-name").first
        company_name = await company_el.inner_text() if await company_el.count() > 0 else ""

        salary_el = card.locator(".salary, .price, .budget").first
        salary_text = await salary_el.inner_text() if await salary_el.count() > 0 else ""
        salary_from, salary_to, currency = self._parse_salary(salary_text)

        location_el = card.locator(".location, .city, .geo").first
        location = await location_el.inner_text() if await location_el.count() > 0 else ""

        is_remote = "удалён" in location.lower() or "remote" in location.lower()

        return ParsedVacancy(
            platform="workspace",
            external_id=ext_id,
            url=url,
            title=title.strip(),
            company_name=company_name.strip(),
            salary_from=salary_from,
            salary_to=salary_to,
            salary_currency=currency,
            location=location.strip(),
            is_remote=is_remote,
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

            title = await page.locator("h1").first.inner_text() if await page.locator("h1").count() > 0 else ""
            desc_el = page.locator(".description, .vacancy-description, .content, .job-description").first
            description = await desc_el.inner_text() if await desc_el.count() > 0 else ""

            await page.close()
            return ParsedVacancy(
                platform="workspace",
                external_id=url.split("/")[-1],
                url=url,
                title=title.strip(),
                description=description.strip(),
            )

        except Exception as e:
            log.error("workspace_details_error", url=url, error=str(e))
            await page.close()
            return None

    async def check_messages(self) -> list[dict]:
        page = await self._get_page()
        messages = []
        try:
            await page.goto(f"{WORKSPACE_BASE}/messages", wait_until="domcontentloaded")
            await random_delay(2, 3)

            items = page.locator(".message-item, .dialog-item, .chat-item")
            for i in range(min(await items.count(), 20)):
                item = items.nth(i)
                unread = await item.locator(".unread, .new, .badge").count() > 0
                if unread:
                    sender = await item.locator(".sender, .name").first.inner_text() if await item.locator(
                        ".sender, .name").count() > 0 else ""
                    preview = await item.locator(".preview, .text").first.inner_text() if await item.locator(
                        ".preview, .text").count() > 0 else ""
                    messages.append({
                        "platform": "workspace",
                        "sender": sender.strip(),
                        "text": preview.strip(),
                    })

            await page.close()
        except Exception as e:
            log.error("workspace_messages_error", error=str(e))
            await page.close()

        return messages
