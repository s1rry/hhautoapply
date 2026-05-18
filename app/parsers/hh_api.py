"""
HH.ru API client — fast applies via direct HTTP requests
(вместо Playwright). Использует существующие cookies из Playwright-сессии.

Скорость: ~1 сек на отклик вместо 60-90 сек.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

HH_STATE_PATH = Path("data/browser_sessions/hh_state.json")


def _load_cookies() -> dict[str, str]:
    """Load cookies from Playwright storage state into a flat dict."""
    if not HH_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(HH_STATE_PATH.read_text())
    except Exception:
        return {}
    cookies = {}
    for c in data.get("cookies", []):
        # Only top-level hh.ru cookies (skip .chatik.hh.ru etc)
        domain = c.get("domain", "")
        if "hh.ru" in domain:
            cookies[c["name"]] = c["value"]
    return cookies


def _headers(xsrf: str, accept_html: bool = False) -> dict[str, str]:
    accept = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        if accept_html
        else "application/json, text/javascript, */*; q=0.01"
    )
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Origin": "https://hh.ru",
        "Referer": "https://hh.ru/",
        "X-Requested-With": "XMLHttpRequest",
        "X-XsrfToken": xsrf,
    }


def _randomize_letter(text: str) -> str:
    """Add tiny randomness so cover letters don't dedupe in hh-side."""
    # Random invisible variation
    if random.random() < 0.5:
        text = text.replace(". ", ".  ", 1) if ". " in text else text
    return text


class HHApiClient:
    platform = "hh"

    def __init__(self):
        self._cookies: dict[str, str] = {}
        self._xsrf: str = ""

    def reload_cookies(self) -> bool:
        self._cookies = _load_cookies()
        self._xsrf = self._cookies.get("_xsrf", "")
        if not self._xsrf:
            log.warning("hh_api_no_xsrf")
            return False
        return True

    async def is_logged_in(self) -> bool:
        if not self.reload_cookies():
            return False
        # Use HTML accept header for the resumes page
        async with httpx.AsyncClient(
            cookies=self._cookies,
            headers=_headers(self._xsrf, accept_html=True),
            timeout=15,
        ) as c:
            try:
                r = await c.get("https://hh.ru/applicant/resumes", follow_redirects=False)
                if r.status_code in (301, 302, 303):
                    loc = r.headers.get("location", "")
                    if "/account/login" in loc or "/auth/" in loc:
                        return False
                    # Some redirects within /applicant/ are still ok
                if r.status_code != 200:
                    return False
                body = r.text or ""
                # If we see the user resume page markers — logged in
                if 'data-qa="resume"' in body or '"resumesList"' in body:
                    return True
                # If body asks for login — not logged in
                if 'data-qa="account-login"' in body or "Войти на сайт" in body:
                    return False
                # Default: if status 200 on /applicant/* without redirect to login — logged in
                return True
            except Exception as e:
                log.warning("hh_api_login_check_error", error=str(e))
                return False

    async def fetch_applied_vacancy_ids(self) -> set[str]:
        """Scrape /applicant/negotiations HTML for vacancy IDs the user
        has already applied to. Walks pagination via ?page=N until empty."""
        if not self.reload_cookies():
            return set()
        ids: set[str] = set()
        async with httpx.AsyncClient(
            cookies=self._cookies,
            headers=_headers(self._xsrf, accept_html=True),
            timeout=25,
        ) as c:
            for page in range(0, 30):  # up to ~30 pages of negotiations
                try:
                    r = await c.get(
                        "https://hh.ru/applicant/negotiations",
                        params={"page": page},
                        follow_redirects=True,
                    )
                except Exception as e:
                    log.warning("hh_api_fetch_applied_error", error=str(e), page=page)
                    break
                if r.status_code not in (200, 406):
                    break
                body = r.text or ""
                if not body:
                    break
                # Extract vacancy IDs from URLs in markup
                page_ids = set(re.findall(r"/vacancy/(\d+)", body))
                # Filter out IDs that may appear in nav/recommendations
                # (these usually appear in /vacancy/ links inside negotiation cards)
                new_ids = page_ids - ids
                if not new_ids and page > 0:
                    # No new ids on this page → end
                    break
                ids |= page_ids
                # No pagination marker — break if page got generic markup
                if len(page_ids) < 5 and page > 0:
                    break
        log.info("hh_api_fetched_applied_ids", count=len(ids))
        return ids

    async def apply(self, vacancy_id: str, cover_letter: str, resume_hash: str | None = None) -> tuple[bool | str, dict]:
        """Submit application via internal HH API.

        Returns (result, info):
          result is True (sent) / "already" / False (failed)
          info contains http details / error
        """
        if not self.reload_cookies():
            return False, {"error": "no cookies / not logged in"}

        rhash = resume_hash or settings.hh_resume_id or ""
        if not rhash:
            return False, {"error": "HH_RESUME_ID not set in .env"}

        url = "https://hh.ru/applicant/vacancy_response/popup"
        form = {
            "resume_hash": rhash,
            "vacancy_id": str(vacancy_id),
            "letterRequired": "true",
            "letter": _randomize_letter(cover_letter or ""),
            "lux": "true",
            "ignore_postponed": "true",
        }
        headers = _headers(self._xsrf)
        headers["Referer"] = f"https://hh.ru/vacancy/{vacancy_id}"
        async with httpx.AsyncClient(cookies=self._cookies, headers=headers, timeout=20) as c:
            try:
                r = await c.post(url, data=form)
            except httpx.RequestError as e:
                return False, {"error": f"http: {e}"}

            text = r.text or ""

            # Auth issues
            if r.status_code in (401, 403):
                return False, {"error": "auth_required", "status": r.status_code}

            # Hh-specific text markers (from Vlad's project)
            if "negotiations-limit-exceeded" in text:
                return False, {"error": "daily_limit"}
            if "test-required" in text:
                return False, {"error": "needs_test"}
            if "alreadyApplied" in text or "already-applied" in text:
                return "already", {}

            if r.status_code == 200:
                # Body usually contains JSON with shortVacancy when successful
                if "shortVacancy" in text or '"success":true' in text or '"responded":true' in text:
                    return True, {"status": 200}
                # Login page check
                if "data-qa=\"account-login\"" in text or "Войти на сайт" in text:
                    return False, {"error": "auth_required"}
                # Empty / unknown 200 — assume success
                return True, {"status": 200, "note": "no_marker"}

            if r.status_code in (204, 303):
                return True, {"status": r.status_code}

            return False, {"status": r.status_code, "body": text[:400]}


hh_api_client = HHApiClient()
