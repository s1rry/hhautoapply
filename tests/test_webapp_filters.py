"""Тесты сборки hh-параметров из фильтров и нормализации карточек вакансий."""
from multidict import MultiDict

from app.api.webapp_filters import build_hh_params, vacancy_card, vacancy_detail


def test_build_params_multi_and_scalar():
    q = MultiDict()
    q.add("area", "1")
    q.add("area", "2")
    q.add("experience", "between1And3")
    q.add("text", "python")
    q.add("salary", "150000")
    q.add("only_with_salary", "true")
    params = build_hh_params(q)
    assert params["area"] == ["1", "2"]
    assert params["experience"] == ["between1And3"]
    assert params["text"] == "python"
    assert params["salary"] == "150000"
    assert params["only_with_salary"] == "true"


def test_build_params_order_by_whitelist():
    good = build_hh_params(MultiDict([("order_by", "salary_desc")]))
    assert good["order_by"] == "salary_desc"
    bad = build_hh_params(MultiDict([("order_by", "; DROP TABLE")]))
    assert "order_by" not in bad


def test_build_params_ignores_unknown_keys():
    params = build_hh_params(MultiDict([("evil", "1"), ("text", "qa")]))
    assert params == {"text": "qa"}


def test_vacancy_card_normalization():
    raw = {
        "id": "42", "name": "QA Engineer",
        "employer": {"name": "Acme", "logo_urls": {"90": "http://logo"}},
        "area": {"name": "Москва"},
        "salary": {"from": 100000, "to": 150000, "currency": "RUR"},
        "experience": {"name": "1–3 года"},
        "snippet": {"requirement": "req", "responsibility": "resp"},
        "alternate_url": "http://hh/42",
    }
    card = vacancy_card(raw)
    assert card["id"] == "42"
    assert card["company"] == "Acme"
    assert card["company_logo"] == "http://logo"
    assert card["salary_from"] == 100000 and card["currency"] == "RUR"
    assert card["url"] == "http://hh/42"


def test_vacancy_detail_skills_and_missing_fields():
    raw = {
        "id": "7", "name": "Dev",
        "key_skills": [{"name": "Python"}, {"name": "SQL"}],
        "salary": None, "employer": {}, "area": {}, "address": None,
    }
    d = vacancy_detail(raw)
    assert d["key_skills"] == ["Python", "SQL"]
    assert d["company"] is None
    assert d["salary_from"] is None
