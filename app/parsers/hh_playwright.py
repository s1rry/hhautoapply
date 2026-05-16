"""
Playwright-based hh.ru operations: login, apply, messages, negotiations.
Only used when Playwright is available (VPS deployment).
"""

import asyncio
import re

import structlog
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.config import settings
from app.utils.browser import browser_manager
from app.utils.anti_detect import random_delay

log = structlog.get_logger()

HH_BASE = "https://hh.ru"
HH_LOGIN_URL = "https://hh.ru/account/login"
HH_NEGOTIATIONS = "https://hh.ru/applicant/negotiations"


class HHPlaywright:
    """Playwright-based hh.ru automation for login, apply, messages."""

    def __init__(self):
        self._logged_in = False
        self._page: Page | None = None

    async def _get_page(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page
        self._page = await browser_manager.new_page("hh")
        return self._page

    async def login(self) -> bool:
        """Login to hh.ru using saved session or credentials."""
        if self._logged_in:
            return True

        page = await self._get_page()

        # Check if already logged in via saved session
        try:
            await page.goto(HH_BASE, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)

            # Check for user menu (means logged in)
            logged = await page.query_selector('[data-qa="mainmenu_applicantProfile"]')
            if logged:
                self._logged_in = True
                log.info("hh_already_logged_in")
                await browser_manager.save_context("hh")
                return True
        except Exception as e:
            log.warning("hh_login_check_error", error=str(e))

        # Need to login with credentials
        if not settings.hh_login or not settings.hh_password:
            log.error("hh_credentials_missing")
            return False

        try:
            await page.goto(HH_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)

            # Click "Войти с паролем" if available
            pwd_btn = await page.query_selector('[data-qa="expand-login-by-password"]')
            if pwd_btn:
                await pwd_btn.click()
                await page.wait_for_timeout(1000)

            # Fill login
            login_input = await page.query_selector('[data-qa="login-input-username"]')
            if login_input:
                await login_input.fill(settings.hh_login)
            else:
                # Try alternative selector
                login_input = await page.query_selector('input[name="login"]')
                if login_input:
                    await login_input.fill(settings.hh_login)

            await page.wait_for_timeout(500)

            # Fill password
            pwd_input = await page.query_selector('[data-qa="login-input-password"]')
            if pwd_input:
                await pwd_input.fill(settings.hh_password)
            else:
                pwd_input = await page.query_selector('input[type="password"]')
                if pwd_input:
                    await pwd_input.fill(settings.hh_password)

            await page.wait_for_timeout(500)

            # Click submit
            submit_btn = await page.query_selector('[data-qa="account-login-submit"]')
            if submit_btn:
                await submit_btn.click()
            else:
                await page.keyboard.press("Enter")

            # Wait for navigation
            await page.wait_for_timeout(5000)

            # Check if login was successful
            logged = await page.query_selector('[data-qa="mainmenu_applicantProfile"]')
            if logged:
                self._logged_in = True
                await browser_manager.save_context("hh")
                log.info("hh_login_success")
                return True

            # Check for captcha or error
            error_el = await page.query_selector('[data-qa="account-login-error"]')
            if error_el:
                error_text = await error_el.inner_text()
                log.error("hh_login_failed", reason=error_text)
            else:
                log.error("hh_login_failed", reason="unknown, possibly captcha")

            return False

        except Exception as e:
            log.error("hh_login_error", error=str(e))
            return False

    async def apply_to_vacancy(self, vacancy_url: str, cover_letter: str) -> bool:
        """Apply to vacancy via Playwright browser automation."""
        if not self._logged_in:
            if not await self.login():
                return False

        page = await self._get_page()

        try:
            await page.goto(vacancy_url, wait_until="domcontentloaded", timeout=20000)
            await random_delay(2, 5)

            # Find "Откликнуться" button
            apply_btn = await page.query_selector('[data-qa="vacancy-response-link-top"]')
            if not apply_btn:
                apply_btn = await page.query_selector('[data-qa="vacancy-response-link-bottom"]')
            if not apply_btn:
                # Maybe already applied
                applied_el = await page.query_selector('[data-qa="vacancy-response-link-view-topic"]')
                if applied_el:
                    log.info("hh_already_applied", url=vacancy_url)
                    return True
                log.warning("hh_apply_btn_not_found", url=vacancy_url)
                return False

            await apply_btn.click()
            await page.wait_for_timeout(3000)

            # Check if cover letter textarea appeared (modal)
            letter_area = await page.query_selector('[data-qa="vacancy-response-popup-form-letter-input"]')
            if not letter_area:
                letter_area = await page.query_selector('textarea[name="text"]')

            if letter_area and cover_letter:
                await letter_area.fill(cover_letter)
                await page.wait_for_timeout(1000)

            # Select resume if resume picker is shown
            resume_select = await page.query_selector('[data-qa="vacancy-response-popup-form-resume-dropdown"]')
            if resume_select:
                await resume_select.click()
                await page.wait_for_timeout(500)
                # Click first resume option
                first_resume = await page.query_selector('[data-qa="vacancy-response-popup-form-resume-option"]')
                if first_resume:
                    await first_resume.click()
                    await page.wait_for_timeout(500)

            # Submit response
            submit_btn = await page.query_selector('[data-qa="vacancy-response-submit-popup"]')
            if not submit_btn:
                submit_btn = await page.query_selector('[data-qa="vacancy-response-letter-submit"]')
            if not submit_btn:
                # Try generic submit in the popup
                submit_btn = await page.query_selector('.vacancy-response-popup-actions button[type="submit"]')

            if submit_btn:
                await submit_btn.click()
                await page.wait_for_timeout(3000)

                # Check success
                success_el = await page.query_selector('[data-qa="vacancy-response-link-view-topic"]')
                if success_el:
                    log.info("hh_apply_success", url=vacancy_url)
                    await browser_manager.save_context("hh")
                    return True

                # Check if we're on negotiations page (redirect after success)
                if "/applicant/negotiations" in page.url:
                    log.info("hh_apply_success_redirect", url=vacancy_url)
                    await browser_manager.save_context("hh")
                    return True

            log.warning("hh_apply_uncertain", url=vacancy_url, current_url=page.url)
            return False

        except PlaywrightTimeout:
            log.error("hh_apply_timeout", url=vacancy_url)
            return False
        except Exception as e:
            log.error("hh_apply_error", url=vacancy_url, error=str(e))
            return False

    async def check_messages(self) -> list[dict]:
        """Check negotiations/messages on hh.ru."""
        if not self._logged_in:
            if not await self.login():
                return []

        page = await self._get_page()
        messages = []

        try:
            await page.goto(HH_NEGOTIATIONS, wait_until="domcontentloaded", timeout=20000)
            await random_delay(2, 4)

            # Parse negotiations list
            items = await page.query_selector_all('[data-qa="negotiations-item"]')
            if not items:
                # Try alternative selectors
                items = await page.query_selector_all('.negotiations-list-item')

            for item in items[:20]:  # Limit to 20 most recent
                try:
                    msg = await self._parse_negotiation_item(item)
                    if msg:
                        messages.append(msg)
                except Exception as e:
                    log.warning("hh_parse_negotiation_error", error=str(e))
                    continue

            log.info("hh_messages_fetched", count=len(messages))

        except Exception as e:
            log.error("hh_messages_error", error=str(e))

        return messages

    async def _parse_negotiation_item(self, item) -> dict | None:
        """Parse a single negotiation row from the page."""
        title_el = await item.query_selector('[data-qa="negotiations-item-title"]')
        if not title_el:
            title_el = await item.query_selector('a[href*="/vacancy/"]')

        title = await title_el.inner_text() if title_el else ""
        href = await title_el.get_attribute("href") if title_el else ""

        company_el = await item.query_selector('[data-qa="negotiations-item-company"]')
        company = await company_el.inner_text() if company_el else ""

        status_el = await item.query_selector('[data-qa="negotiations-item-status"]')
        status = await status_el.inner_text() if status_el else ""

        # Extract thread ID from href
        thread_id = ""
        if href:
            tid_match = re.search(r"/(\d+)/?$", href)
            if tid_match:
                thread_id = f"hh_{tid_match.group(1)}"

        # Check for new/unread messages indicator
        unread_el = await item.query_selector('.negotiations-item__unread, [data-qa="negotiations-item-unread"]')
        has_unread = unread_el is not None

        if not title and not status:
            return None

        return {
            "platform": "hh",
            "title": title.strip(),
            "company": company.strip(),
            "status": status.strip(),
            "text": f"Статус: {status.strip()}" if status else "",
            "thread_id": thread_id,
            "sender": company.strip(),
            "has_unread": has_unread,
        }

    async def check_negotiations_status(self) -> list[dict]:
        """Check the status of all active negotiations (invites, rejections, etc.)."""
        if not self._logged_in:
            if not await self.login():
                return []

        page = await self._get_page()
        statuses = []

        tabs = [
            ("invitations", f"{HH_NEGOTIATIONS}?page=0&filter=response&state=invitation"),
            ("discard", f"{HH_NEGOTIATIONS}?page=0&filter=response&state=discard"),
            ("active", f"{HH_NEGOTIATIONS}?page=0&filter=response&state=response"),
        ]

        for tab_name, url in tabs:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await random_delay(1, 3)

                items = await page.query_selector_all('[data-qa="negotiations-item"]')
                if not items:
                    items = await page.query_selector_all('.negotiations-list-item')

                for item in items[:10]:
                    msg = await self._parse_negotiation_item(item)
                    if msg:
                        msg["tab"] = tab_name
                        statuses.append(msg)

            except Exception as e:
                log.warning("hh_negotiations_tab_error", tab=tab_name, error=str(e))

        log.info("hh_negotiations_status", total=len(statuses))
        return statuses

    async def close(self):
        """Close page and save session."""
        if self._page and not self._page.is_closed():
            await browser_manager.save_context("hh")
            await self._page.close()
            self._page = None
        self._logged_in = False


# Singleton - created only when Playwright is available
hh_playwright: HHPlaywright | None = None

try:
    from app.utils.browser import browser_manager  # noqa: F811
    hh_playwright = HHPlaywright()
except ImportError:
    pass
