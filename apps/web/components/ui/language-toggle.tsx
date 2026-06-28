"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";
import { cn } from "@/lib/cn";
import { setLocale } from "@/lib/i18n/actions";
import { useLocale } from "@/lib/i18n/provider";

/**
 * LanguageToggle — 2-segment mono control for JA|EN.
 * active = white text + left-edge white tick (current location = white).
 * aria-pressed / full keyboard support.
 */
export function LanguageToggle() {
  const locale = useLocale();
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  function toggle(next: "ja" | "en") {
    if (next === locale || isPending) return;
    startTransition(async () => {
      await setLocale(next);
      router.refresh();
    });
  }

  return (
    <span className="inline-flex items-center rounded border border-outline-variant">
      {(["ja", "en"] as const).map((lang) => {
        const active = locale === lang;
        return (
          <button
            key={lang}
            type="button"
            aria-pressed={active}
            aria-label={lang === "ja" ? "日本語" : "English"}
            disabled={isPending}
            onClick={() => toggle(lang)}
            className={cn(
              "data w-7 py-1 text-center uppercase transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--color-surface-container-low)]",
              active ? "text-on-surface" : "text-on-surface-variant hover:text-on-surface",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
            style={active ? { boxShadow: "inset 2px 0 0 0 var(--color-on-surface)" } : undefined}
          >
            {lang.toUpperCase()}
          </button>
        );
      })}
    </span>
  );
}
