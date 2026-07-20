"""
Прогон автоотклика для одного пользователя (Фаза 3, мультиюзер).

Поиск идёт прямо по фильтрам пользователя (его настройки = параметры hh),
поэтому результаты уже релевантны — отдельный тяжёлый скоринг пока не нужен.
Отклик выполняется токеном пользователя, всё пишется с его user_id, с учётом
дневного лимита и окна расписания.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.models.user import User
from app.models.vacancy import Vacancy, VacancyStatus
from app.models.application import Application, ApplicationStatus
from app.ai.claude import claude_ai
from app.parsers.hh_user_client import HHUserClient
from app.parsers.letter_template import render_letter
from app.utils.anti_detect import random_delay

log = structlog.get_logger()

_REFUSAL = ("не могу", "as an ai", "извините", "не имею возможности")

# Потолок ИИ-оценок за один цикл на пользователя — защита от лишних трат/латентности
# при широком поиске (умный отбор делает отдельный ИИ-запрос на каждую вакансию).
MAX_SCORINGS_PER_CYCLE = 40
# До скольких страниц выдачи (по 50) заходить за цикл, чтобы добраться до
# ещё не отработанных вакансий, когда топ уже весь обработан.
MAX_SEARCH_PAGES = 10

# hh отвечает 429 при слишком частых откликах. Ждём и повторяем — пауза растёт
# с каждой попыткой (60с, 120с). Не помогло — останавливаем аккаунт до цикла.
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_PAUSE_SEC = 60

# Столько подряд неудачных оценок подряд считаем «ИИ лежит» и останавливаем
# аккаунт, чтобы не выжечь дневной лимит hh откликами вслепую.
MAX_SCORE_FAILS = 5


async def _build_letter(item: dict, title: str, st, resume_text: str,
                        ab_index: int = 0,
                        model: str | None = None) -> tuple[str, str | None]:
    """Собрать письмо. Возвращает (текст, вариант A/B|None).
      off      — без письма ("")
      required — письмо только если вакансия его требует
      always   — всегда; ИИ-персонализация при ai_enabled (иначе шаблон).
    Если включён A/B и заданы оба письма — по очереди шлём A/B как есть."""
    mode = getattr(st, "letter_mode", "always")
    if mode == "off":
        return "", None
    if mode == "required" and not item.get("response_letter_required"):
        return "", None
    contact = (getattr(st, "contact", "") or "").strip()
    # A/B тест: чередуем письмо A и B (по чётности отправленных).
    la = (getattr(st, "letter_a", "") or "").strip()
    lb = (getattr(st, "letter_b", "") or "").strip()
    if getattr(st, "ab_enabled", False) and la and lb:
        variant = "A" if ab_index % 2 == 0 else "B"
        text = render_letter(title, template=(la if variant == "A" else lb),
                             contact=(contact or None))
        if contact and contact not in text:
            text += f"\n\nКонтакты: {contact}"
        return text, variant
    if st.ai_enabled and resume_text:
        snip = item.get("snippet") or {}
        desc = " ".join(x for x in (snip.get("responsibility"), snip.get("requirement")) if x)
        company = (item.get("employer") or {}).get("name") or ""
        try:
            text, _, _ = await claude_ai.generate_cover_letter(
                title, desc, company_name=company, resume=resume_text,
                custom_prompt=(st.ai_custom_prompt or None), model=model
            )
            text = (text or "").strip()
            low = text.lower()
            if text and len(text) >= 40 and not any(m in low for m in _REFUSAL):
                if contact:
                    text += f"\n\nКонтакты: {contact}"
                return text, None
        except Exception as e:
            log.warning("ai_letter_failed", error=str(e))
    # Без ИИ: своё готовое письмо пользователя, иначе нейтральный шаблон.
    custom = (getattr(st, "custom_letter", "") or "").strip()
    text = render_letter(title, template=(custom or None), contact=(contact or None))
    if custom and contact and contact not in text:
        text += f"\n\nКонтакты: {contact}"
    return text, None


def _within_window(start: int, end: int) -> bool:
    hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
    return start <= hour < end


async def _sent_today(session, user_id: int, account_ref: str, task_id: int | None = None) -> int:
    conds = [
        Application.user_id == user_id,
        Application.account_ref == account_ref,
        Application.status == ApplicationStatus.SENT,
        func.date(Application.created_at) == func.current_date(),
    ]
    if task_id is not None:
        conds.append(Application.search_task_id == task_id)
    return (await session.execute(
        select(func.count(Application.id)).where(*conds)
    )).scalar() or 0


async def _mark_skip(vac_id: int, reason: str) -> None:
    """Пометить вакансию как пропущенную (для статистики) и убрать из будущих
    прогонов (REJECTED → дедуп её больше не берёт)."""
    async with async_session() as session:
        v = await session.get(Vacancy, vac_id)
        if v:
            v.skip_reason = reason
            v.status = VacancyStatus.REJECTED
            await session.commit()


async def _account_contexts(session, user) -> list[dict]:
    """Список аккаунтов для прогона: основной (User.hh_*) + активные доп."""
    from app.models.hh_account import HHAccount
    ctxs: list[dict] = []
    if user.hh_connected and user.hh_access_token:
        import json
        cookies = None
        if user.hh_cookies:
            try:
                cookies = json.loads(user.hh_cookies)
            except Exception:
                cookies = None
        ctxs.append({
            "ref": f"u{user.id}", "kind": "primary", "id": None,
            "access_token": user.hh_access_token,
            "refresh_token": user.hh_refresh_token or "",
            "resume_id": user.hh_resume_id,
            "expires_at": user.hh_token_expires.timestamp() if user.hh_token_expires else 0.0,
            "resume_text": user.resume_text or "",
            "cookies": cookies,
        })
    accs = (await session.execute(
        select(HHAccount).where(HHAccount.user_id == user.id, HHAccount.is_active.is_(True))
    )).scalars().all()
    for a in accs:
        if not a.hh_access_token:
            continue
        ctxs.append({
            "ref": f"a{a.id}", "kind": "extra", "id": a.id,
            "access_token": a.hh_access_token, "refresh_token": a.hh_refresh_token or "",
            "resume_id": a.hh_resume_id,
            "expires_at": a.hh_token_expires.timestamp() if a.hh_token_expires else 0.0,
            "resume_text": a.resume_text or "",
        })
    return ctxs


async def _save_refreshed_token(ctx: dict, new_token: dict) -> None:
    from datetime import timezone
    from app.models.hh_account import HHAccount
    expires = datetime.fromtimestamp(new_token["expires_at"], tz=timezone.utc)
    async with async_session() as session:
        if ctx["kind"] == "primary":
            u = await session.get(User, ctx["user_id"])
            if u:
                u.hh_access_token = new_token["access_token"]
                u.hh_refresh_token = new_token["refresh_token"]
                u.hh_token_expires = expires
                await session.commit()
        else:
            a = await session.get(HHAccount, ctx["id"])
            if a:
                a.hh_access_token = new_token["access_token"]
                a.hh_refresh_token = new_token["refresh_token"]
                a.hh_token_expires = expires
                await session.commit()


async def _refresh_negotiations(user_id: int, ctx: dict) -> None:
    """Воронка по задачам из hh /negotiations: приглашения и просмотры (всего/сегодня).
    Считаем по последним откликам, мапим вакансию → задачу, пишем в SearchTask."""
    from datetime import timezone, timedelta
    from app.models.search_task import SearchTask
    client = HHUserClient(
        access_token=ctx["access_token"], refresh_token=ctx["refresh_token"],
        resume_id=ctx["resume_id"], expires_at=ctx["expires_at"],
    )
    msk_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date().isoformat()
    by_vac: dict[str, dict] = {}
    for page in range(5):
        items, found, pages = await client.negotiations(100, page)
        if client.new_token:
            await _save_refreshed_token(ctx, client.new_token)
        if not items:
            break
        for it in items:
            vid = str((it.get("vacancy") or {}).get("id") or "")
            if not vid:
                continue
            state = (it.get("state") or {}).get("id")
            upd = (it.get("updated_at") or it.get("created_at") or "")[:10]
            # hh отдаёт приглашение как state.id="interview" (Собеседование).
            # "invitation" в API не встречается — из-за него приглашения
            # считались нулём. Оставлен для совместимости.
            by_vac[vid] = {"invited": state in ("interview", "invitation"),
                           "viewed": bool(it.get("viewed_by_opponent")),
                           "today": upd == msk_today}
        if page >= max(pages - 1, 0):
            break

    async with async_session() as session:
        vac_task: dict[str, int] = {}
        vac_variant: dict[str, str] = {}
        if by_vac:
            rows = (await session.execute(
                select(Vacancy.external_id, Vacancy.search_task_id).where(
                    Vacancy.user_id == user_id, Vacancy.platform == "hh",
                    Vacancy.external_id.in_(list(by_vac.keys())))
            )).all()
            for ext, tid in rows:
                if tid and ext not in vac_task:
                    vac_task[ext] = tid
            # Вариант письма (A/B) для каждой вакансии — через Application.
            vrows = (await session.execute(
                select(Vacancy.external_id, Application.letter_variant)
                .join(Application, Application.vacancy_id == Vacancy.id)
                .where(Vacancy.user_id == user_id, Vacancy.platform == "hh",
                       Vacancy.external_id.in_(list(by_vac.keys())),
                       Application.letter_variant.is_not(None))
            )).all()
            for ext, var in vrows:
                if var and ext not in vac_variant:
                    vac_variant[ext] = var
        agg: dict[int, dict] = {}
        for vid, d in by_vac.items():
            tid = vac_task.get(vid)
            if not tid:
                continue
            a = agg.setdefault(tid, {"inv": 0, "inv_t": 0, "vw": 0, "vw_t": 0, "ab_a": 0, "ab_b": 0})
            if d["invited"]:
                a["inv"] += 1
                a["inv_t"] += 1 if d["today"] else 0
                var = vac_variant.get(vid)
                if var == "A":
                    a["ab_a"] += 1
                elif var == "B":
                    a["ab_b"] += 1
            if d["viewed"]:
                a["vw"] += 1
                a["vw_t"] += 1 if d["today"] else 0
        tasks = (await session.execute(
            select(SearchTask).where(SearchTask.user_id == user_id))).scalars().all()
        for t in tasks:
            a = agg.get(t.id)
            t.invites = a["inv"] if a else 0
            t.invites_today = a["inv_t"] if a else 0
            t.views = a["vw"] if a else 0
            t.views_today = a["vw_t"] if a else 0
            t.ab_inv_a = a["ab_a"] if a else 0
            t.ab_inv_b = a["ab_b"] if a else 0
        await session.commit()


async def run_user_cycle(user_id: int) -> int:
    """Цикл автоотклика пользователя по всем его аккаунтам. Число новых откликов."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user or not user.is_active:
            return 0
        st_global = user.get_settings()
        # Задачи из активных SearchTask (ключ + своё резюме + свои настройки).
        # Fallback — старые ключевые фразы на общих настройках пользователя.
        from app.services.search_tasks import ensure_seeded, active_tasks
        await ensure_seeded(session, user)
        task_objs = await active_tasks(session, user.id)
        if task_objs:
            tasks = [{"keyword": t.keyword, "resume_id": t.resume_id,
                      "resume_text": t.resume_text, "task_id": t.id,
                      "settings": (t.get_settings() if t.settings_json else st_global)}
                     for t in task_objs]
        else:
            tasks = [{"keyword": p, "resume_id": None, "resume_text": None,
                      "task_id": None, "settings": st_global}
                     for p in st_global.search_phrases()]
        # Без ключевых слов hh вернёт всё подряд — не откликаемся во избежание спама.
        if not tasks:
            log.info("user_no_keywords_skip", user_id=user.id)
            return 0
        contexts = await _account_contexts(session, user)
        # Письма: платным — основная модель, бесплатным — подешевле.
        letter_model = None if user.is_paid else (settings.ai_letter_model_free or None)

    total = 0
    for ctx in contexts:
        ctx["user_id"] = user_id
        ctx["letter_model"] = letter_model
        try:
            total += await run_account_cycle(user_id, ctx, tasks)
        except Exception as e:
            log.error("account_cycle_error", user_id=user_id, ref=ctx["ref"], error=str(e))
    # Воронка (приглашения/просмотры) по основному аккаунту — раз за цикл.
    if contexts:
        try:
            await _refresh_negotiations(user_id, contexts[0])
        except Exception as e:
            log.warning("negotiations_refresh_failed", user_id=user_id, error=str(e))
    log.info("user_cycle_done", user_id=user_id, applied=total, accounts=len(contexts))
    if any(c.get("ai_down") for c in contexts):
        await _notify_ai_down(user_id)
    for c in contexts:
        if c.get("captcha_task") is not None:
            await _notify_captcha(user_id, c.get("captcha_task") or "")
            break
    if any(c.get("token_revoked") for c in contexts):
        await _notify_token_revoked(user_id)
    return total


async def _notify_token_revoked(user_id: int) -> None:
    """Доступ к hh отозван — без переподключения бот не работает совсем.

    Раньше это была только строчка в логе: человек думал, что бот ищет
    вакансии, а он не отправлял ничего.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user or not user.telegram_id:
            return
        if user.hh_connected:          # снимаем флаг: аккаунт больше не рабочий
            user.hh_connected = False
            await session.commit()
    await _send_tg(
        user.telegram_id,
        "🔌 <b>Доступ к hh.ru отключён</b>\n\n"
        "hh больше не принимает наш доступ к твоему аккаунту — так бывает "
        "после смены пароля или выхода из всех устройств.\n\n"
        "Отклики сейчас не отправляются. Чтобы продолжить, подключи аккаунт "
        "заново — это минута и пароль не нужен 👇",
        buttons=[[{"text": "🔗 Подключить заново", "callback_data": "connect:start"}]],
    )


async def _send_tg(chat_id, text: str, buttons: list | None = None) -> None:
    """Отправить сообщение в Telegram напрямую (воркер живёт вне aiogram)."""
    import httpx
    token = settings.tg_bot_token
    if not token:
        return
    payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
    except Exception as e:
        log.warning("tg_send_failed", chat_id=chat_id, error=str(e))


async def _notify_captcha(user_id: int, task_name: str) -> None:
    """hh просит капчу — просим пользователя пройти её на сайте.

    Решать капчу за него нельзя: это обход антибот-защиты, и при обнаружении
    банят его аккаунт, а не бота. Честнее остановиться и попросить минуту
    внимания — задача продолжится сама на следующем цикле.
    """
    async with async_session() as session:
        user = await session.get(User, user_id)
    if not user or not user.telegram_id:
        return
    where = f" «{task_name}»" if task_name else ""
    text = (f"🤖 <b>hh просит подтвердить, что ты не робот</b>\n\n"
            f"Задача{where} поставлена на паузу — пройти проверку за тебя я не могу: "
            f"это защита hh, и обход приведёт к блокировке твоего аккаунта.\n\n"
            f"Что делать: зайди на <b>hh.ru</b> с телефона или компьютера, "
            f"открой любую вакансию и пройди проверку — это займёт секунд десять.\n\n"
            f"Дальше бот продолжит сам, ничего нажимать не нужно.")
    await _send_tg(user.telegram_id, text)


async def _notify_ai_down(user_id: int) -> None:
    """ИИ недоступен — сообщаем ТОЛЬКО владельцу бота.

    Пользователя не дёргаем: пауза обычно короткая, а сообщение о сбое
    тревожит сильнее, чем сама задержка. Отклики уже остановлены
    (fail-closed) — лучше не отправить ничего, чем откликаться без отбора.
    """
    admin = str(settings.tg_admin_chat_id or "")
    if not admin:
        return
    await _send_tg(admin, f"🔴 ИИ недоступен, отклики остановлены (user_id={user_id}). "
                          f"Проверь баланс провайдера.")


async def run_account_cycle(user_id: int, ctx: dict, tasks: list[dict]) -> int:
    """Один цикл автоотклика для одного hh-аккаунта.

    Каждая задача (tasks[i]) идёт со своими настройками (settings), ключом,
    резюме и дневным лимитом — свой поиск, свой отбор, своё окно расписания.
    """
    ref = ctx["ref"]
    account_resume_id = ctx["resume_id"]
    account_resume_text = ctx["resume_text"]
    total_applied = 0

    client = HHUserClient(
        access_token=ctx["access_token"], refresh_token=ctx["refresh_token"],
        resume_id=ctx["resume_id"], expires_at=ctx["expires_at"],
    )
    for task in tasks:
        st = task["settings"]
        task_id = task.get("task_id")
        # Окно расписания — своё у задачи.
        if not _within_window(st.apply_hour_start, st.apply_hour_end):
            continue
        async with async_session() as session:
            remaining = st.daily_limit - await _sent_today(session, user_id, ref, task_id)
        if remaining <= 0:
            continue
        phrase = task.get("keyword") or ""
        resume_id = task.get("resume_id") or account_resume_id
        resume_text = task.get("resume_text") or account_resume_text
        client.resume_id = resume_id  # переключаем резюме под задачу
        base_params = st.to_hh_params()
        excluded = st.excluded_words()
        applied = 0
        seen = 0
        scored = 0  # бюджет ИИ-оценок — свой на каждую задачу (иначе 1-я съедает всё)
        score_fails = 0  # подряд идущие сбои ИИ-оценки
        stop = False
        # Источник вакансий: ключ задачи и/или лента рекомендаций hh под резюме.
        src_mode = getattr(st, "vacancy_source", "keyword")
        sources: list[str] = []
        if src_mode in ("keyword", "both") and phrase:
            sources.append("keyword")
        if src_mode in ("recommended", "both") and resume_id:
            sources.append("recommended")
        if not sources:  # нет ключа или нет резюме — берём что доступно
            sources = ["keyword"] if phrase else (["recommended"] if resume_id else [])
        exhausted: set[str] = set()
        for source, page in [(s, p) for s in sources for p in range(MAX_SEARCH_PAGES)]:
            if stop or applied >= remaining:
                break
            if source in exhausted:
                continue
            if source == "keyword":
                params = dict(base_params)
                params["text"] = phrase
                items = await client.search(params, per_page=50, page=page)
            else:
                items = await client.similar_vacancies(resume_id, per_page=50, page=page)
            if client.new_token:
                await _save_refreshed_token(ctx, client.new_token)
            if not items:
                exhausted.add(source)  # лента кончилась — не долбим пустые страницы
                continue
            for item in items:
                if applied >= remaining:
                    break
                seen += 1
                vid = str(item.get("id") or "")
                if not vid:
                    continue
                title = item.get("name") or ""
                url = (item.get("alternate_url") or f"https://hh.ru/vacancy/{vid}")

                # hh сам помечает вакансии, где уже был отклик/отказ (relations) —
                # не тратим на них отклик (особенно важно для ленты рекомендаций).
                if item.get("relations"):
                    continue

                # Слова-исключения: отсеиваем по названию + сниппету (на своей стороне).
                if excluded:
                    snip = item.get("snippet") or {}
                    blob = " ".join(str(x) for x in (
                        title, snip.get("responsibility"), snip.get("requirement")) if x).lower()
                    if any(w in blob for w in excluded):
                        continue

                async with async_session() as session:
                    # Дедуп в рамках этого аккаунта.
                    vac = (await session.execute(
                        select(Vacancy).where(
                            Vacancy.user_id == user_id, Vacancy.platform == "hh",
                            Vacancy.external_id == vid, Vacancy.account_ref == ref,
                        )
                    )).scalar_one_or_none()
                    if vac and vac.status in (VacancyStatus.APPLIED, VacancyStatus.REJECTED):
                        continue
                    if vac is None:
                        vac = Vacancy(
                            user_id=user_id, platform="hh", external_id=vid, account_ref=ref,
                            url=url, title=title, status=VacancyStatus.NEW,
                            search_task_id=task_id,
                        )
                        session.add(vac)
                        await session.commit()
                        await session.refresh(vac)
                    vac_id = vac.id

                # Умный отбор: ИИ оценивает соответствие вакансии резюме и отсекает слабые.
                if getattr(st, "ai_score_enabled", False) and resume_text:
                    if scored >= MAX_SCORINGS_PER_CYCLE:
                        log.info("user_score_budget_reached", user_id=user_id, ref=ref, scored=scored)
                        stop = True
                        break
                    snip = item.get("snippet") or {}
                    desc = " ".join(x for x in (snip.get("responsibility"), snip.get("requirement")) if x)
                    scored += 1
                    score = await claude_ai.score_vacancy(title, desc, resume_text)
                    # Fail-closed: пользователь включил умный отбор — значит откликаться
                    # без оценки нельзя. Иначе при сбое ИИ (кончился баланс, упал
                    # провайдер) бот выжжет дневной лимит hh на нерелевантных вакансиях.
                    if score is None:
                        score_fails += 1
                        log.warning("user_score_unavailable", user_id=user_id, vid=vid,
                                    fails=score_fails)
                        if score_fails >= MAX_SCORE_FAILS:
                            log.error("user_score_ai_down", user_id=user_id, ref=ref)
                            ctx["ai_down"] = True
                            stop = True
                            break
                        continue
                    score_fails = 0
                    async with async_session() as session:
                        v = await session.get(Vacancy, vac_id)
                        if v:
                            v.ai_score = float(score)
                            if score < st.ai_score_min:
                                v.status = VacancyStatus.REJECTED
                                v.skip_reason = "ai_low"
                                v.ai_reason = f"Умный отбор: {score}% < порога {st.ai_score_min}%"
                            await session.commit()
                    if score < st.ai_score_min:
                        log.info("user_vacancy_skipped_low_score", user_id=user_id, vid=vid, score=score)
                        continue

                letter, letter_variant = await _build_letter(
                    item, title, st, resume_text, ab_index=applied,
                    model=ctx.get("letter_model"))
                try:
                    if item.get("has_test"):
                        # Вакансия с тестом: обычный API-отклик не пройдёт.
                        cookies_state = ctx.get("cookies")
                        if cookies_state and st.ai_enabled and resume_text:
                            result, info = await client.apply_with_test(vid, letter, cookies_state)
                            if result is not True:
                                log.info("test_apply_skip", user_id=user_id, vid=vid, info=info)
                                await _mark_skip(vac_id, "needs_test")
                                continue
                        else:
                            # Нет веб-сессии/ИИ — не тратим на тест-вакансию.
                            await _mark_skip(vac_id, "needs_test")
                            continue
                    else:
                        result, info = await client.apply(vid, letter)
                        # hh троттлит (429) — ждём и повторяем, вакансия не теряется.
                        for attempt in range(RATE_LIMIT_RETRIES):
                            if info.get("error") != "rate_limited":
                                break
                            pause = RATE_LIMIT_PAUSE_SEC * (attempt + 1)
                            log.warning("user_rate_limited", user_id=user_id, vid=vid,
                                        attempt=attempt + 1, pause=pause,
                                        body=info.get("body", ""))
                            await asyncio.sleep(pause)
                            result, info = await client.apply(vid, letter)
                except Exception as e:
                    log.error("user_apply_error", user_id=user_id, vid=vid, error=str(e))
                    continue

                if info.get("error") == "captcha_required":
                    log.warning("user_captcha_required", user_id=user_id, ref=ref,
                                task_id=task_id)
                    ctx["captcha_task"] = task.get("keyword") or ""
                    stop = True
                    break

                if info.get("error") == "rate_limited":
                    # Не сдался и после повторов — притормаживаем весь аккаунт.
                    log.warning("user_rate_limit_giveup", user_id=user_id, ref=ref)
                    stop = True
                    break

                if info.get("error") == "daily_limit":
                    log.info("user_daily_limit_hit", user_id=user_id, ref=ref)
                    stop = True
                    break

                success = result is True
                already = result == "already"

                async with async_session() as session:
                    if not already:
                        session.add(Application(
                            user_id=user_id, vacancy_id=vac_id, platform="hh",
                            cover_letter=letter, account_ref=ref, search_task_id=task_id,
                            letter_variant=letter_variant,
                            status=ApplicationStatus.SENT if success else ApplicationStatus.FAILED,
                            attempt_count=1,
                        ))
                    v = await session.get(Vacancy, vac_id)
                    if v and (success or already):
                        v.status = VacancyStatus.APPLIED
                        if already:
                            v.skip_reason = "already"
                    await session.commit()

                if success:
                    applied += 1
                await random_delay(st.apply_delay_min, st.apply_delay_max)
            if stop:
                break
        if client.token_revoked:
            ctx["token_revoked"] = True
        log.info("account_task_done", user_id=user_id, ref=ref, task_id=task_id,
                 phrase=phrase, applied=applied, seen=seen)
        # Кэш для карточки задачи: сколько подобрал источник + время прогона.
        if task_id:
            from datetime import timezone
            from app.models.search_task import SearchTask
            async with async_session() as session:
                t = await session.get(SearchTask, task_id)
                if t:
                    if client.last_found is not None:
                        t.rec_found = int(client.last_found)
                    t.last_run_at = datetime.now(timezone.utc).isoformat(timespec="minutes")
                    await session.commit()
        total_applied += applied

    log.info("account_cycle_done", user_id=user_id, ref=ref, applied=total_applied)
    return total_applied
