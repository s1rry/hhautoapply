import type { VacancyCard as V } from "../types";
import { formatSalary, formatDate } from "../format";

export function VacancyCardView({
  v,
  saved,
  onOpen,
  onToggleSave,
}: {
  v: V;
  saved: boolean;
  onOpen: (id: string) => void;
  onToggleSave: (v: V) => void;
}) {
  const salary = formatSalary(v);
  const snippet = v.responsibility || v.requirement;
  return (
    <div className="card" onClick={() => onOpen(v.id)}>
      <div className="card-top">
        <div style={{ minWidth: 0 }}>
          <h3 className="card-title">{v.name}</h3>
          {salary && <div className="card-salary">{salary}</div>}
          {v.company && <div className="card-company">{v.company}</div>}
        </div>
        <button
          className={`heart ${saved ? "on" : ""}`}
          aria-label={saved ? "Убрать из сохранённых" : "Сохранить"}
          onClick={(e) => { e.stopPropagation(); onToggleSave(v); }}
        >
          {saved ? "♥" : "♡"}
        </button>
      </div>
      <div className="chips">
        {v.area && <span className="chip">{v.area}</span>}
        {v.experience && <span className="chip">{v.experience}</span>}
        {v.schedule && <span className="chip">{v.schedule}</span>}
        {v.employment && <span className="chip">{v.employment}</span>}
      </div>
      {snippet && <div className="card-snippet" dangerouslySetInnerHTML={{ __html: snippet }} />}
      {v.published_at && <div className="card-date">{formatDate(v.published_at)}</div>}
    </div>
  );
}
