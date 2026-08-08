import { useEffect, useState } from "react";
import { getVacancy } from "../api";
import type { VacancyCard, VacancyDetail } from "../types";
import { formatSalary, formatDate } from "../format";
import { haptic } from "../telegram";

export function Detail({
  id,
  saved,
  onToggleSave,
}: {
  id: string;
  saved: boolean;
  onToggleSave: (card: VacancyCard) => void;
}) {
  const [v, setV] = useState<VacancyDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setV(null);
    setError(null);
    getVacancy(id)
      .then((d) => alive && setV(d))
      .catch(() => alive && setError("Не удалось загрузить вакансию"));
    return () => { alive = false; };
  }, [id]);

  if (error) return <div className="state"><h3>Ошибка</h3><p>{error}</p></div>;
  if (!v) return <div className="detail"><div className="skeleton" style={{ height: 320 }} /></div>;

  const salary = formatSalary(v);
  const asCard: VacancyCard = {
    id: v.id, name: v.name, company: v.company, company_logo: v.company_logo, area: v.area,
    salary_from: v.salary_from, salary_to: v.salary_to, currency: v.currency,
    experience: v.experience, schedule: v.schedule, employment: v.employment,
    published_at: v.published_at, url: v.url, requirement: null, responsibility: null,
  };

  return (
    <div className="detail">
      <div className="detail-head">
        {v.company_logo && <img className="detail-logo" src={v.company_logo} alt="" />}
        <h1 className="detail-title">{v.name}</h1>
        {salary && <div className="detail-salary">{salary}</div>}
        {v.company && <div className="detail-company">{v.company}</div>}
        {(v.area || v.address) && <div className="detail-area">{v.address || v.area}</div>}
      </div>

      <div className="chips">
        {v.experience && <span className="chip">{v.experience}</span>}
        {v.schedule && <span className="chip">{v.schedule}</span>}
        {v.employment && <span className="chip">{v.employment}</span>}
      </div>

      {v.key_skills.length > 0 && (
        <div className="detail-block">
          <h4>Ключевые навыки</h4>
          <div className="chips">
            {v.key_skills.map((s) => <span className="chip skill" key={s}>{s}</span>)}
          </div>
        </div>
      )}

      {v.description && (
        <div className="detail-block detail-desc" dangerouslySetInnerHTML={{ __html: v.description }} />
      )}

      {v.published_at && <div className="card-date">Опубликовано {formatDate(v.published_at)}</div>}

      <div className="detail-actions">
        <button className={`btn-ghost ${saved ? "saved" : ""}`} onClick={() => onToggleSave(asCard)}>
          {saved ? "♥ В сохранённых" : "♡ Сохранить"}
        </button>
        {v.url && (
          <a className="btn-primary" href={v.url} target="_blank" rel="noreferrer" onClick={() => haptic("light")}>
            Открыть на hh.ru
          </a>
        )}
      </div>
    </div>
  );
}
