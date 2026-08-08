import type { VacancyCard as V } from "../types";
import { formatSalary, formatDate } from "../format";
import { haptic } from "../telegram";

export function VacancyCardView({ v }: { v: V }) {
  const salary = formatSalary(v);
  const snippet = v.responsibility || v.requirement;
  return (
    <a
      className="card"
      href={v.url ?? "#"}
      target="_blank"
      rel="noreferrer"
      onClick={() => haptic("light")}
    >
      <div className="card-top">
        <div style={{ minWidth: 0 }}>
          <h3 className="card-title">{v.name}</h3>
          {salary && <div className="card-salary">{salary}</div>}
          {v.company && <div className="card-company">{v.company}</div>}
        </div>
        {v.company_logo && <img className="card-logo" src={v.company_logo} alt="" />}
      </div>
      <div className="chips">
        {v.area && <span className="chip">{v.area}</span>}
        {v.experience && <span className="chip">{v.experience}</span>}
        {v.schedule && <span className="chip">{v.schedule}</span>}
        {v.employment && <span className="chip">{v.employment}</span>}
      </div>
      {snippet && <div className="card-snippet" dangerouslySetInnerHTML={{ __html: snippet }} />}
      {v.published_at && <div className="card-date">{formatDate(v.published_at)}</div>}
    </a>
  );
}
