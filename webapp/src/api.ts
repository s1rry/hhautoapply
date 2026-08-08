/** Клиент к бэкенду Mini App. В каждый запрос кладём Telegram initData —
 * бэкенд по ней аутентифицирует пользователя (см. app/api/webapp_auth.py).
 *
 * Поддерживает отмену устаревших запросов (AbortController), чтобы при быстром
 * наборе не показывать результаты предыдущего запроса (race condition). */
import { initData } from "./telegram";
import type {
  Dictionaries, Filters, MeResponse, SearchResponse, SelectedArea, VacancyCard, VacancyDetail,
} from "./types";

const BASE = "/api";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "X-Telegram-Init-Data": initData(), ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body?.error || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return req<T>(path, { signal });
}

export class ApiError extends Error {
  constructor(public status: number, public code: string) {
    super(code);
  }
}

export function getMe(signal?: AbortSignal): Promise<MeResponse> {
  return get<MeResponse>("/me", signal);
}

export function getDictionaries(signal?: AbortSignal): Promise<Dictionaries> {
  return get<Dictionaries>("/dictionaries", signal);
}

export async function suggestAreas(text: string, signal?: AbortSignal): Promise<SelectedArea[]> {
  const r = await get<{ items: SelectedArea[] }>(
    `/areas/suggest?text=${encodeURIComponent(text)}`,
    signal
  );
  return r.items;
}

function buildQuery(f: Filters, page: number, perPage: number): string {
  const q = new URLSearchParams();
  if (f.text.trim()) q.set("text", f.text.trim());
  for (const s of f.searchField) q.append("search_field", s);
  for (const a of f.areas) q.append("area", a.id);
  for (const e of f.experience) q.append("experience", e);
  for (const e of f.employment) q.append("employment", e);
  for (const s of f.schedule) q.append("schedule", s);
  for (const w of f.work_format) q.append("work_format", w);
  for (const e of f.education) q.append("education", e);
  for (const r of f.professional_role) q.append("professional_role", r);
  for (const i of f.industry) q.append("industry", i);
  if (f.salary) q.set("salary", String(f.salary));
  if (f.only_with_salary) q.set("only_with_salary", "true");
  if (f.order_by) q.set("order_by", f.order_by);
  q.set("page", String(page));
  q.set("per_page", String(perPage));
  return q.toString();
}

export function searchVacancies(
  f: Filters,
  page: number,
  perPage: number,
  signal?: AbortSignal
): Promise<SearchResponse> {
  return get<SearchResponse>(`/vacancies/search?${buildQuery(f, page, perPage)}`, signal);
}

export function getVacancy(id: string, signal?: AbortSignal): Promise<VacancyDetail> {
  return get<VacancyDetail>(`/vacancies/${encodeURIComponent(id)}`, signal);
}

export function getFavorites(signal?: AbortSignal): Promise<{ items: VacancyCard[]; ids: string[] }> {
  return get<{ items: VacancyCard[]; ids: string[] }>("/favorites", signal);
}

export function addFavorite(card: VacancyCard): Promise<{ ok: boolean; id: string }> {
  return req("/favorites", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(card),
  });
}

export function removeFavorite(id: string): Promise<{ ok: boolean; id: string }> {
  return req(`/favorites/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** Сколько «групп» фильтров активно (для бейджа на кнопке «Фильтры»). */
export function countActiveFilters(f: Filters): number {
  let n = 0;
  if (f.areas.length) n++;
  if (f.experience.length) n++;
  if (f.employment.length) n++;
  if (f.schedule.length) n++;
  if (f.work_format.length) n++;
  if (f.education.length) n++;
  if (f.professional_role.length) n++;
  if (f.industry.length) n++;
  if (f.salary) n++;
  if (f.only_with_salary) n++;
  if (f.searchField.length) n++;
  return n;
}
