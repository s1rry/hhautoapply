"""
Per-user клиент hh.ru (Фаза 3, мультиюзер).

Работает с токеном конкретного пользователя (из строки User), а не с
глобальным файлом. Умеет: обновить токен, искать вакансии по настройкам
пользователя, откликаться. Одиночный путь (HHOAuth) не трогаем.
"""
from __future__ import annotations

import json
import time

import httpx
import structlog

from app.parsers.hh_oauth import CLIENT_ID, CLIENT_SECRET, UA

log = structlog.get_logger()

API = "https://api.hh.ru"


def classify_apply(status_code: int, body_text: str) -> tuple[bool | str, dict]:
    """Разобрать ответ POST /negotiations в (результат, инфо).

    result: True (успех) | "already" (уже/недоступно) | False (ошибка).
    """
    if status_code in (200, 201, 204):
        return True, {"status": status_code}
    if status_code in (400, 403):
        try:
            d = json.loads(body_text)
        except Exception:
            d = {"raw": (body_text or "")[:200]}
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
        if any(k in low for k in ("archived", "not_found", "vacancy_not_found", "unavailable", "hidden")):
            return "already", {"error": "unavailable", "data": d}
        if any(k in low for k in ("application_denied", "can't respond", "cant respond", "respond to specified")):
            return "already", {"error": "application_denied", "data": d}
        return False, {"error": err_value or "bad_request", "status": status_code, "data": d}
    if status_code == 401:
        return False, {"error": "auth_expired"}
    if status_code == 404:
        return "already", {"error": "not_found"}
    return False, {"status": status_code, "body": (body_text or "")[:300]}


class HHUserClient:
    def __init__(self, access_token: str, refresh_token: str = "", resume_id: str | None = None,
                 expires_at: float | None = None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.resume_id = resume_id
        self.expires_at = expires_at or 0.0
        # Заполняется, если токен обновился — вызывающий сохранит в User.
        self.new_token: dict | None = None

    async def ensure_token(self) -> bool:
        """Обновить токен, если протух (или скоро протухнет). True — токен валиден."""
        if self.access_token and self.expires_at > time.time() + 300:
            return True
        if not self.refresh_token:
            return bool(self.access_token)
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r = await c.post(
                    "https://hh.ru/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "refresh_token": self.refresh_token,
                    },
                    headers={"User-Agent": UA},
                )
            if r.status_code == 200:
                d = r.json()
                self.access_token = d["access_token"]
                self.refresh_token = d.get("refresh_token", self.refresh_token)
                self.expires_at = time.time() + d.get("expires_in", 1209599)
                self.new_token = {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_at": self.expires_at,
                }
                return True
            log.warning("user_token_refresh_failed", status=r.status_code, body=r.text[:200])
        except Exception as e:
            log.warning("user_token_refresh_error", error=str(e))
        return bool(self.access_token)

    async def search(self, params: dict, per_page: int = 50, page: int = 0) -> list[dict]:
        """Искать вакансии по параметрам настроек пользователя."""
        if not await self.ensure_token():
            return []
        q = dict(params)
        q["per_page"] = per_page
        q["page"] = page
        headers = {"Authorization": f"Bearer {self.access_token}", "User-Agent": UA}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{API}/vacancies", headers=headers, params=q)
            if r.status_code == 200:
                data = r.json() or {}
                items = data.get("items") or []
                log.info("user_search_ok", text=str(q.get("text"))[:80],
                         page=page, found=data.get("found"), items=len(items))
                return items
            log.warning("user_search_failed", status=r.status_code, body=r.text[:200])
        except Exception as e:
            log.warning("user_search_error", error=str(e))
        return []

    async def bump_resume(self) -> bool:
        """Поднять резюме на hh (POST /resumes/{id}/publish). 204 = успех, 429 = рано."""
        if not self.resume_id or not await self.ensure_token():
            return False
        headers = {"Authorization": f"Bearer {self.access_token}", "User-Agent": UA}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{API}/resumes/{self.resume_id}/publish", headers=headers)
            return r.status_code in (200, 204)
        except Exception as e:
            log.warning("bump_resume_error", error=str(e))
            return False

    async def hide_rejections(self, max_pages: int = 20) -> dict:
        """Скрыть отклики со статусом «отказ» (discard) в списке откликов hh.

        Возвращает {"hidden": N, "checked": M}. hh помечает отказ работодателя
        state.id == 'discard'; убираем через DELETE /negotiations/active/{id}
        (как в hh-applicant-tool — этот эндпоинт реально работает по токену).
        """
        if not await self.ensure_token():
            return {"hidden": 0, "checked": 0, "error": "no_token"}
        headers = {"Authorization": f"Bearer {self.access_token}", "User-Agent": UA}
        hidden = checked = 0
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                for page in range(max_pages):
                    r = await c.get(
                        f"{API}/negotiations",
                        headers=headers,
                        params={"page": page, "per_page": 100, "order_by": "updated"},
                    )
                    if r.status_code != 200:
                        break
                    data = r.json() or {}
                    items = data.get("items") or []
                    if not items:
                        break
                    for it in items:
                        checked += 1
                        state = ((it.get("state") or {}).get("id") or "").lower()
                        if state != "discard":
                            continue
                        nid = str(it.get("id") or "")
                        if not nid:
                            continue
                        dr = await c.delete(f"{API}/negotiations/active/{nid}", headers=headers)
                        if dr.status_code in (200, 204):
                            hidden += 1
                    if page + 1 >= (data.get("pages") or 1):
                        break
        except Exception as e:
            log.warning("hide_rejections_error", error=str(e))
            return {"hidden": hidden, "checked": checked, "error": str(e)[:120]}
        log.info("hide_rejections_done", hidden=hidden, checked=checked)
        return {"hidden": hidden, "checked": checked}

    async def apply(self, vacancy_id: str, message: str = "") -> tuple[bool | str, dict]:
        """Откликнуться на вакансию токеном пользователя."""
        if not self.resume_id:
            return False, {"error": "no_resume_id"}
        if not await self.ensure_token():
            return False, {"error": "no_oauth_token"}
        data = {"vacancy_id": str(vacancy_id), "resume_id": self.resume_id}
        if message:
            data["message"] = message
        headers = {
            "User-Agent": UA,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r = await c.post(f"{API}/negotiations", headers=headers, data=data)
        except httpx.RequestError as e:
            return False, {"error": f"http: {e}"}
        return classify_apply(r.status_code, r.text or "")
