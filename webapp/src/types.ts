export interface VacancyCard {
  id: string;
  name: string;
  company: string | null;
  company_logo: string | null;
  area: string | null;
  salary_from: number | null;
  salary_to: number | null;
  currency: string | null;
  experience: string | null;
  schedule: string | null;
  employment: string | null;
  published_at: string | null;
  url: string | null;
  requirement: string | null;
  responsibility: string | null;
}

export interface SearchResponse {
  found: number;
  page: number;
  per_page: number;
  items: VacancyCard[];
}

export interface MeResponse {
  exists: boolean;
  telegram_id?: number;
  username?: string | null;
  connected: boolean;
  has_resume?: boolean;
  is_paid?: boolean;
}

/** Активные фильтры поиска (расширяется по мере готовности спеки). */
export interface Filters {
  text: string;
  area: string[];
  experience: string[];
  employment: string[];
  schedule: string[];
  work_format: string[];
  salary?: number;
  only_with_salary?: boolean;
  order_by?: string;
}

export const EMPTY_FILTERS: Filters = {
  text: "",
  area: [],
  experience: [],
  employment: [],
  schedule: [],
  work_format: [],
};
