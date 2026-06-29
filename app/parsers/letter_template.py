"""
Шаблонные сопроводительные письма без трат AI-токенов.

Синтаксис шаблона (как в hh-applicant-tool):
  {вариант1|вариант2|вариант3}  → случайно выбирается один вариант
  %(vacancy_name)s              → подставляется название вакансии
  %(vacancy_suffix)s            → " «Название»" либо пусто, если названия нет

Зачем: раньше письмо было одинаковым байт-в-байт для всех откликов.
Теперь каждое письмо немного разное и упоминает вакансию. Токены не тратятся,
AI остаётся только для вакансий с обязательным тестом/вопросами.
"""
from __future__ import annotations

import random
import re

_CHOICE_RE = re.compile(r"\{([^{}]+)\}")

DEFAULT_TEMPLATE = (
    "{Здравствуйте|Добрый день}! "
    "{Заинтересовала ваша вакансия|Откликаюсь на вашу вакансию|"
    "Заинтересовало ваше предложение}%(vacancy_suffix)s. "
    "Имею коммерческий опыт в роли системного и бизнес-аналитика: "
    "сбор и анализ требований, BPMN / UML, проектирование REST API "
    "и интеграций, SQL, постановка задач разработчикам, приёмка результатов. "
    "{Готов обсудить детали и пройти интервью.|"
    "Буду рад обсудить детали на интервью.|"
    "Готов подробнее рассказать о своём опыте на собеседовании.}\n\n"
    "Контакты: i.egorov8080@gmail.com, tg https://t.me/egorov_analyst"
)


def _expand_choices(text: str) -> str:
    """Заменить каждый {a|b|c} на случайный вариант (один проход)."""
    return _CHOICE_RE.sub(lambda m: random.choice(m.group(1).split("|")), text)


def render_letter(vacancy_name: str = "", template: str | None = None) -> str:
    """Собрать готовое письмо из шаблона."""
    text = _expand_choices(template or DEFAULT_TEMPLATE)
    name = (vacancy_name or "").strip()
    suffix = f" «{name}»" if name else ""
    text = text.replace("%(vacancy_suffix)s", suffix)
    text = text.replace("%(vacancy_name)s", name)
    return text
