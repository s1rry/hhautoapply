/** Тонкая обёртка над Telegram WebApp SDK (window.Telegram.WebApp).
 *
 * Держит всё «телеграмное» в одном месте: initData для авторизации, тему,
 * кнопку «Назад», haptic. Если открыто вне Telegram (обычный браузер) —
 * методы деградируют без ошибок, чтобы можно было отлаживать в dev. */

interface TgWebApp {
  initData: string;
  colorScheme: "light" | "dark";
  themeParams: Record<string, string>;
  viewportStableHeight?: number;
  ready: () => void;
  expand: () => void;
  BackButton: { show: () => void; hide: () => void; onClick: (cb: () => void) => void; offClick: (cb: () => void) => void };
  HapticFeedback?: { impactOccurred: (s: string) => void; notificationOccurred: (t: string) => void; selectionChanged: () => void };
  onEvent: (e: string, cb: () => void) => void;
}

function tg(): TgWebApp | null {
  return (window as unknown as { Telegram?: { WebApp?: TgWebApp } }).Telegram?.WebApp ?? null;
}

export function initTelegram(): void {
  const w = tg();
  if (!w) return;
  w.ready();
  w.expand();
  applyTheme();
  w.onEvent("themeChanged", applyTheme);
}

/** initData — подпись, которую бэкенд проверяет. В dev вне Telegram пусто. */
export function initData(): string {
  return tg()?.initData ?? "";
}

export function isInsideTelegram(): boolean {
  return Boolean(tg()?.initData);
}

/** Прокинуть тему Telegram в CSS-переменные (:root). */
function applyTheme(): void {
  const w = tg();
  const root = document.documentElement;
  if (!w) return;
  root.dataset.theme = w.colorScheme;
  const p = w.themeParams || {};
  const map: Record<string, string | undefined> = {
    "--tg-bg": p.bg_color,
    "--tg-text": p.text_color,
    "--tg-hint": p.hint_color,
    "--tg-link": p.link_color,
    "--tg-button": p.button_color,
    "--tg-button-text": p.button_text_color,
    "--tg-secondary-bg": p.secondary_bg_color,
  };
  for (const [k, v] of Object.entries(map)) if (v) root.style.setProperty(k, v);
}

export function backButton(show: boolean, onClick?: () => void): void {
  const w = tg();
  if (!w) return;
  if (show) {
    if (onClick) w.BackButton.onClick(onClick);
    w.BackButton.show();
  } else {
    w.BackButton.hide();
  }
}

export function haptic(type: "light" | "medium" | "success" | "error" = "light"): void {
  const h = tg()?.HapticFeedback;
  if (!h) return;
  if (type === "success" || type === "error") h.notificationOccurred(type);
  else h.impactOccurred(type);
}
