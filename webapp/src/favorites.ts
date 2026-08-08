import { useCallback, useEffect, useState } from "react";
import { addFavorite, getFavorites, removeFavorite } from "./api";
import type { VacancyCard } from "./types";
import { haptic } from "./telegram";

/** Общее состояние избранного: множество сохранённых id + оптимистичный toggle. */
export function useFavorites() {
  const [ids, setIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    getFavorites()
      .then((r) => setIds(new Set(r.ids)))
      .catch(() => {});
  }, []);

  const toggle = useCallback((card: VacancyCard) => {
    haptic("light");
    setIds((prev) => {
      const next = new Set(prev);
      if (next.has(card.id)) {
        next.delete(card.id);
        removeFavorite(card.id).catch(() => {});
      } else {
        next.add(card.id);
        addFavorite(card).catch(() => {});
      }
      return next;
    });
  }, []);

  return { ids, toggle };
}
