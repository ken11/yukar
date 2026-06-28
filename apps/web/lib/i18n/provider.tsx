"use client";

import { createContext, useCallback, useContext } from "react";
import type { Dict, Locale } from "./dictionary";

type I18nContextValue = {
  dict: Dict;
  locale: Locale;
};

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({
  dict,
  locale,
  children,
}: {
  dict: Dict;
  locale: Locale;
  children: React.ReactNode;
}) {
  return <I18nContext value={{ dict, locale }}>{children}</I18nContext>;
}

function resolvePath(obj: unknown, path: string): string {
  const keys = path.split(".");
  let current: unknown = obj;
  for (const key of keys) {
    if (current == null || typeof current !== "object") return path;
    current = (current as Record<string, unknown>)[key];
  }
  if (typeof current === "string") return current;
  return path;
}

export function useT() {
  const ctx = useContext(I18nContext);
  return useCallback(
    function t(path: string): string {
      if (!ctx) return path;
      return resolvePath(ctx.dict, path);
    },
    [ctx],
  );
}

export function useLocale(): Locale {
  const ctx = useContext(I18nContext);
  return ctx?.locale ?? "ja";
}

export function useDict(): Dict {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    // Fallback when rendered outside provider (e.g. tests without wrapper)
    return {} as Dict;
  }
  return ctx.dict;
}
