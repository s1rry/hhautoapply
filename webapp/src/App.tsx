import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, getMe, getDictionaries, searchVacancies, getFavorites, countActiveFilters,
  recordHistory, saveSearch,
} from "./api";
import { EMPTY_FILTERS, type Dictionaries, type Filters, type VacancyCard } from "./types";
import { VacancyCardView } from "./components/VacancyCard";
import { FilterSheet } from "./components/FilterSheet";
import { Detail } from "./components/Detail";
import { Searches } from "./components/Searches";
import { Resume } from "./components/Resume";
import { pluralVacancies } from "./format";
import { haptic, backButton } from "./telegram";
import { useFavorites } from "./favorites";

const PER_PAGE = 20;

type Screen =
  | { name: "list" }
  | { name: "detail"; id: string }
  | { name: "favorites" }
  | { name: "searches" }
  | { name: "resume" };

export default function App() {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [items, setItems] = useState<VacancyCard[]>([]);
  const [found, setFound] = useState<number | null>(null);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [dict, setDict] = useState<Dictionaries | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [screen, setScreen] = useState<Screen>({ name: "list" });

  const fav = useFavorites();
  const abortRef = useRef<AbortController | null>(null);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const lastHistKey = useRef<string>("");
  const [savedNote, setSavedNote] = useState(false);

  useEffect(() => {
    getMe().then((me) => setConnected(me.connected)).catch(() => setConnected(false));
    getDictionaries().then(setDict).catch(() => setDict(null));
  }, []);

  // Telegram Back: закрывает панель / возвращает из деталей и избранного.
  useEffect(() => {
    const showBack = sheetOpen || screen.name !== "list";
    const onBack = () => {
      if (sheetOpen) setSheetOpen(false);
      else setScreen({ name: "list" });
    };
    backButton(showBack, onBack);
  }, [sheetOpen, screen]);

  const runSearch = useCallback(async (f: Filters, nextPage: number) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    if (nextPage === 0) setLoading(true);
    else setLoadingMore(true);
    setError(null);
    try {
      const res = await searchVacancies(f, nextPage, PER_PAGE, ctrl.signal);
      setFound(res.found);
      setPage(res.page);
      setItems((prev) => (nextPage === 0 ? res.items : [...prev, ...res.items]));
      // Пишем в историю только осмысленный поиск (есть текст или фильтры),
      // первую страницу, и дедупим повтор того же запроса.
      if (nextPage === 0 && (f.text.trim() || countActiveFilters(f) > 0)) {
        const key = JSON.stringify(f);
        if (key !== lastHistKey.current) {
          lastHistKey.current = key;
          recordHistory(f.text.trim(), f, res.found).catch(() => {});
        }
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof ApiError ? e.code : "network");
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, []);

  useEffect(() => {
    if (connected !== true) return;
    const t = setTimeout(() => runSearch(filtersRef.current, 0), 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, connected]);

  const sentinel = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el || found === null || screen.name !== "list") return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && !loading && !loadingMore && items.length < (found ?? 0)) {
        runSearch(filtersRef.current, page + 1);
      }
    }, { rootMargin: "300px" });
    io.observe(el);
    return () => io.disconnect();
  }, [found, page, loading, loadingMore, items.length, runSearch, screen]);

  const open = (id: string) => { haptic("light"); setScreen({ name: "detail", id }); };

  if (connected === false) {
    return (
      <div className="app">
        <div className="state">
          <h3>hh.ru не подключён</h3>
          <p>Чтобы искать вакансии под ваш профиль, подключите аккаунт hh в боте — команда /connect.</p>
        </div>
      </div>
    );
  }

  if (screen.name === "detail") {
    return (
      <div className="app">
        <Detail id={screen.id} saved={fav.ids.has(screen.id)} onToggleSave={fav.toggle} />
      </div>
    );
  }

  if (screen.name === "favorites") {
    return <div className="app"><Favorites fav={fav} onOpen={open} /></div>;
  }

  if (screen.name === "searches") {
    return (
      <div className="app">
        <Searches onApply={(f) => { setFilters(f); setScreen({ name: "list" }); }} />
      </div>
    );
  }

  if (screen.name === "resume") {
    return <div className="app"><Resume /></div>;
  }

  const canSave = filters.text.trim() !== "" || countActiveFilters(filters) > 0;
  const doSave = () => {
    const name = filters.text.trim() || `Фильтров: ${countActiveFilters(filters)}`;
    saveSearch(name, filters).then(() => {
      haptic("success");
      setSavedNote(true);
      setTimeout(() => setSavedNote(false), 1800);
    }).catch(() => {});
  };

  const activeCount = countActiveFilters(filters);
  return (
    <div className="app">
      <div className="search-header">
        <div className="topbar">
          <b>Вакансии</b>
          <span className="topbar-links">
            <button className="link-btn" onClick={() => { haptic("light"); setScreen({ name: "resume" }); }}>
              📄 Резюме
            </button>
            <button className="link-btn" onClick={() => { haptic("light"); setScreen({ name: "searches" }); }}>
              🔖 Поиски
            </button>
            <button className="link-btn" onClick={() => { haptic("light"); setScreen({ name: "favorites" }); }}>
              ♥
            </button>
          </span>
        </div>
        <div className="search-row">
          <input
            className="search-input"
            placeholder="Профессия, должность или компания"
            value={filters.text}
            onChange={(e) => setFilters((f) => ({ ...f, text: e.target.value }))}
            enterKeyHint="search"
          />
          <button className="filters-btn" onClick={() => { haptic("light"); setSheetOpen(true); }}>
            Фильтры{activeCount > 0 && <span className="badge">{activeCount}</span>}
          </button>
        </div>
        <ActiveChips filters={filters} dict={dict} onChange={setFilters} />
        <div className="header-actions">
          {found !== null && !loading && <span className="result-count">{pluralVacancies(found)}</span>}
          {canSave && (
            <button className="save-search" onClick={doSave}>
              {savedNote ? "✓ Сохранено" : "🔖 Сохранить поиск"}
            </button>
          )}
        </div>
      </div>

      {loading ? (
        <Skeletons />
      ) : error ? (
        <ErrorState code={error} onRetry={() => runSearch(filters, 0)} />
      ) : items.length === 0 ? (
        <EmptyState />
      ) : (
        <>
          {items.map((v) => (
            <VacancyCardView key={v.id} v={v} saved={fav.ids.has(v.id)} onOpen={open} onToggleSave={fav.toggle} />
          ))}
          <div ref={sentinel} />
          {loadingMore && <div className="loader">Загружаем ещё…</div>}
        </>
      )}

      {sheetOpen && (
        <FilterSheet
          initial={filters}
          dict={dict}
          onApply={(f) => { setFilters(f); setSheetOpen(false); }}
          onClose={() => setSheetOpen(false)}
        />
      )}
    </div>
  );
}

function Favorites({
  fav,
  onOpen,
}: {
  fav: ReturnType<typeof useFavorites>;
  onOpen: (id: string) => void;
}) {
  const [items, setItems] = useState<VacancyCard[] | null>(null);
  useEffect(() => {
    getFavorites().then((r) => setItems(r.items)).catch(() => setItems([]));
  }, []);
  // Показываем актуально сохранённые (учёт снятия сердечка на этом экране).
  const visible = (items ?? []).filter((v) => fav.ids.has(v.id));
  if (items === null) return <Skeletons />;
  if (visible.length === 0) {
    return (
      <div className="state">
        <h3>Пока пусто</h3>
        <p>Сохраняйте интересные вакансии сердечком — они появятся здесь.</p>
      </div>
    );
  }
  return (
    <div style={{ paddingTop: 12 }}>
      {visible.map((v) => (
        <VacancyCardView key={v.id} v={v} saved={fav.ids.has(v.id)} onOpen={onOpen} onToggleSave={fav.toggle} />
      ))}
    </div>
  );
}

function ActiveChips({
  filters,
  dict,
  onChange,
}: {
  filters: Filters;
  dict: Dictionaries | null;
  onChange: (fn: (f: Filters) => Filters) => void;
}) {
  const chips: { label: string; clear: () => void }[] = [];
  const nameOf = (list: { id: string; name: string }[] | undefined, id: string) =>
    list?.find((x) => x.id === id)?.name ?? id;

  for (const a of filters.areas)
    chips.push({ label: a.name, clear: () => onChange((f) => ({ ...f, areas: f.areas.filter((x) => x.id !== a.id) })) });
  const groups: [keyof Filters, { id: string; name: string }[] | undefined][] = [
    ["work_format", dict?.work_format], ["experience", dict?.experience],
    ["employment", dict?.employment], ["schedule", dict?.schedule],
    ["education", dict?.education], ["professional_role", dict?.professional_role],
    ["industry", dict?.industry],
  ];
  for (const [key, list] of groups) {
    for (const id of filters[key] as string[]) {
      chips.push({
        label: nameOf(list, id),
        clear: () => onChange((f) => ({ ...f, [key]: (f[key] as string[]).filter((x) => x !== id) })),
      });
    }
  }
  if (filters.salary)
    chips.push({ label: `от ${filters.salary.toLocaleString("ru-RU")} ₽`, clear: () => onChange((f) => ({ ...f, salary: undefined })) });
  if (filters.only_with_salary)
    chips.push({ label: "с доходом", clear: () => onChange((f) => ({ ...f, only_with_salary: false })) });

  if (chips.length === 0) return null;
  return (
    <div className="active-chips">
      {chips.map((c, i) => (
        <button className="chip-removable" key={i} onClick={() => { haptic("light"); c.clear(); }}>
          {c.label} ✕
        </button>
      ))}
    </div>
  );
}

function Skeletons() {
  return (
    <>
      {Array.from({ length: 6 }).map((_, i) => (
        <div className="skeleton" key={i} />
      ))}
    </>
  );
}

function EmptyState() {
  return (
    <div className="state">
      <h3>Ничего не найдено</h3>
      <p>Измените запрос, уберите часть фильтров или расширьте регион.</p>
    </div>
  );
}

function ErrorState({ code, onRetry }: { code: string; onRetry: () => void }) {
  const msg =
    code === "hh_token_revoked"
      ? "Доступ к hh истёк — переподключите аккаунт в боте (/connect)."
      : code === "hh_not_connected"
      ? "hh не подключён. Подключите аккаунт в боте (/connect)."
      : "Не удалось загрузить вакансии.";
  return (
    <div className="state">
      <h3>Ошибка</h3>
      <p>{msg}</p>
      <button onClick={onRetry}>Повторить</button>
    </div>
  );
}
