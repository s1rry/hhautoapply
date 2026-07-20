"""
Per-user клиент hh.ru (Фаза 3, мультиюзер).

Работает с токеном конкретного пользователя (из строки User), а не с
глобальным файлом. Умеет: обновить токен, искать вакансии по настройкам
пользователя, откликаться. Одиночный путь (HHOAuth) не трогаем.
"""
from __future__ import annotations

import json
import re
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
        # hh просит подтвердить, что за откликами человек. Решать её за
        # пользователя нельзя — это обход антибот-защиты и прямой путь к бану
        # его аккаунта. Останавливаем задачу и просим пройти вручную.
        if "captcha" in low:
            return False, {"error": "captcha_required", "data": d}
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
    if status_code == 403 and "token_revoked" in (body_text or "").lower():
        # Пользователь отозвал доступ на hh — сам бот починить это не может.
        return False, {"error": "token_revoked"}
    if status_code == 404:
        return "already", {"error": "not_found"}
    if status_code == 429:
        # hh троттлит. Тело важно: там бывает и «слишком часто», и дневной лимит.
        low = (body_text or "").lower()
        if "limit" in low and "exceeded" in low:
            return False, {"error": "daily_limit", "body": (body_text or "")[:300]}
        return False, {"error": "rate_limited", "status": 429,
                       "body": (body_text or "")[:300]}
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
        # Сколько всего вакансий вернул последний search/similar (поле found).
        self.last_found: int | None = None
        # hh ответил token_revoked — пользователь отозвал доступ, нужен переконнект.
        self.token_revoked: bool = False

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
                self.last_found = data.get("found")
                log.info("user_search_ok", text=str(q.get("text"))[:80],
                         page=page, found=data.get("found"), items=len(items))
                return items
            if r.status_code == 403 and "token_revoked" in (r.text or "").lower():
                self.token_revoked = True
            log.warning("user_search_failed", status=r.status_code, body=r.text[:200])
        except Exception as e:
            log.warning("user_search_error", error=str(e))
        return []

    async def similar_vacancies(self, resume_id: str, per_page: int = 50, page: int = 0,
                                params: dict | None = None) -> list[dict]:
        """Рекомендованные вакансии под резюме (GET /resumes/{id}/similar_vacancies).
        Персональная лента hh — большая и обновляется сама, в отличие от узкого
        поиска по ключу. Возвращает items той же формы, что и search."""
        if not resume_id or not await self.ensure_token():
            return []
        q = dict(params or {})
        q["per_page"] = per_page
        q["page"] = page
        headers = {"Authorization": f"Bearer {self.access_token}", "User-Agent": UA}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{API}/resumes/{resume_id}/similar_vacancies",
                                headers=headers, params=q)
            if r.status_code == 200:
                data = r.json() or {}
                items = data.get("items") or []
                self.last_found = data.get("found")
                log.info("user_recommend_ok", resume=resume_id, page=page,
                         found=data.get("found"), items=len(items))
                return items
            if r.status_code == 403 and "token_revoked" in (r.text or "").lower():
                self.token_revoked = True
            log.warning("user_recommend_failed", status=r.status_code, body=r.text[:200])
        except Exception as e:
            log.warning("user_recommend_error", error=str(e))
        return []

    async def negotiations(self, per_page: int = 100, page: int = 0) -> tuple[list[dict], int, int]:
        """Отклики пользователя (GET /negotiations). Возвращает (items, found, pages).
        В items у каждого есть state (id: response/interview/discard — приглашение
        на собеседование это "interview", значения "invitation" в API нет),
        viewed_by_opponent, vacancy.id, updated_at — из них считаем приглашения/просмотры."""
        if not await self.ensure_token():
            return [], 0, 0
        headers = {"Authorization": f"Bearer {self.access_token}", "User-Agent": UA}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{API}/negotiations", headers=headers,
                                params={"per_page": per_page, "page": page})
            if r.status_code == 200:
                data = r.json() or {}
                return data.get("items") or [], data.get("found") or 0, data.get("pages") or 0
            if r.status_code == 403 and "token_revoked" in (r.text or "").lower():
                self.token_revoked = True
            log.warning("user_negotiations_failed", status=r.status_code, body=r.text[:200])
        except Exception as e:
            log.warning("user_negotiations_error", error=str(e))
        return [], 0, 0

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

    async def clone_resume(self) -> dict:
        """Клонировать резюме (POST /resume_profile) — как в hh-applicant-tool.

        Даёт свежую копию, чтобы обойти запрет hh откликаться на вакансию дважды.
        Возвращает {"ok": bool, "error": ...}.
        """
        if not self.resume_id:
            return {"ok": False, "error": "no_resume_id"}
        if not await self.ensure_token():
            return {"ok": False, "error": "no_token"}
        headers = {"Authorization": f"Bearer {self.access_token}", "User-Agent": UA,
                   "Content-Type": "application/json"}
        payload = {"clone_resume_id": self.resume_id, "additional_properties": {"any_job": True}}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{API}/resume_profile", headers=headers, json=payload)
            if r.status_code in (200, 201, 204):
                return {"ok": True}
            log.warning("clone_resume_failed", status=r.status_code, body=r.text[:200])
            return {"ok": False, "error": f"hh {r.status_code}"}
        except Exception as e:
            log.warning("clone_resume_error", error=str(e))
            return {"ok": False, "error": str(e)[:120]}

    async def apply_with_test(self, vacancy_id: str, letter: str, cookies_state: dict) -> tuple[bool | str, dict]:
        """Отклик на вакансию с тестом через веб (как hh-applicant-tool).

        Парсит vacancyTests со страницы отклика, отвечает ИИ, шлёт форму на
        /applicant/vacancy_response/popup. Требует веб-cookies. Хрупко (веб hh
        может меняться) — best-effort: при любой проблеме возвращает (False, ...).
        """
        from app.ai.claude import claude_ai

        web_cookies: dict[str, str] = {}
        for ck in (cookies_state or {}).get("cookies") or []:
            if "hh.ru" in (ck.get("domain") or ""):
                web_cookies[ck.get("name")] = ck.get("value")
        xsrf = web_cookies.get("_xsrf")
        if not xsrf:
            return False, {"error": "no_web_session"}

        resp_url = (f"https://hh.ru/applicant/vacancy_response?vacancyId={vacancy_id}"
                    "&startedWithQuestion=false&hhtmFrom=vacancy")
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.get(resp_url, cookies=web_cookies, headers={"User-Agent": UA})
                marker = ',"vacancyTests":'
                pos = r.text.find(marker)
                if pos == -1:
                    return False, {"error": "no_tests_marker"}
                tests_data, _ = json.JSONDecoder().raw_decode(r.text, pos + len(marker))
                test_data = (tests_data or {}).get(str(vacancy_id))
                if not test_data:
                    return False, {"error": "no_test_data"}

                payload = {
                    "_xsrf": xsrf, "uidPk": test_data["uidPk"], "guid": test_data["guid"],
                    "startTime": test_data["startTime"], "testRequired": test_data["required"],
                    "vacancy_id": vacancy_id, "resume_hash": self.resume_id,
                    "ignore_postponed": "true", "incomplete": "false",
                    "mark_applicant_visible_in_vacancy_country": "false",
                    "country_ids": "[]", "lux": "true", "withoutTest": "no", "letter": letter,
                }
                for task in test_data.get("tasks") or []:
                    field = f"task_{task['id']}"
                    solutions = task.get("candidateSolutions") or []
                    question = re.sub(r"<[^>]+>", " ", task.get("description") or "").strip()
                    if solutions:
                        opts = "\n".join(f"{s['id']}: {re.sub(r'<[^>]+>', ' ', s['text'])}" for s in solutions)
                        ans = await claude_ai.complete(
                            f"Вопрос: {question}\nВарианты:\n{opts}\nВыбери ID правильного ответа. Пришли только ID.")
                        m = re.search(r"\d+", ans or "")
                        payload[field] = m.group(0) if m else str(solutions[len(solutions) // 2]["id"])
                    else:
                        ans = await claude_ai.complete(f"Дай краткий профессиональный ответ на вопрос: {question}")
                        payload[f"{field}_text"] = ans or "Да"

                pr = await c.post(
                    "https://hh.ru/applicant/vacancy_response/popup",
                    data=payload, cookies=web_cookies,
                    headers={"Referer": resp_url, "X-Hhtmfrom": "vacancy",
                             "X-Hhtmsource": "vacancy_response", "X-Requested-With": "XMLHttpRequest",
                             "X-Xsrftoken": xsrf, "User-Agent": UA},
                )
                if pr.status_code in (200, 201, 204):
                    return True, {"via": "test"}
                log.warning("apply_with_test_failed", status=pr.status_code, body=pr.text[:200])
                return False, {"status": pr.status_code}
        except Exception as e:
            log.warning("apply_with_test_error", error=str(e))
            return False, {"error": str(e)[:120]}

    async def hide_rejections(self, cookies_state: dict | None = None, max_pages: int = 20) -> dict:
        """Скрыть чаты-отказы (state=discard) из списка откликов hh.

        Список отказов берём через API (/negotiations, status=active). А реально
        СКРЫТЬ чат можно только через веб (POST /applicant/negotiations/trash,
        substate=HIDE) с веб-куками и XSRF — как делает hh-applicant-tool
        (--delete-chat). Поэтому нужны cookies_state (storage_state с логина).

        Возвращает {"hidden": N, "checked": M, "web": bool}. web=False → куки не
        заданы, отказы только «отменены» по API (в списке могут остаться).
        """
        if not await self.ensure_token():
            return {"hidden": 0, "checked": 0, "error": "no_token"}
        api_headers = {"Authorization": f"Bearer {self.access_token}", "User-Agent": UA}

        # Веб-куки hh.ru + XSRF из storage_state.
        web_cookies: dict[str, str] = {}
        xsrf = None
        if cookies_state:
            for ck in (cookies_state.get("cookies") or []):
                if "hh.ru" in (ck.get("domain") or ""):
                    web_cookies[ck.get("name")] = ck.get("value")
            xsrf = web_cookies.get("_xsrf")

        trash_headers = {
            "X-Hhtmfrom": "main", "X-Hhtmsource": "negotiation_list",
            "X-Requested-With": "XMLHttpRequest", "X-Xsrftoken": xsrf or "",
            "Referer": "https://hh.ru/applicant/negotiations?hhtmFrom=main&hhtmFromLabel=header",
            "User-Agent": UA,
        }
        hidden = checked = 0
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                for page in range(max_pages):
                    r = await c.get(f"{API}/negotiations", headers=api_headers,
                                    params={"page": page, "per_page": 100, "status": "active"})
                    if r.status_code != 200:
                        log.warning("negotiations_get_failed", status=r.status_code, body=r.text[:200])
                        break
                    data = r.json() or {}
                    items = data.get("items") or []
                    if not items:
                        break
                    for it in items:
                        checked += 1
                        if ((it.get("state") or {}).get("id") or "").lower() != "discard":
                            continue
                        nid = str(it.get("id") or "")
                        if not nid:
                            continue
                        if xsrf:
                            tr = await c.post(
                                "https://hh.ru/applicant/negotiations/trash",
                                headers=trash_headers, cookies=web_cookies,
                                data={"topic": nid,
                                      "query": "?hhtmFrom=main&hhtmFromLabel=header",
                                      "substate": "HIDE"},
                            )
                            if tr.status_code in (200, 204):
                                hidden += 1
                        else:
                            dr = await c.delete(f"{API}/negotiations/active/{nid}", headers=api_headers)
                            if dr.status_code in (200, 204):
                                hidden += 1
                    if page + 1 >= (data.get("pages") or 1):
                        break
        except Exception as e:
            log.warning("hide_rejections_error", error=str(e))
            return {"hidden": hidden, "checked": checked, "web": bool(xsrf), "error": str(e)[:120]}
        log.info("hide_rejections_done", hidden=hidden, checked=checked, web=bool(xsrf))
        return {"hidden": hidden, "checked": checked, "web": bool(xsrf)}

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
