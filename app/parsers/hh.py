import re
import json

import structlog
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.config import settings
from app.parsers.base import BaseParser, ParsedVacancy
from app.utils.anti_detect import random_delay, human_type, human_scroll

log = structlog.get_logger()

HH_BASE = "https://hh.ru"
HH_SEARCH = "https://hh.ru/search/vacancy"


class HHParser(BaseParser):
    platform = "hh"

    async def login(self) -> bool:
        if not settings.hh_login:
            log.warning("hh_login_skip", reason="credentials not set")
            return False

        page = await self._get_page()
        try:
            await page.goto(f"{HH_BASE}/account/login", wait_until="domcontentloaded")
            await random_delay(2, 4)

            # Проверяем, залогинены ли уже
            if await self._is_logged_in(page):
                log.info("hh_already_logged_in")
                await page.close()
                return True

            # Переключаемся на вход по паролю
            pwd_tab = page.locator('[data-qa="expand-login-by-password"]')
            if await pwd_tab.count() > 0:
                await pwd_tab.click()
                await random_delay(1, 2)

            await human_type(page, '[data-qa="login-input-username"]', settings.hh_login)
            await random_delay(0.5, 1.5)
            await human_type(page, '[data-qa="login-input-password"]', settings.hh_password)
            await random_delay(0.5, 1)

            await page.click('[data-qa="account-login-submit"]')
            await page.wait_for_load_state("networkidle", timeout=15000)
            await random_delay(2, 4)

            if await self._is_logged_in(page):
                log.info("hh_login_success")
                await self._save_session()
                await page.close()
                return True

            log.error("hh_login_failed")
            await page.close()
            return False

        except Exception as e:
            log.error("hh_login_error", error=str(e))
            await page.close()
            return False

    async def _is_logged_in(self, page: Page) -> bool:
        try:
            return await page.locator('[data-qa="mainmenu_myResumes"]').count() > 0
        except Exception:
            return False

    async def search_vacancies(self, query: str, **filters) -> list[ParsedVacancy]:
        page = await self._get_page()
        vacancies = []

        try:
            params = {
                "text": query,
                "area": "1",  # Москва
                "search_field": "name",
                "enable_snippets": "true",
                "clusters": "true",
                "no_magic": "true",
            }

            # Удалёнка
            if filters.get("remote", True):
                params["schedule"] = "remote"

            # Зарплата
            if filters.get("salary_from"):
                params["salary"] = str(filters["salary_from"])
                params["only_with_salary"] = "true"

            # Опыт
            experience_map = {
                "no": "noExperience",
                "1-3": "between1And3",
                "3-6": "between3And6",
                "6+": "moreThan6",
            }
            if filters.get("experience") in experience_map:
                params["experience"] = experience_map[filters["experience"]]

            query_str = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{HH_SEARCH}?{query_str}"

            await page.goto(url, wait_until="domcontentloaded")
            await random_delay(2, 5)
            await human_scroll(page, 500)

            # Парсим карточки вакансий
            cards = page.locator('[data-qa="vacancy-serp__vacancy"]')
            count = await cards.count()
            log.info("hh_search_results", query=query, count=count)

            for i in range(min(count, 50)):
                card = cards.nth(i)
                try:
                    vacancy = await self._parse_card(card)
                    if vacancy:
                        vacancies.append(vacancy)
                except Exception as e:
                    log.warning("hh_card_parse_error", index=i, error=str(e))
                    continue

            await page.close()

        except Exception as e:
            log.error("hh_search_error", error=str(e))
            await page.close()

        return vacancies

    async def _parse_card(self, card) -> ParsedVacancy | None:
        title_el = card.locator('[data-qa="serp-item__title"]')
        if await title_el.count() == 0:
            return None

        title = await title_el.inner_text()
        url = await title_el.get_attribute("href") or ""

        # Извлекаем ID из URL
        ext_id_match = re.search(r"/vacancy/(\d+)", url)
        ext_id = ext_id_match.group(1) if ext_id_match else ""

        # Компания
        company_el = card.locator('[data-qa="vacancy-serp__vacancy-employer"]')
        company_name = ""
        company_url = ""
        if await company_el.count() > 0:
            company_name = await company_el.inner_text()
            link = company_el.locator("a")
            if await link.count() > 0:
                company_url = await link.get_attribute("href") or ""

        # Зарплата
        salary_from, salary_to, currency = None, None, ""
        salary_el = card.locator('[data-qa="vacancy-serp__vacancy-compensation"]')
        if await salary_el.count() > 0:
            salary_text = await salary_el.inner_text()
            salary_from, salary_to, currency = self._parse_salary(salary_text)

        # Локация
        location = ""
        loc_el = card.locator('[data-qa="vacancy-serp__vacancy-address"]')
        if await loc_el.count() > 0:
            location = await loc_el.inner_text()

        is_remote = "удалённ" in location.lower() or "remote" in location.lower()

        return ParsedVacancy(
            platform="hh",
            external_id=ext_id,
            url=url.split("?")[0] if url else "",
            title=title.strip(),
            company_name=company_name.strip(),
            company_url=company_url,
            salary_from=salary_from,
            salary_to=salary_to,
            salary_currency=currency,
            location=location.strip(),
            is_remote=is_remote,
        )

    def _parse_salary(self, text: str) -> tuple[int | None, int | None, str]:
        text = text.replace(" ", "").replace("\xa0", "").replace(" ", "")
        currency = ""
        if "₽" in text or "руб" in text:
            currency = "RUB"
        elif "$" in text or "USD" in text:
            currency = "USD"
        elif "€" in text or "EUR" in text:
            currency = "EUR"

        numbers = [int(x) for x in re.findall(r"\d+", text)]
        if "от" in text and "до" in text and len(numbers) >= 2:
            return numbers[0], numbers[1], currency
        elif "от" in text and numbers:
            return numbers[0], None, currency
        elif "до" in text and numbers:
            return None, numbers[0], currency
        elif numbers:
            return numbers[0], numbers[-1] if len(numbers) > 1 else None, currency
        return None, None, currency

    async def get_vacancy_details(self, url: str) -> ParsedVacancy | None:
        page = await self._get_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await random_delay(2, 4)

            title_el = page.locator('[data-qa="vacancy-title"]')
            title = await title_el.inner_text() if await title_el.count() > 0 else ""

            desc_el = page.locator('[data-qa="vacancy-description"]')
            description = await desc_el.inner_text() if await desc_el.count() > 0 else ""

            # Навыки
            skills = []
            skill_tags = page.locator('[data-qa="bloko-tag__text"]')
            for i in range(await skill_tags.count()):
                skills.append(await skill_tags.nth(i).inner_text())

            # Опыт
            exp_el = page.locator('[data-qa="vacancy-experience"]')
            experience = await exp_el.inner_text() if await exp_el.count() > 0 else ""

            # Тип занятости
            emp_el = page.locator('[data-qa="vacancy-view-employment-mode"]')
            employment = await emp_el.inner_text() if await emp_el.count() > 0 else ""

            ext_id_match = re.search(r"/vacancy/(\d+)", url)
            ext_id = ext_id_match.group(1) if ext_id_match else ""

            await page.close()
            return ParsedVacancy(
                platform="hh",
                external_id=ext_id,
                url=url,
                title=title.strip(),
                description=description.strip(),
                experience=experience.strip(),
                employment_type=employment.strip(),
                skills=skills,
            )

        except Exception as e:
            log.error("hh_details_error", url=url, error=str(e))
            await page.close()
            return None

    async def apply_to_vacancy(self, url: str, cover_letter: str) -> bool:
        page = await self._get_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await random_delay(2, 4)

            # Кнопка "Откликнуться"
            apply_btn = page.locator('[data-qa="vacancy-response-link-top"]')
            if await apply_btn.count() == 0:
                apply_btn = page.locator('[data-qa="vacancy-response-link-bottom"]')

            if await apply_btn.count() == 0:
                log.warning("hh_apply_no_button", url=url)
                await page.close()
                return False

            await apply_btn.click()
            await random_delay(2, 4)

            # Сопроводительное письмо
            letter_area = page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
            if await letter_area.count() > 0:
                await letter_area.fill("")
                await human_type(page, '[data-qa="vacancy-response-popup-form-letter-input"]', cover_letter)
                await random_delay(1, 2)

            # Отправка
            submit = page.locator('[data-qa="vacancy-response-submit-popup"]')
            if await submit.count() > 0:
                await submit.click()
                await random_delay(2, 4)
                log.info("hh_apply_success", url=url)
                await self._save_session()
                await page.close()
                return True

            log.warning("hh_apply_no_submit", url=url)
            await page.close()
            return False

        except Exception as e:
            log.error("hh_apply_error", url=url, error=str(e))
            await page.close()
            return False

    async def check_messages(self) -> list[dict]:
        page = await self._get_page()
        messages = []
        try:
            await page.goto(f"{HH_BASE}/applicant/negotiations", wait_until="domcontentloaded")
            await random_delay(2, 4)

            items = page.locator('[data-qa="negotiations-list-item"]')
            count = await items.count()

            for i in range(min(count, 20)):
                item = items.nth(i)
                try:
                    title_el = item.locator('[data-qa="negotiations-item-title"]')
                    company_el = item.locator('[data-qa="negotiations-item-company"]')
                    status_el = item.locator('[data-qa="negotiations-item-status"]')

                    title = await title_el.inner_text() if await title_el.count() > 0 else ""
                    company = await company_el.inner_text() if await company_el.count() > 0 else ""
                    status = await status_el.inner_text() if await status_el.count() > 0 else ""

                    if "новое" in status.lower() or "приглашение" in status.lower():
                        link_el = item.locator("a").first
                        link = await link_el.get_attribute("href") if await link_el.count() > 0 else ""
                        messages.append({
                            "platform": "hh",
                            "title": title.strip(),
                            "company": company.strip(),
                            "status": status.strip(),
                            "url": link,
                        })
                except Exception:
                    continue

            await page.close()
        except Exception as e:
            log.error("hh_messages_error", error=str(e))
            await page.close()

        return messages
