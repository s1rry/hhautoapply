import { useEffect, useState } from "react";
import {
  getSavedSearches, deleteSavedSearch, getHistory, clearHistory,
  countActiveFilters, type SavedSearchItem, type HistoryItem,
} from "../api";
import type { Filters } from "../types";
import { pluralVacancies } from "../format";
import { haptic } from "../telegram";

function summary(f: Filters): string {
  const parts: string[] = [];
  if (f.text?.trim()) parts.push(`«${f.text.trim()}»`);
  for (const a of f.areas ?? []) parts.push(a.name);
  const n = countActiveFilters(f);
  if (n > 0) parts.push(`фильтров: ${n}`);
  return parts.join(" · ") || "Без параметров";
}

export function Searches({ onApply }: { onApply: (f: Filters) => void }) {
  const [saved, setSaved] = useState<SavedSearchItem[] | null>(null);
  const [history, setHistory] = useState<HistoryItem[] | null>(null);

  useEffect(() => {
    getSavedSearches().then((r) => setSaved(r.items)).catch(() => setSaved([]));
    getHistory().then((r) => setHistory(r.items)).catch(() => setHistory([]));
  }, []);

  const apply = (f: Filters) => { haptic("medium"); onApply(f); };

  const removeSaved = (id: number) => {
    haptic("light");
    setSaved((s) => (s ?? []).filter((x) => x.id !== id));
    deleteSavedSearch(id).catch(() => {});
  };

  const wipeHistory = () => {
    haptic("light");
    setHistory([]);
    clearHistory().catch(() => {});
  };

  return (
    <div style={{ paddingTop: 12 }}>
      <section className="fsec">
        <h3>Сохранённые поиски</h3>
        {saved && saved.length === 0 && <p className="muted">Сохраняйте набор фильтров кнопкой «🔖 Сохранить поиск».</p>}
        {(saved ?? []).map((s) => (
          <div className="row-item" key={s.id}>
            <button className="row-main" onClick={() => apply(s.filters)}>
              <div className="row-title">{s.name}</div>
              <div className="row-sub">{summary(s.filters)}</div>
            </button>
            <button className="row-del" onClick={() => removeSaved(s.id)} aria-label="Удалить">🗑</button>
          </div>
        ))}
      </section>

      <section className="fsec">
        <h3 style={{ justifyContent: "space-between" }}>
          История
          {history && history.length > 0 && (
            <button className="link-btn" onClick={wipeHistory}>Очистить</button>
          )}
        </h3>
        {history && history.length === 0 && <p className="muted">Здесь появятся ваши недавние поиски.</p>}
        {(history ?? []).map((h) => (
          <button className="row-item row-main" key={h.id} onClick={() => apply(h.filters)}>
            <div className="row-title">{h.text || "Без запроса"}</div>
            <div className="row-sub">{summary(h.filters)} · {pluralVacancies(h.found)}</div>
          </button>
        ))}
      </section>
    </div>
  );
}
