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

from pydantic import BaseModel, Field


class UserSettings(BaseModel):
    # --- Поиск ---
    # Ключевые слова/должность. Пусто = набор запросов по умолчанию (аналитик).
    search_text: str = ""
    # Где искать: в названии вакансии/компании/описании.
    search_fields: list[str] = Field(default_factory=lambda: ["name"])
    # Регион(ы) — id областей hh (Москва=1, СПб=2, вся Россия=113).
    areas: list[int] = Field(default_factory=lambda: [1])
    # Слова-исключения (через запятую).
    excluded_text: str = ""

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

    def to_hh_params(self) -> dict:
        """Собрать параметры для поиска вакансий hh /vacancies."""
        params: dict = {}
        if self.search_text:
            params["text"] = self.search_text
        if self.search_fields:
            params["search_field"] = self.search_fields
        if self.areas:
            params["area"] = self.areas
        if self.excluded_text:
            params["excluded_text"] = self.excluded_text
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
