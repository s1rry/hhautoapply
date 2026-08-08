import { useEffect, useState } from "react";
import { getResume, bumpResume } from "../api";
import type { ResumeView } from "../types";
import { haptic } from "../telegram";

const CUR: Record<string, string> = { RUR: "₽", RUB: "₽", USD: "$", EUR: "€", KZT: "₸" };

function experienceText(months: number | null): string {
  if (!months) return "";
  const y = Math.floor(months / 12);
  const m = months % 12;
  const yy = y ? `${y} ${plural(y, ["год", "года", "лет"])}` : "";
  const mm = m ? `${m} ${plural(m, ["месяц", "месяца", "месяцев"])}` : "";
  return [yy, mm].filter(Boolean).join(" ");
}
function plural(n: number, f: [string, string, string]): string {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return f[0];
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return f[1];
  return f[2];
}
function period(start: string | null, end: string | null): string {
  const fmt = (s: string | null) => (s ? new Date(s).toLocaleDateString("ru-RU", { month: "short", year: "numeric" }) : "");
  if (!start) return "";
  return `${fmt(start)} — ${end ? fmt(end) : "по наст. время"}`;
}

export function Resume() {
  const [r, setR] = useState<ResumeView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [bumping, setBumping] = useState(false);
  const [bumpMsg, setBumpMsg] = useState<string | null>(null);

  useEffect(() => {
    getResume()
      .then(setR)
      .catch((e) => setError(e?.code === "no_resume" ? "no_resume" : "load"));
  }, []);

  const doBump = async () => {
    setBumping(true);
    try {
      const res = await bumpResume();
      haptic(res.ok ? "success" : "error");
      setBumpMsg(res.ok ? "Резюме поднято в поиске ✓" : "Поднять пока нельзя — hh разрешает раз в несколько часов");
    } catch {
      haptic("error");
      setBumpMsg("Не удалось поднять резюме");
    } finally {
      setBumping(false);
      setTimeout(() => setBumpMsg(null), 3000);
    }
  };

  if (error === "no_resume")
    return <div className="state"><h3>Резюме не найдено</h3><p>Создайте резюме на hh.ru — оно появится здесь.</p></div>;
  if (error) return <div className="state"><h3>Ошибка</h3><p>Не удалось загрузить резюме.</p></div>;
  if (!r) return <div className="detail"><div className="skeleton" style={{ height: 280 }} /></div>;

  const salary = r.salary_amount ? `${r.salary_amount.toLocaleString("ru-RU")} ${CUR[r.salary_currency ?? ""] ?? r.salary_currency ?? ""}` : null;

  return (
    <div className="detail">
      <div className="detail-head">
        <h1 className="detail-title">{r.title || "Моё резюме"}</h1>
        {(r.first_name || r.last_name) && <div className="detail-company">{[r.first_name, r.last_name].filter(Boolean).join(" ")}</div>}
        {salary && <div className="detail-salary">{salary}</div>}
        {r.area && <div className="detail-area">{r.area}</div>}
      </div>

      <div className="chips">
        {r.total_months ? <span className="chip">Опыт: {experienceText(r.total_months)}</span> : null}
        {r.education_level && <span className="chip">{r.education_level}</span>}
      </div>

      {r.skills.length > 0 && (
        <div className="detail-block">
          <h4>Навыки</h4>
          <div className="chips">{r.skills.map((s) => <span className="chip skill" key={s}>{s}</span>)}</div>
        </div>
      )}

      {r.experience.length > 0 && (
        <div className="detail-block">
          <h4>Опыт работы</h4>
          {r.experience.map((e, i) => (
            <div className="exp" key={i}>
              <div className="exp-pos">{e.position}</div>
              <div className="exp-meta">{e.company}{e.company && (e.start || e.end) ? " · " : ""}{period(e.start, e.end)}</div>
              {e.description && <div className="card-snippet" dangerouslySetInnerHTML={{ __html: e.description }} />}
            </div>
          ))}
        </div>
      )}

      {bumpMsg && <div className="bump-msg">{bumpMsg}</div>}
      <div className="detail-actions">
        <button className="btn-primary" onClick={doBump} disabled={bumping}>
          {bumping ? "Поднимаю…" : "⬆️ Поднять резюме в поиске"}
        </button>
        {r.url && (
          <a className="btn-ghost" href={r.url} target="_blank" rel="noreferrer" onClick={() => haptic("light")}>
            На hh.ru
          </a>
        )}
      </div>
    </div>
  );
}
