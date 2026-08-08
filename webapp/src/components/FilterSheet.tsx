import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { searchVacancies, suggestAreas } from "../api";
import type { Dictionaries, DictItem, Filters, SelectedArea } from "../types";
import { EMPTY_FILTERS } from "../types";
import { pluralVacancies } from "../format";
import { haptic } from "../telegram";

const SEARCH_FIELDS: DictItem[] = [
  { id: "name", name: "В названии вакансии" },
  { id: "company_name", name: "В названии компании" },
  { id: "description", name: "В описании" },
];

export function FilterSheet({
  initial,
  dict,
  onApply,
  onClose,
}: {
  initial: Filters;
  dict: Dictionaries | null;
  onApply: (f: Filters) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<Filters>(initial);
  const [count, setCount] = useState<number | null>(null);

  // Живой счётчик результатов по черновику фильтров (per_page=1).
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => {
    const t = setTimeout(async () => {
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      try {
        const r = await searchVacancies(draft, 0, 1, c.signal);
        setCount(r.found);
      } catch {
        /* игнорируем — счётчик необязателен */
      }
    }, 350);
    return () => clearTimeout(t);
  }, [draft]);

  const toggle = useCallback((key: keyof Filters, id: string) => {
    haptic("light");
    setDraft((f) => {
      const arr = f[key] as string[];
      const next = arr.includes(id) ? arr.filter((x) => x !== id) : [...arr, id];
      return { ...f, [key]: next };
    });
  }, []);

  return (
    <div className="sheet-overlay" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <div className="sheet-head">
          <h2>Фильтры</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Закрыть">✕</button>
        </div>

        <div className="sheet-body">
          <CheckSection
            title="Искать только"
            items={SEARCH_FIELDS}
            selected={draft.searchField}
            onToggle={(id) => toggle("searchField", id)}
          />

          <RegionPicker
            selected={draft.areas}
            onChange={(areas) => setDraft((f) => ({ ...f, areas }))}
          />

          <CheckSection title="Формат работы" items={dict?.work_format} selected={draft.work_format} onToggle={(id) => toggle("work_format", id)} />
          <CheckSection title="Опыт работы" items={dict?.experience} selected={draft.experience} onToggle={(id) => toggle("experience", id)} />
          <CheckSection title="Тип занятости" items={dict?.employment} selected={draft.employment} onToggle={(id) => toggle("employment", id)} />
          <CheckSection title="График работы" items={dict?.schedule} selected={draft.schedule} onToggle={(id) => toggle("schedule", id)} />

          <section className="fsec">
            <h3>Уровень дохода</h3>
            <input
              className="fsec-input"
              type="number"
              inputMode="numeric"
              placeholder="от, ₽"
              value={draft.salary ?? ""}
              onChange={(e) =>
                setDraft((f) => ({ ...f, salary: e.target.value ? Number(e.target.value) : undefined }))
              }
            />
            <label className="check">
              <input
                type="checkbox"
                checked={!!draft.only_with_salary}
                onChange={(e) => setDraft((f) => ({ ...f, only_with_salary: e.target.checked }))}
              />
              <span>Только с указанным доходом</span>
            </label>
          </section>

          <CheckSection title="Образование" items={dict?.education} selected={draft.education} onToggle={(id) => toggle("education", id)} />
          <SearchableSection title="Специализации" items={dict?.professional_role} selected={draft.professional_role} onToggle={(id) => toggle("professional_role", id)} />
          <SearchableSection title="Отрасль компании" items={dict?.industry} selected={draft.industry} onToggle={(id) => toggle("industry", id)} />

          <section className="fsec">
            <h3>Сортировка</h3>
            {(dict?.order_by ?? []).map((o) => (
              <label className="check" key={o.id}>
                <input
                  type="radio"
                  name="order_by"
                  checked={draft.order_by === o.id}
                  onChange={() => setDraft((f) => ({ ...f, order_by: o.id }))}
                />
                <span>{o.name}</span>
              </label>
            ))}
          </section>
        </div>

        <div className="sheet-foot">
          <button className="btn-ghost" onClick={() => setDraft({ ...EMPTY_FILTERS, text: draft.text })}>
            Сбросить
          </button>
          <button className="btn-primary" onClick={() => { haptic("medium"); onApply(draft); }}>
            {count === null ? "Показать" : `Показать ${pluralVacancies(count)}`}
          </button>
        </div>
      </div>
    </div>
  );
}

function CheckSection({
  title,
  items,
  selected,
  onToggle,
}: {
  title: string;
  items?: DictItem[];
  selected: string[];
  onToggle: (id: string) => void;
}) {
  if (!items || items.length === 0) return null;
  return (
    <section className="fsec">
      <h3>{title}</h3>
      {items.map((it) => (
        <label className="check" key={it.id}>
          <input type="checkbox" checked={selected.includes(it.id)} onChange={() => onToggle(it.id)} />
          <span>{it.name}</span>
        </label>
      ))}
    </section>
  );
}

/** Секция с поиском внутри (для больших деревьев: профроли, отрасли). */
function SearchableSection({
  title,
  items,
  selected,
  onToggle,
}: {
  title: string;
  items?: DictItem[];
  selected: string[];
  onToggle: (id: string) => void;
}) {
  const [q, setQ] = useState("");
  const filtered = useMemo(() => {
    if (!items) return [];
    const term = q.trim().toLowerCase();
    const base = term ? items.filter((i) => i.name.toLowerCase().includes(term)) : items;
    // Выбранные — всегда сверху и видны, даже вне поиска.
    const sel = items.filter((i) => selected.includes(i.id) && !base.includes(i));
    return [...sel, ...base].slice(0, term ? 60 : 40);
  }, [items, q, selected]);

  if (!items || items.length === 0) return null;
  return (
    <section className="fsec">
      <h3>{title}{selected.length > 0 && <span className="sec-badge">{selected.length}</span>}</h3>
      <input className="fsec-input" placeholder="Поиск…" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="check-list">
        {filtered.map((it) => (
          <label className="check" key={it.id}>
            <input type="checkbox" checked={selected.includes(it.id)} onChange={() => onToggle(it.id)} />
            <span>{it.name}{it.group && <em className="check-group"> · {it.group}</em>}</span>
          </label>
        ))}
      </div>
    </section>
  );
}

function RegionPicker({
  selected,
  onChange,
}: {
  selected: SelectedArea[];
  onChange: (areas: SelectedArea[]) => void;
}) {
  const [q, setQ] = useState("");
  const [opts, setOpts] = useState<SelectedArea[]>([]);
  const ctrl = useRef<AbortController | null>(null);

  useEffect(() => {
    if (q.trim().length < 2) { setOpts([]); return; }
    const t = setTimeout(async () => {
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      try {
        setOpts(await suggestAreas(q, c.signal));
      } catch { /* ignore */ }
    }, 250);
    return () => clearTimeout(t);
  }, [q]);

  const add = (a: SelectedArea) => {
    haptic("light");
    if (!selected.some((s) => s.id === a.id)) onChange([...selected, a]);
    setQ("");
    setOpts([]);
  };
  const remove = (id: string) => onChange(selected.filter((s) => s.id !== id));

  return (
    <section className="fsec">
      <h3>Регион</h3>
      {selected.length > 0 && (
        <div className="chips">
          {selected.map((a) => (
            <button className="chip-removable" key={a.id} onClick={() => remove(a.id)}>
              {a.name} ✕
            </button>
          ))}
        </div>
      )}
      <input className="fsec-input" placeholder="Город или регион" value={q} onChange={(e) => setQ(e.target.value)} />
      {opts.length > 0 && (
        <div className="suggest">
          {opts.map((o) => (
            <button className="suggest-item" key={o.id} onClick={() => add(o)}>{o.name}</button>
          ))}
        </div>
      )}
    </section>
  );
}
