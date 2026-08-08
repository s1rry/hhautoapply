/** Клиент к бэкенду Mini App. В каждый запрос кладём Telegram initData —
 * бэкенд по ней аутентифицирует пользователя (см. app/api/webapp_auth.py).
 *
 * Поддерживает отмену устаревших запросов (AbortController), чтобы при быстром
 * наборе не показывать результаты предыдущего запроса (race condition). */
import { initData } from "./telegram";
import type { Filters, MeResponse, SearchResponse } from "./types";

const BASE = "/api";

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "X-Telegram-Init-Data": initData() },
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body?.error || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string) {
    super(code);
  }
}

export function getMe(signal?: AbortSignal): Promise<MeResponse> {
  return get<MeResponse>("/me", signal);
}

export function searchVacancies(
  f: Filters,
  page: number,
  perPage: number,
  signal?: AbortSignal
): Promise<SearchResponse> {
  const q = new URLSearchParams();
  if (f.text.trim()) q.set("text", f.text.trim());
  for (const a of f.area) q.append("area", a);
  for (const e of f.experience) q.append("experience", e);
  for (const e of f.employment) q.append("employment", e);
  for (const s of f.schedule) q.append("schedule", s);
  for (const w of f.work_format) q.append("work_format", w);
  if (f.salary) q.set("salary", String(f.salary));
  if (f.only_with_salary) q.set("only_with_salary", "true");
  if (f.order_by) q.set("order_by", f.order_by);
  q.set("page", String(page));
  q.set("per_page", String(perPage));
  return get<SearchResponse>(`/vacancies/search?${q.toString()}`, signal);
}

export function countActiveFilters(f: Filters): number {
  let n = 0;
  n += f.area.length ? 1 : 0;
  n += f.experience.length ? 1 : 0;
  n += f.employment.length ? 1 : 0;
  n += f.schedule.length ? 1 : 0;
  n += f.work_format.length ? 1 : 0;
  n += f.salary ? 1 : 0;
  n += f.only_with_salary ? 1 : 0;
  return n;
}
