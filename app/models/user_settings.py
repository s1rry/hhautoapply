"""
Схема настроек пользователя ("Задача" в боте).

Повторяет фильтры поиска hh.ru и параметры автоотклика. Хранится как JSON
в поле User.settings. Значения полей-списков используют коды hh API, чтобы
их можно было напрямую отдавать в поиск вакансий.

Справочники кодов hh (используются в поиске /vacancies):
  search_fields : name | company_name | description
  work_format   : ON_SITE | REMOTE | HYBRID | FIELD_WORK   (формат работы)
  schedule      : fullDay | shift | flexible | remote | flyInFlyOut  (график)
  experience    : noExperience | between1And3 | between3And6 | moreThan6
  employment    : full | part | project | probation                 (тип занятости)
  education      : not_required_or_not_specified | higher | special_secondary
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field


class UserSettings(BaseModel):
    # --- Поиск ---
    # Ключевые слова/должность. Пусто = набор запросов по умолчанию (аналитик).
    search_text: str = ""
    # Где искать: в названии вакансии/компании/описании.
    search_fields: list[str] = Field(default_factory=lambda: ["name"])
    # Искать не только в названии, но и в описании вакансии (в разы больше
    # вакансий; точность держит умный ИИ-отбор). По умолчанию включено.
    search_in_description: bool = True
    # Источник вакансий: "keyword" — поиск по ключу задачи; "recommended" —
    # лента рекомендаций hh под резюме (/similar_vacancies, большая и сама
    # обновляется); "both" — сначала ключ, потом рекомендации.
    vacancy_source: str = "keyword"
    # Регион(ы) — id областей hh (Москва=1, СПб=2, вся Россия=113).
    areas: list[int] = Field(default_factory=lambda: [1])
    # Слова-исключения (через запятую).
    excluded_text: str = ""
    # Контакт для сопроводительных писем (например второй ТГ @username, почта,
    # телефон). Подставляется в письмо, чтобы HR писал не на личный ТГ.
    contact: str = ""

    # --- Условия работы ---
    # Формат работы (новый параметр hh work_format).
    work_format: list[str] = Field(default_factory=list)
    # График работы.
    schedule: list[str] = Field(default_factory=list)
    # Опыт работы.
    experience: list[str] = Field(default_factory=list)
    # Тип занятости.
    employment: list[str] = Field(default_factory=list)
    # Образование.
    education: list[str] = Field(default_factory=list)

    # --- Зарплата ---
    salary_min: int = 0          # "от", 0 = не задано
    only_with_salary: bool = False

    # --- Прочие флаги hh (панель "Другие параметры") ---
    exclude_agencies: bool = False       # без вакансий от кадровых агентств
    accredited_it_only: bool = False     # от аккредитованных ИТ-компаний
    with_address: bool = False           # только с адресом
    low_competition: bool = False        # меньше 10 откликов (низкая конкуренция)
    age_16: bool = False                 # доступные с 16 лет
    age_14: bool = False                 # доступные с 14 лет
    # Особенности здоровья (панель "Особенности здоровья"), коды hh
    # accept_handicapped и т.п. — опционально, для инклюзивного поиска.
    health_features: list[str] = Field(default_factory=list)

    # --- Письма / ИИ ---
    ai_enabled: bool = False             # ИИ-персонализация писем
    ai_custom_prompt: str = ""           # свой промт для ИИ-писем (пусто = стандартный)
    custom_letter: str = ""              # своё готовое письмо (шлётся как есть, без ИИ)
    # Умный отбор: ИИ оценивает вакансию по резюме (0–100) и отклик идёт
    # только на совпадения >= ai_score_min. Отсекает слабые вакансии.
    ai_score_enabled: bool = False
    ai_score_min: int = 70
    # Режим сопроводительных писем:
    #   always   — всегда прикладывать письмо (шаблон или ИИ)
    #   required — только когда вакансия требует письмо
    #   off      — не прикладывать письмо
    letter_mode: str = "always"

    # --- Автоотклик ---
    resume_bump: bool = True             # авто-поднятие резюме на hh (раз в ~4ч)
    daily_limit: int = 50                # лимит откликов в день (free=50, paid выше)
    # Окно откликов по МСК (по умолчанию 9-21, настраивается).
    apply_hour_start: int = 9
    apply_hour_end: int = 21
    # Пауза между откликами, сек (анти-бан).
    apply_delay_min: int = 3
    apply_delay_max: int = 12

    def search_phrases(self) -> list[str]:
        """Ключевые фразы для поиска (через запятую/слэш/перенос строки).

        hh не понимает булев OR в тексте (возвращает 0), поэтому ищем по каждой
        фразе отдельным запросом и объединяем — как при одиночной фразе, что
        заведомо работает.
        """
        raw = (self.search_text or "").strip()
        if not raw:
            return []
        return [p.strip() for p in re.split(r"[,/\n]+", raw) if p.strip()]

    def excluded_words(self) -> list[str]:
        """Слова-исключения (через запятую/слэш/перенос) в нижнем регистре.

        Фильтруем на своей стороне: hh не понимает список исключений (запятую
        трактует как фразу), поэтому отсев делаем сами по названию/описанию.
        """
        raw = (self.excluded_text or "").strip().lower()
        if not raw:
            return []
        return [w.strip() for w in re.split(r"[,/\n]+", raw) if w.strip()]

    def to_hh_params(self) -> dict:
        """Параметры поиска hh /vacancies БЕЗ text — фразу подставляет движок."""
        params: dict = {}
        params["search_field"] = ["name", "description"] if self.search_in_description else ["name"]
        if self.areas:
            params["area"] = self.areas
        if self.work_format:
            params["work_format"] = self.work_format
        if self.schedule:
            params["schedule"] = self.schedule
        if self.experience:
            params["experience"] = self.experience
        if self.employment:
            params["employment"] = self.employment
        if self.education:
            params["education"] = self.education
        if self.salary_min:
            params["salary"] = self.salary_min
        if self.only_with_salary:
            params["only_with_salary"] = "true"
        # label в hh — мультизначный; собираем в список.
        labels: list[str] = []
        if self.exclude_agencies:
            labels.append("not_from_agency")
        if self.accredited_it_only:
            labels.append("accredited_it")
        if self.with_address:
            labels.append("with_address")
        if self.low_competition:
            labels.append("low_performance")  # TODO: сверить код hh для "меньше 10 откликов"
        if labels:
            params["label"] = labels
        if self.health_features:
            params["accept_handicapped"] = "true"
        # age_14/age_16 хранятся в настройках; точные коды hh для возрастной
        # доступности сверим при интеграции поиска (Фаза 3), пока не шлём.
        return params
