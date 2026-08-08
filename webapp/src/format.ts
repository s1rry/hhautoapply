import type { VacancyCard } from "./types";

const CUR: Record<string, string> = { RUR: "₽", RUB: "₽", USD: "$", EUR: "€", KZT: "₸", BYR: "Br", UAH: "₴" };

export function formatSalary(v: VacancyCard): string | null {
  const { salary_from: from, salary_to: to, currency } = v;
  if (!from && !to) return null;
  const sym = currency ? CUR[currency] ?? currency : "";
  const n = (x: number) => x.toLocaleString("ru-RU");
  if (from && to) return `${n(from)}–${n(to)} ${sym}`;
  if (from) return `от ${n(from)} ${sym}`;
  return `до ${n(to as number)} ${sym}`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

export function pluralVacancies(n: number): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  let word = "вакансий";
  if (mod10 === 1 && mod100 !== 11) word = "вакансия";
  else if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) word = "вакансии";
  return `${n.toLocaleString("ru-RU")} ${word}`;
}
