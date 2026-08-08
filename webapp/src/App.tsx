import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getMe, searchVacancies, countActiveFilters } from "./api";
import { EMPTY_FILTERS, type Filters, type VacancyCard } from "./types";
import { VacancyCardView } from "./components/VacancyCard";
import { pluralVacancies } from "./format";
import { haptic } from "./telegram";

const PER_PAGE = 20;

export default function App() {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [items, setItems] = useState<VacancyCard[]>([]);
  const [found, setFound] = useState<number | null>(null);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  // Профиль: подключён ли hh. Без подключения поиск невозможен.
  useEffect(() => {
    getMe()
      .then((me) => setConnected(me.connected))
      .catch(() => setConnected(false));
  }, []);

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
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      const code = e instanceof ApiError ? e.code : "network";
      setError(code);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, []);

  // Дебаунс: поиск через 400мс после последнего изменения фильтров.
  useEffect(() => {
    if (connected !== true) return;
    const t = setTimeout(() => runSearch(filtersRef.current, 0), 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, connected]);

  // Бесконечная прокрутка.
  const sentinel = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el || found === null) return;
    const io = new IntersectionObserver((entries) => {
      if (
        entries[0].isIntersecting &&
        !loading &&
        !loadingMore &&
        items.length < (found ?? 0)
      ) {
        runSearch(filtersRef.current, page + 1);
      }
    }, { rootMargin: "300px" });
    io.observe(el);
    return () => io.disconnect();
  }, [found, page, loading, loadingMore, items.length, runSearch]);

  const activeCount = countActiveFilters(filters);

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

  return (
    <div className="app">
      <div className="search-header">
        <div className="search-row">
          <input
            className="search-input"
            placeholder="Профессия, должность или компания"
            value={filters.text}
            onChange={(e) => setFilters((f) => ({ ...f, text: e.target.value }))}
            enterKeyHint="search"
          />
          <button
            className="filters-btn"
            onClick={() => { haptic("light"); /* фильтры — следующий этап */ }}
          >
            Фильтры{activeCount > 0 && <span className="badge">{activeCount}</span>}
          </button>
        </div>
        {found !== null && !loading && (
          <div className="result-count">{pluralVacancies(found)}</div>
        )}
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
            <VacancyCardView key={v.id} v={v} />
          ))}
          <div ref={sentinel} />
          {loadingMore && <div className="loader">Загружаем ещё…</div>}
        </>
      )}
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
