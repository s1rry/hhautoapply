"""
HH.ru OAuth via official Android app credentials.

This bypasses DDoS Guard entirely because api.hh.ru is a clean API host
without anti-bot protection. The Android app credentials are publicly
known and used by many third-party HH clients.

Flow:
1. GET hh.ru/oauth/authorize?... with logged-in cookies → returns code
2. POST hh.ru/oauth/token (code + client_secret) → access_token + refresh_token
3. POST api.hh.ru/negotiations (Bearer token + vacancy_id + resume_id) → apply
4. Token cached + refreshed automatically
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

# Public HH Android app credentials — used by all OSS HH clients
CLIENT_ID = "HIOMIAS39CA9DICTA7JIO64LQKQJF5AGIK74G9ITJKLNEDAOH5FHS5G1JI7FOEGD"
CLIENT_SECRET = "V9M870DE342BGHFRUJ5FTCGCUA1482AN0DI8C5TFI9ULMA89H10N60NOP8I4JMVS"
REDIRECT_URI = "hhandroid://oauthresponse"

UA = "ru.hh.android/8.116 (Android 13; samsung SM-S908B)"
TOKEN_FILE = Path("data/hh_oauth_token.json")
COOKIES_FILE = Path("data/browser_sessions/hh_state.json")


def _load_hh_cookies() -> dict[str, str]:
    if not COOKIES_FILE.exists():
        return {}
    try:
        data = json.loads(COOKIES_FILE.read_text())
    except Exception:
        return {}
    out = {}
    for c in data.get("cookies", []):
        if "hh.ru" in c.get("domain", ""):
            out[c["name"]] = c["value"]
    return out


def _load_token() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def _save_token(token: dict):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token))


class HHOAuth:
    platform = "hh"

    async def _refresh(self, refresh_token: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r = await c.post(
                    "https://hh.ru/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "refresh_token": refresh_token,
                    },
                    headers={"User-Agent": UA},
                )
                if r.status_code == 200:
                    d = r.json()
                    return {
                        "access_token": d["access_token"],
                        "refresh_token": d.get("refresh_token", refresh_token),
                        "expires_at": time.time() + d.get("expires_in", 1209599),
                    }
                log.warning("oauth_refresh_failed", status=r.status_code, body=r.text[:200])
        except Exception as e:
            log.warning("oauth_refresh_error", error=str(e))
        return None

    async def _authorize_via_playwright(self) -> str | None:
        """Get OAuth authorization code by navigating to the authorize URL
        in a real Playwright browser. The browser intercepts the redirect to
        hhandroid:// and we capture the `code` parameter."""
        try:
            from app.utils.browser import browser_manager
        except ImportError:
            log.error("oauth_no_playwright")
            return None
        try:
            page = await browser_manager.new_page("hh")
            authorize_url = (
                "https://hh.ru/oauth/authorize?"
                f"response_type=code&client_id={CLIENT_ID}"
                f"&redirect_uri={REDIRECT_URI}&state=bot"
            )
            captured = {"code": None}

            # Intercept ANY request/response containing the auth code
            def _check(url: str):
                if not url or captured["code"]:
                    return
                mm = re.search(r"code=([^&\s]+)", url)
                if mm:
                    captured["code"] = mm.group(1)

            page.on("request", lambda req: _check(req.url))
            page.on("response", lambda resp: _check(resp.url))
            page.on("framenavigated", lambda f: _check(f.url))

            try:
                await page.goto(authorize_url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass  # hhandroid:// will fail navigation

            await page.wait_for_timeout(2000)

            # Click "Продолжить" / "Разрешить" / "Подтвердить"
            try:
                approve = await page.query_selector('[data-qa="oauth-grant-allow"]')
                if not approve:
                    approve = await page.query_selector(
                        'button:has-text("Продолжить"), button:has-text("Разрешить"), button:has-text("Подтвердить"), button[data-qa*="oauth-grant"]'
                    )
                if approve:
                    log.info("oauth_clicking_approve")
                    try:
                        await approve.click()
                    except Exception:
                        # Click might fail due to navigation — that's fine
                        pass
                    await page.wait_for_timeout(3000)
            except Exception as e:
                log.warning("oauth_approve_error", error=str(e))

            # Wait a bit more for redirect listeners to fire
            await page.wait_for_timeout(1500)

            try:
                await page.close()
            except Exception:
                pass

            if captured["code"]:
                return captured["code"]
            log.error("oauth_no_code_playwright_final", url=authorize_url[:100])
            return None
        except Exception as e:
            log.error("oauth_playwright_error", error=str(e))
            return None

    async def _authorize(self) -> dict | None:
        """Get OAuth token: authorize via Playwright + token exchange via httpx."""
        code = await self._authorize_via_playwright()
        if not code:
            return None
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r3 = await c.post(
                    "https://hh.ru/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "redirect_uri": REDIRECT_URI,
                        "code": code,
                    },
                    headers={
                        "User-Agent": UA,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            if r3.status_code == 200:
                d = r3.json()
                token = {
                    "access_token": d["access_token"],
                    "refresh_token": d.get("refresh_token", ""),
                    "expires_at": time.time() + d.get("expires_in", 1209599),
                }
                log.info("oauth_authorize_success", expires_in=d.get("expires_in"))
                return token
            log.error("oauth_token_exchange_failed", status=r3.status_code, body=r3.text[:200])
        except Exception as e:
            log.error("oauth_authorize_error", error=str(e))
        return None

    async def get_token(self) -> str:
        cached = _load_token()
        if cached and cached.get("expires_at", 0) > time.time() + 300:
            return cached["access_token"]
        # Try refresh
        if cached and cached.get("refresh_token"):
            new = await self._refresh(cached["refresh_token"])
            if new:
                _save_token(new)
                return new["access_token"]
        # Full authorize
        new = await self._authorize()
        if new:
            _save_token(new)
            return new["access_token"]
        return ""

    async def apply(self, vacancy_id: str, message: str = "") -> tuple[bool | str, dict]:
        rhash = settings.hh_resume_id
        if not rhash:
            return False, {"error": "no_resume_id"}

        data: dict[str, Any] = {"vacancy_id": str(vacancy_id), "resume_id": rhash}
        if message:
            data["message"] = message

        async def _post(token: str):
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                return await c.post(
                    "https://api.hh.ru/negotiations",
                    headers={
                        "User-Agent": UA,
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data=data,
                )

        token = await self.get_token()
        if not token:
            return False, {"error": "no_oauth_token"}

        try:
            r = await _post(token)
        except httpx.RequestError as e:
            return False, {"error": f"http: {e}"}

        # Check body BEFORE retrying on 401/403 — these statuses may be
        # business errors (already applied, vacancy archived, etc.)
        body_text = r.text or ""
        if r.status_code in (401, 403):
            # HH uses 403 with body for business errors. Only retry if it really
            # looks like a token problem (no body or auth keywords).
            looks_like_auth_dead = (
                not body_text
                or "token" in body_text.lower()
                or "unauthorized" in body_text.lower()
                or "auth" in body_text.lower() and "applied" not in body_text.lower()
            )
            if looks_like_auth_dead and "already" not in body_text.lower():
                log.warning("oauth_token_died_retrying", body=body_text[:200])
                TOKEN_FILE.unlink(missing_ok=True)
                token2 = await self.get_token()
                if token2:
                    try:
                        r = await _post(token2)
                    except httpx.RequestError as e:
                        return False, {"error": f"http_retry: {e}"}

        if r.status_code in (200, 201, 204):
            return True, {"status": r.status_code}
        # Parse JSON body for both 400 AND 403 — HH uses both for business errors
        if r.status_code in (400, 403):
            try:
                d = r.json()
            except Exception:
                d = {"raw": (r.text or "")[:200]}
            errors = d.get("errors") or []
            err_value = ""
            if errors and isinstance(errors, list):
                err_value = errors[0].get("value", "") or errors[0].get("type", "")
            elif d.get("description"):
                err_value = str(d.get("description"))
            low = (err_value or "").lower() + " " + str(d).lower()
            if "limit" in low and "applied" not in low:
                return False, {"error": "daily_limit", "data": d}
            if "already" in low or "duplicate" in low:
                return "already", {"data": d}
            if "test" in low or "questionnaire" in low:
                return False, {"error": "needs_test", "data": d}
            if "archived" in low or "not_found" in low or "vacancy_not_found" in low or "unavailable" in low or "hidden" in low:
                return "already", {"error": "unavailable", "data": d}
            # hh.ru сам запретил отклик ("Can't respond to specified vacancy") —
            # часто требование к резюме/гео/seniority. Помечаем как "already",
            # чтобы не накапливать FAILED и не блокировать вакансию в failed_3plus.
            if "application_denied" in low or "can't respond" in low or "cant respond" in low or "respond to specified" in low:
                return "already", {"error": "application_denied", "data": d}
            return False, {"error": err_value or "bad_request", "status": r.status_code, "data": d}
        if r.status_code == 401:
            TOKEN_FILE.unlink(missing_ok=True)
            return False, {"error": "auth_expired"}
        if r.status_code == 404:
            return "already", {"error": "not_found"}
        return False, {"status": r.status_code, "body": (r.text or "")[:300]}

    # ── Negotiations (отклики) ─────────────────────────────────────────
    # GET  /negotiations?status=active&page=&per_page=100  — список откликов
    # DELETE /negotiations/active/{id}                     — отозвать/скрыть
    # state.id == "discard" → отказ работодателя.

    async def list_negotiations(self, status: str = "active") -> list[dict]:
        """Собрать все отклики постранично. Пусто, если нет токена."""
        token = await self.get_token()
        if not token:
            return []
        out: list[dict] = []
        headers = {"User-Agent": UA, "Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=20, verify=False) as c:
                page = 0
                while True:
                    r = await c.get(
                        "https://api.hh.ru/negotiations",
                        headers=headers,
                        params={"status": status, "page": page, "per_page": 100},
                    )
                    if r.status_code != 200:
                        log.warning("neg_list_failed", status=r.status_code, body=r.text[:200])
                        break
                    d = r.json()
                    items = d.get("items", [])
                    if not items:
                        break
                    out.extend(items)
                    if page + 1 >= d.get("pages", 0):
                        break
                    page += 1
        except Exception as e:
            log.warning("neg_list_error", error=str(e))
        return out

    async def _delete_negotiation(self, nid: str, with_decline_message: bool) -> bool:
        token = await self.get_token()
        if not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r = await c.delete(
                    f"https://api.hh.ru/negotiations/active/{nid}",
                    headers={"User-Agent": UA, "Authorization": f"Bearer {token}"},
                    params={"with_decline_message": str(with_decline_message).lower()},
                )
            if r.status_code in (200, 204):
                return True
            log.warning("neg_delete_failed", nid=nid, status=r.status_code, body=r.text[:150])
        except Exception as e:
            log.warning("neg_delete_error", nid=nid, error=str(e))
        return False

    async def clear_negotiations(
        self, older_than_days: int | None = None, dry_run: bool = False
    ) -> dict:
        """Удалить отклики.

        older_than_days=None  → только отказы (state == "discard").
        older_than_days=N     → любые отклики, обновлённые больше N дней назад.

        Возвращает {"scanned", "deleted", "names": [...], "error"?}.
        """
        import datetime as _dt

        items = await self.list_negotiations(status="active")
        if not items:
            tok = await self.get_token()
            if not tok:
                return {"scanned": 0, "deleted": 0, "names": [], "error": "no_oauth_token"}
            return {"scanned": 0, "deleted": 0, "names": []}

        deleted = 0
        names: list[str] = []
        now = _dt.datetime.now(_dt.timezone.utc)

        for neg in items:
            state_id = (neg.get("state") or {}).get("id", "")
            is_discard = state_id == "discard"

            if older_than_days is not None:
                upd = neg.get("updated_at") or neg.get("created_at") or ""
                try:
                    dt_upd = _dt.datetime.strptime(upd, "%Y-%m-%dT%H:%M:%S%z")
                except (ValueError, TypeError):
                    continue
                if (now - dt_upd).days <= older_than_days:
                    continue
            elif not is_discard:
                continue

            vac = neg.get("vacancy") or {}
            vac_name = vac.get("name", "без названия")
            if dry_run:
                deleted += 1
                if len(names) < 30:
                    names.append(vac_name)
                continue

            # Для активного отклика (не отказ) нужно передать decline-сообщение.
            ok = await self._delete_negotiation(
                str(neg.get("id")), with_decline_message=not is_discard
            )
            if ok:
                deleted += 1
                if len(names) < 30:
                    names.append(vac_name)

        return {"scanned": len(items), "deleted": deleted, "names": names}


hh_oauth = HHOAuth()
