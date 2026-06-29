"""
Rule-based vacancy analyzer. Doesn't spend AI tokens.

Score is built from explicit signals: title match, stack match, salary,
remote, level. Negative keywords zero the score.

Approved if score >= 60 AND title matches.
"""
from __future__ import annotations

import json
import re

# Must-have title patterns (any one match is enough)
TITLE_POSITIVES = [
    r"\bаналит(ик|ика)\b",
    r"\bba\b",
    r"\bsa\b",
    r"business.?analyst",
    r"system.?analyst",
    r"бизнес.?аналит",
    r"системн.?аналит",
    r"продукт.?аналит",
    r"product.?analyst",
    r"фулстек.?аналит",
]

# Минимальная зарплата: вакансии с указанной ЗП ниже отсеиваем.
# Без указанной ЗП — откликаемся (многие не ставят).
SALARY_FLOOR = 120000

# Бренды-исключения — не откликаемся на их вакансии (по запросу пользователя)
BRAND_NEGATIVES = [
    r"wildberries", r"вайлдберриз", r"вайлдбериз", r"\bwb\b",
    r"\bozon\b", r"озон",
]

# Hard negatives — instant disqualify
HARD_NEGATIVES = [
    r"\b1с\b",  # 1С аналитики — отдельная вселенная
    r"\b1c\b",
    r"backend\s+(developer|разработчик)",
    r"frontend\s+(developer|разработчик)",
    r"data\s+engineer",
    r"ml.?engineer",
    r"devops",
    r"qa\b",
    r"тестировщик",
    r"стажёр",
    r"стажер",
    r"ученик",
    r"intern\b",
    r"\bjunior\b",  # only junior
]

# Stack keywords — each gives +5 (capped at +40)
STACK_KEYWORDS = [
    "rest api", "rest", "api", "bpmn", "uml",
    "sql", "postgres", "postgresql", "mysql",
    "erp", "crm", "интеграц", "микросервис",
    "agile", "scrum", "kanban",
    "swagger", "openapi",
    "use case", "user story", "user stories",
    "jira", "confluence",
    "postman", "soap",
    "android", "ios", "мобильн",
    "figma",
]

# Seniority signals
LEVEL_MIDDLE = [r"\bmiddle\b", r"\bmid\b", r"\bсредн", r"\b\+\s*2\s*лет", r"опыт.{0,10}3"]
LEVEL_SENIOR = [r"\bsenior\b", r"\bстарш", r"\bлид\b", r"\blead\b"]


def _match_any(text: str, patterns: list[str]) -> bool:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def analyze_vacancy(
    title: str,
    description: str = "",
    skills: str = "",
    salary_from: int | None = None,
    salary_to: int | None = None,
    is_remote: bool = False,
    salary_currency: str = "",
    desired_salary_min: int = 200000,
    desired_salary_max: int = 400000,
) -> dict:
    """Return same shape as claude_ai.analyze_vacancy but without AI calls."""
    t = (title or "").lower()
    d = (description or "").lower()
    s_skills = (skills or "").lower()
    full = f"{t}\n{d}\n{s_skills}"

    red_flags: list[str] = []

    # 1. Title must match
    title_match = _match_any(t, TITLE_POSITIVES)
    if not title_match:
        return {
            "score": 0,
            "reason": "Title doesn't match analyst positions",
            "is_relevant": False,
            "seniority": "unknown",
            "red_flags": ["title_mismatch"],
            "stack_match": 0,
        }

    # 2. Hard negatives — disqualify
    for neg in HARD_NEGATIVES:
        if re.search(neg, t, re.IGNORECASE):
            return {
                "score": 0,
                "reason": f"Disqualified by negative keyword: {neg}",
                "is_relevant": False,
                "seniority": "unknown",
                "red_flags": [f"neg:{neg}"],
                "stack_match": 0,
            }

    # 2b. Бренды-исключения (WB/Ozon) — ищем в заголовке+описании+навыках
    for brand in BRAND_NEGATIVES:
        if re.search(brand, full, re.IGNORECASE):
            return {
                "score": 0,
                "reason": f"Disqualified by brand exclusion: {brand}",
                "is_relevant": False,
                "seniority": "unknown",
                "red_flags": [f"brand:{brand}"],
                "stack_match": 0,
            }

    # 2c. Зарплата ниже порога (если указана и в рублях) — отсеиваем.
    #     Без указанной ЗП — пропускаем дальше (многие не ставят).
    cur = (salary_currency or "").upper()
    if (salary_from or salary_to) and cur in ("", "RUR", "RUB", "РУБ"):
        best = max(salary_from or 0, salary_to or 0)
        if 0 < best < SALARY_FLOOR:
            return {
                "score": 0,
                "reason": f"Salary {best} below floor {SALARY_FLOOR}",
                "is_relevant": False,
                "seniority": "unknown",
                "red_flags": ["salary_below_floor"],
                "stack_match": 0,
            }

    # 3. Score build-up
    score = 30  # title matched
    stack_hits = 0
    matched_stack: list[str] = []
    for kw in STACK_KEYWORDS:
        if kw in full:
            stack_hits += 1
            matched_stack.append(kw)
    stack_score = min(stack_hits * 5, 40)
    score += stack_score

    # Salary
    if salary_from or salary_to:
        sal_mid = (salary_from or 0) + (salary_to or salary_from or 0)
        if salary_from and salary_to:
            sal_mid = (salary_from + salary_to) // 2
        elif salary_from:
            sal_mid = salary_from
        elif salary_to:
            sal_mid = salary_to
        if sal_mid >= desired_salary_min:
            score += 15
        elif sal_mid >= desired_salary_min * 0.7:
            score += 5
        else:
            red_flags.append("low_salary")
    # No salary = neutral, no penalty

    # Remote
    if is_remote:
        score += 5
    else:
        if "удалённ" in d or "удаленн" in d or "remote" in d:
            score += 5

    # Seniority
    seniority = "middle"
    is_senior = _match_any(full, LEVEL_SENIOR)
    is_middle = _match_any(full, LEVEL_MIDDLE)
    if is_senior and not is_middle:
        seniority = "senior"
        score += 5  # senior — выше, но всё ещё ок для middle+
    elif is_middle:
        score += 10

    score = max(0, min(score, 100))
    is_relevant = score >= 60

    reason_parts = [f"title=ok", f"stack={stack_hits}({stack_score}p)"]
    if matched_stack:
        reason_parts.append(f"matched: {', '.join(matched_stack[:5])}")
    if is_remote:
        reason_parts.append("remote")
    if seniority:
        reason_parts.append(f"level={seniority}")
    reason = ", ".join(reason_parts)

    return {
        "score": score,
        "reason": reason,
        "is_relevant": is_relevant,
        "seniority": seniority,
        "red_flags": red_flags,
        "stack_match": stack_score * 100 // 40 if stack_score else 0,
    }
