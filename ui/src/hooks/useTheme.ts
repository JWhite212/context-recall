import { useEffect, useSyncExternalStore } from "react";

export type Theme = "system" | "light" | "dark";

const STORAGE_KEY = "meetingmind-theme";

function getStored(): Theme {
  return (localStorage.getItem(STORAGE_KEY) as Theme) || "system";
}

function resolveEffective(theme: Theme): "light" | "dark" {
  if (theme !== "system") return theme;
  return window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
}

function apply(theme: Theme) {
  const effective = resolveEffective(theme);
  document.documentElement.classList.toggle("light", effective === "light");
  document.documentElement.classList.toggle("dark", effective === "dark");
}

// Tiny external store so multiple components stay in sync.
let current: Theme = getStored();
const listeners = new Set<() => void>();

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function getSnapshot() {
  return current;
}

export function setTheme(theme: Theme) {
  current = theme;
  localStorage.setItem(STORAGE_KEY, theme);
  apply(theme);
  listeners.forEach((cb) => cb());
}

export function useTheme() {
  const theme = useSyncExternalStore(subscribe, getSnapshot);

  // Apply on mount and listen for system changes.
  useEffect(() => {
    apply(theme);

    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const handler = () => {
      if (current === "system") apply("system");
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme]);

  return { theme, setTheme } as const;
}

// Apply immediately so there's no flash of wrong theme.
apply(current);
