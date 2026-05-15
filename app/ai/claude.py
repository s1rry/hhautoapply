import json

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


class ClaudeAI:
    def __init__(self):
        kwargs = {"api_key": settings.anthropic_api_key}
        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def _call(self, system: str, user_message: str, max_tokens: int = 1024) -> tuple[str, int, int]:
        response = await self.client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text
        return text, response.usage.input_tokens, response.usage.output_tokens

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

    async def generate_reply(self, recruiter_message: str, vacancy_context: str = "") -> tuple[str, int, int]:
        system = SYSTEM_REPLY_GENERATOR.format(
            resume=settings.resume_text,
            salary_min=settings.desired_salary_min,
            salary_max=settings.desired_salary_max,
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
