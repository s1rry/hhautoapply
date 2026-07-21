import json
import re
from pathlib import Path

import httpx
import structlog

try:
    import anthropic
except ImportError:  # self-host на groq может не ставить anthropic
    anthropic = None

from app.config import settings
from app.ai.prompts import (
    SYSTEM_VACANCY_ANALYZER,
    SYSTEM_COVER_LETTER,
    SYSTEM_REPLY_GENERATOR,
    SYSTEM_SENTIMENT_ANALYZER,
)

log = structlog.get_logger()

MODEL = "claude-sonnet-4-6"
# Модель для прохождения тестов вакансий (ответы на вопросы/тесты работодателя)
TEST_MODEL = "claude-haiku-4-5"
AI_STATE_FILE = Path("data/ai_state.json")

# Сколько эндпоинт пула «отдыхает» после ошибки, прежде чем пробовать снова.
# Лимиты бесплатных тиров минутные, так что перепроверять есть смысл.
POOL_COOLDOWN_SEC = 600


class ClaudeAI:
    def __init__(self):
        self.primary = None
        self.fallback = None
        # Anthropic-клиенты создаём только если выбран этот провайдер.
        if settings.ai_provider == "anthropic" and anthropic is not None:
            primary_kwargs = {"api_key": settings.anthropic_api_key}
            if settings.anthropic_base_url:
                primary_kwargs["base_url"] = settings.anthropic_base_url
            self.primary = anthropic.AsyncAnthropic(**primary_kwargs)

            if settings.anthropic_fallback_api_key:
                fb_kwargs = {"api_key": settings.anthropic_fallback_api_key}
                if settings.anthropic_fallback_base_url:
                    fb_kwargs["base_url"] = settings.anthropic_fallback_base_url
                self.fallback = anthropic.AsyncAnthropic(**fb_kwargs)

        # Persistent flag — once primary is exhausted we stick to fallback
        self.use_fallback = self._load_use_fallback()
        # Пул бесплатных эндпоинтов для скоринга + указатель круговой ротации.
        self.score_pool = self._parse_score_pool(settings.ai_score_pool)
        self._pool_idx = 0
        self._pool_alert_at = 0.0  # когда последний раз предупреждали админа
        self._cooldown: dict[str, float] = {}  # base_url -> до какого времени пропускать

    @staticmethod
    def _parse_score_pool(raw: str) -> list[dict]:
        """Разобрать AI_SCORE_POOL: "url|key|model" через ';'.

        Пустые и кривые записи молча пропускаем: неверная строка в конфиге не
        должна ронять бота — он просто отработает на платном провайдере.
        """
        pool: list[dict] = []
        for chunk in (raw or "").split(";"):
            parts = [p.strip() for p in chunk.split("|")]
            if len(parts) >= 3 and all(parts[:3]):
                pool.append({"base_url": parts[0].rstrip("/"),
                             "api_key": parts[1], "model": parts[2]})
        return pool

    def _load_use_fallback(self) -> bool:
        try:
            if AI_STATE_FILE.exists():
                return bool(json.loads(AI_STATE_FILE.read_text()).get("use_fallback", False))
        except Exception:
            pass
        return False

    def _save_use_fallback(self):
        try:
            AI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            AI_STATE_FILE.write_text(json.dumps({"use_fallback": self.use_fallback}))
        except Exception as e:
            log.warning("ai_state_save_error", error=str(e))

    def reset_fallback(self):
        """Manual reset — go back to primary after topping it up."""
        self.use_fallback = False
        self._save_use_fallback()
        log.info("ai_fallback_reset")

    def _extract_text(self, response) -> str:
        """Pull text from response.content[], skipping empty/thinking blocks.
        TonWave with low max_tokens can return content=[] if all budget
        was spent on internal thinking — we'd previously crash with IndexError.
        """
        try:
            blocks = list(response.content or [])
        except Exception:
            blocks = []
        for b in blocks:
            text = getattr(b, "text", None)
            if text:
                return text
        return ""

    async def _call_openai_compatible(self, system: str, user_message: str, max_tokens: int,
                                      model: str | None = None,
                                      base_url: str | None = None,
                                      api_key: str | None = None,
                                      temperature: float | None = None) -> tuple[str, int, int]:
        """Вызов любого OpenAI-совместимого эндпоинта (OpenRouter/Cerebras/Mistral/…)."""
        headers = {
            "Authorization": f"Bearer {api_key or settings.ai_api_key}",
            "Content-Type": "application/json",
            # Браузерный UA — иначе Cloudflare-релеи (tonwave) отвечают 1010.
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        }
        payload = {
            "model": model or settings.ai_model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        # Прокси: явный ai_proxy, иначе tg_proxy (тот же SOCKS, что для Telegram).
        proxy = settings.ai_proxy or (settings.tg_proxy if (settings.tg_proxy or "").startswith("socks5") else "")
        async with httpx.AsyncClient(timeout=60, proxy=proxy or None) as c:
            r = await c.post(f"{base_url or settings.ai_base_url}/chat/completions",
                             headers=headers, json=payload)
        r.raise_for_status()
        d = r.json()
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        usage = d.get("usage") or {}
        return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

    async def _call(self, system: str, user_message: str, max_tokens: int = 1024, model: str | None = None,
                    temperature: float | None = None) -> tuple[str, int, int]:
        # Minimum sane budget — small max_tokens makes models return empty content
        if max_tokens < 800:
            max_tokens = 800

        # OpenAI-совместимый провайдер (по умолчанию для облака/self-host).
        if settings.ai_provider != "anthropic":
            return await self._call_openai_compatible(system, user_message, max_tokens, model,
                                                      temperature=temperature)

        # Permanent fallback: if primary was exhausted before, go straight to fallback.
        if self.use_fallback and self.fallback:
            response = await self.fallback.messages.create(
                model=model or MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = self._extract_text(response)
            return text, response.usage.input_tokens, response.usage.output_tokens

        try:
            response = await self.primary.messages.create(
                model=model or MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = self._extract_text(response)
            return text, response.usage.input_tokens, response.usage.output_tokens
        except Exception as e:
            err_str = str(e)
            is_quota = "insufficient_quota" in err_str or "billing" in err_str.lower() or "402" in err_str
            if is_quota and self.fallback:
                log.warning("ai_quota_exhausted_switching_permanently")
                self.use_fallback = True
                self._save_use_fallback()
                response = await self.fallback.messages.create(
                    model=model or MODEL, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                text = self._extract_text(response)
                return text, response.usage.input_tokens, response.usage.output_tokens
            raise

    async def analyze_vacancy(self, vacancy_title: str, vacancy_description: str, skills: str = "") -> dict:
        system = SYSTEM_VACANCY_ANALYZER.format(
            resume=settings.resume_text,
            salary_min=settings.desired_salary_min,
            salary_max=settings.desired_salary_max,
        )
        user_msg = f"""Вакансия: {vacancy_title}

Описание:
{vacancy_description}

Навыки: {skills}"""

        text, inp_tok, out_tok = await self._call(system, user_msg)
        log.info("ai_vacancy_analyzed", title=vacancy_title[:60], tokens=inp_tok + out_tok)

        try:
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(clean)
            result["_input_tokens"] = inp_tok
            result["_output_tokens"] = out_tok
            return result
        except json.JSONDecodeError:
            log.error("ai_json_parse_error", raw=text[:200])
            return {
                "score": 0,
                "reason": "Ошибка парсинга ответа AI",
                "is_relevant": False,
                "seniority": "unknown",
                "red_flags": [],
                "stack_match": 0,
                "_input_tokens": inp_tok,
                "_output_tokens": out_tok,
            }

    @staticmethod
    def _resume_for_letter(resume: str, limit: int = 3500) -> str:
        """Урезать резюме для письма: резюме уходит в КАЖДОЕ письмо целиком и
        это основная статья входных токенов. Для сопроводительного важны
        проекты и стек (в начале), а образование и софт-скилы (в хвосте) модель
        всё равно не цитирует. Режем по границе строки, чтобы не рвать слова.
        """
        if len(resume) <= limit:
            return resume
        cut = resume.rfind("\n", 0, limit)
        return resume[: cut if cut > limit * 0.6 else limit].rstrip()

    async def generate_cover_letter(self, vacancy_title: str, vacancy_description: str, company_name: str = "", resume: str | None = None, custom_prompt: str | None = None, model: str | None = None) -> tuple[str, int, int]:
        resume_text = self._resume_for_letter(resume or settings.resume_text)
        if custom_prompt:
            system = f"{custom_prompt}\n\nПрофиль кандидата:\n{resume_text}"
        else:
            system = SYSTEM_COVER_LETTER.format(resume=resume_text)
        user_msg = f"""Напиши сопроводительное письмо для вакансии:

Компания: {company_name}
Позиция: {vacancy_title}
Описание:
{vacancy_description}"""

        # Умеренно повышенная температура: письма к разным вакансиям должны
        # отличаться, но 0.9 давал грамматические сбои (сбитые местоимения).
        # Разнообразие в основном обеспечивает промпт («начинай по-разному»).
        text, inp_tok, out_tok = await self._call(system, user_msg, max_tokens=400,
                                                  model=model, temperature=0.7)
        log.info("ai_cover_letter_generated", title=vacancy_title[:60], model=model or "default")
        return self._humanize(text.strip()), inp_tok, out_tok

    @staticmethod
    def _humanize(text: str) -> str:
        """Убрать явные маркеры ИИ, даже если модель проигнорировала промпт.

        Длинное тире и стрелки — то, по чему HR отличает автоотклик. Промпт их
        запрещает, но подстраховываемся: заменяем гарантированно.
        """
        # Стрелки (с пробелами вокруг) и тире между словами → запятая.
        text = re.sub(r"\s*(→|->|⟶)\s*", ", ", text)
        text = re.sub(r"\s+[—–]\s+", ", ", text)
        text = text.replace("—", "-").replace("–", "-")
        # Схлопнуть артефакты замен: пробел перед запятой, двойные запятые/пробелы.
        text = re.sub(r"\s+,", ",", text)
        text = re.sub(r"(,\s*){2,}", ", ", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text

    async def complete(self, prompt: str, max_tokens: int = 300) -> str:
        """Простой запрос к ИИ (для ответов на тесты вакансий и т.п.)."""
        try:
            text, _, _ = await self._call(
                "Ты помощник кандидата. Отвечай кратко, по делу, без воды.",
                prompt, max_tokens=max_tokens,
            )
            return (text or "").strip()
        except Exception as e:
            log.warning("ai_complete_failed", error=str(e))
            return ""

    async def _alert_admin_pool_down(self, errors: list[str]) -> None:
        """Все бесплатные ключи скоринга не отвечают — сказать владельцу.

        Не чаще раза в час: скоринг вызывается тысячи раз, иначе завалим чат.
        Бот при этом продолжает работать на платном провайдере — это
        предупреждение о растущих расходах, а не об аварии.
        """
        import time as _t
        if _t.time() - self._pool_alert_at < 3600:
            return
        self._pool_alert_at = _t.time()
        token = settings.tg_bot_token
        admin = str(settings.tg_admin_chat_id or "")
        if not token or not admin:
            return
        # В сообщение идут только хосты — ключи не светим даже в своём чате.
        text = ("🔑 <b>Бесплатные ключи скоринга не отвечают</b>\n\n"
                + "\n".join(f"• {e}" for e in errors[:5])
                + "\n\nСкоринг ушёл на платного провайдера — бот работает, "
                  "но тратит деньги. Проверь лимиты или замени ключи "
                  "в AI_SCORE_POOL.")
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": admin, "text": text, "parse_mode": "HTML"})
        except Exception as e:
            log.warning("pool_alert_failed", error=str(e))

    async def _score_call(self, system: str, user_msg: str) -> str | None:
        """Скоринг по пулу: каждый вызов начинает со следующего ключа.

        Ротация со сдвигом, а не всегда с первого, — иначе первый ключ выбирал
        бы весь минутный лимит, а остальные простаивали. Все бесплатные легли —
        уходим на основного платного, чтобы отклики не встали.
        """
        import time as _t
        now = _t.time()
        errors = []
        for i in range(len(self.score_pool)):
            ep = self.score_pool[(self._pool_idx + i) % len(self.score_pool)]
            # Упавший эндпоинт отдыхает: иначе каждый скоринг сначала ждёт
            # таймаута от мёртвого ключа и только потом идёт к живому.
            if self._cooldown.get(ep["base_url"], 0) > now:
                continue
            try:
                text, _, _ = await self._call_openai_compatible(
                    system, user_msg, max_tokens=800,
                    model=ep["model"], base_url=ep["base_url"], api_key=ep["api_key"])
                self._pool_idx = (self._pool_idx + i + 1) % len(self.score_pool)
                self._cooldown.pop(ep["base_url"], None)
                return text
            except Exception as e:
                self._cooldown[ep["base_url"]] = now + POOL_COOLDOWN_SEC
                errors.append(f"{ep['base_url']}: {type(e).__name__}")
        if errors:
            log.warning("ai_score_pool_exhausted", tried=errors)
            await self._alert_admin_pool_down(errors)
        try:
            text, _, _ = await self._call(system, user_msg, max_tokens=800,
                                          model=(settings.ai_score_model or None))
            return text
        except Exception as e:
            log.warning("ai_score_failed", error=str(e))
            return None

    async def score_vacancy(self, vacancy_title: str, vacancy_description: str, resume: str) -> int | None:
        """Оценка соответствия вакансии резюме, 0–100. None — ИИ недоступен/не
        дал число (вызывающий решает: при строгом отборе такую вакансию лучше
        пропустить, а не откликаться вслепую)."""
        system = (
            "Ты помощник по поиску работы. Оцени, насколько вакансия подходит "
            "кандидату по его резюме. Учитывай профессию и роль: например для "
            "бизнес/системного аналитика вакансии химика-аналитика, "
            "бухгалтера-аналитика, лаборанта — НЕ подходят. Верни ТОЛЬКО число "
            "от 0 до 100 — процент соответствия. Без слов, без пояснений.\n\n"
            # Вход урезан: платим в основном за входные токены, а для оценки
            # достаточно должности + ключевых навыков, а не всего резюме.
            f"Резюме кандидата:\n{resume[:1500]}"
        )
        user_msg = f"Вакансия: {vacancy_title}\n\nОписание:\n{(vacancy_description or '')[:800]}"
        text = await self._score_call(system, user_msg)
        if text is None:
            return None
        m = re.search(r"\d{1,3}", text or "")
        if not m:
            log.warning("ai_score_no_number", raw=(text or "")[:80])
            return None
        return max(0, min(100, int(m.group(0))))

    async def generate_reply(self, recruiter_message: str, vacancy_context: str = "", platform: str = "") -> tuple[str, int, int]:
        platform_name = {"hh": "hh.ru", "habr": "Хабр Карьера", "avito": "Авито"}.get(platform, platform or "сайт вакансий")
        system = SYSTEM_REPLY_GENERATOR.format(
            resume=settings.resume_text,
            salary_min=settings.desired_salary_min,
            salary_max=settings.desired_salary_max,
            platform=platform_name,
        )
        user_msg = f"""Сообщение рекрутера:
{recruiter_message}

Контекст вакансии:
{vacancy_context}"""

        text, inp_tok, out_tok = await self._call(system, user_msg, max_tokens=512)
        log.info("ai_reply_generated")
        return text.strip(), inp_tok, out_tok

    async def analyze_sentiment(self, message: str) -> dict:
        text, _, _ = await self._call(SYSTEM_SENTIMENT_ANALYZER, message, max_tokens=256)
        try:
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(clean)
        except json.JSONDecodeError:
            return {"sentiment": "neutral", "intent": "info", "urgency": "low", "summary": message[:100]}


claude_ai = ClaudeAI()
