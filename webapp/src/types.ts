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

export interface DictItem {
  id: string;
  name: string;
  group?: string;
}

export interface Dictionaries {
  experience: DictItem[];
  employment: DictItem[];
  schedule: DictItem[];
  order_by: DictItem[];
  work_format: DictItem[];
  education: DictItem[];
  professional_role: DictItem[];
  industry: DictItem[];
}

export interface SelectedArea {
  id: string;
  name: string;
}

/** Активные фильтры поиска (соответствуют параметрам /vacancies hh). */
export interface Filters {
  text: string;
  searchField: string[]; // name | company_name | description
  areas: SelectedArea[];
  experience: string[];
  employment: string[];
  schedule: string[];
  work_format: string[];
  education: string[];
  professional_role: string[];
  industry: string[];
  salary?: number;
  only_with_salary?: boolean;
  order_by?: string;
}

export const EMPTY_FILTERS: Filters = {
  text: "",
  searchField: [],
  areas: [],
  experience: [],
  employment: [],
  schedule: [],
  work_format: [],
  education: [],
  professional_role: [],
  industry: [],
};
