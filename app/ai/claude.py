import json
from pathlib import Path

import anthropic
import structlog

from app.config import settings
from app.ai.prompts import (
    SYSTEM_VACANCY_ANALYZER,
    SYSTEM_COVER_LETTER,
    SYSTEM_REPLY_GENERATOR,
    SYSTEM_SENTIMENT_ANALYZER,
)

log = structlog.get_logger()

MODEL = "claude-sonnet-4-6"
AI_STATE_FILE = Path("data/ai_state.json")


class ClaudeAI:
    def __init__(self):
        primary_kwargs = {"api_key": settings.anthropic_api_key}
        if settings.anthropic_base_url:
            primary_kwargs["base_url"] = settings.anthropic_base_url
        self.primary = anthropic.AsyncAnthropic(**primary_kwargs)

        self.fallback = None
        if settings.anthropic_fallback_api_key:
            fb_kwargs = {"api_key": settings.anthropic_fallback_api_key}
            if settings.anthropic_fallback_base_url:
                fb_kwargs["base_url"] = settings.anthropic_fallback_base_url
            self.fallback = anthropic.AsyncAnthropic(**fb_kwargs)

        # Persistent flag — once primary is exhausted we stick to fallback
        self.use_fallback = self._load_use_fallback()

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

    async def _call(self, system: str, user_message: str, max_tokens: int = 1024) -> tuple[str, int, int]:
        # Permanent fallback: if primary was exhausted before, go straight to fallback.
        if self.use_fallback and self.fallback:
            response = await self.fallback.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text
            return text, response.usage.input_tokens, response.usage.output_tokens

        try:
            response = await self.primary.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text
            return text, response.usage.input_tokens, response.usage.output_tokens
        except Exception as e:
            err_str = str(e)
            is_quota = "insufficient_quota" in err_str or "billing" in err_str.lower() or "402" in err_str
            if is_quota and self.fallback:
                log.warning("ai_quota_exhausted_switching_permanently")
                self.use_fallback = True
                self._save_use_fallback()
                response = await self.fallback.messages.create(
                    model=MODEL, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                text = response.content[0].text
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

    async def generate_cover_letter(self, vacancy_title: str, vacancy_description: str, company_name: str = "") -> tuple[str, int, int]:
        system = SYSTEM_COVER_LETTER.format(resume=settings.resume_text)
        user_msg = f"""Напиши сопроводительное письмо для вакансии:

Компания: {company_name}
Позиция: {vacancy_title}
Описание:
{vacancy_description}"""

        text, inp_tok, out_tok = await self._call(system, user_msg, max_tokens=512)
        log.info("ai_cover_letter_generated", title=vacancy_title[:60])
        return text.strip(), inp_tok, out_tok

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
