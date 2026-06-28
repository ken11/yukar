"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { CostTicker } from "@/components/features/usage/cost-ticker";
import { Icon } from "@/components/icon";
import { LanguageToggle } from "@/components/ui/language-toggle";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import type { UsageSummaryResponse } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import { APP_VERSION } from "@/lib/version";

export const NAV_ITEMS = [
  { href: "/projects", icon: "folder_open", labelKey: "rail.projects" },
  { href: "/usage", icon: "monitoring", labelKey: "rail.usage" },
  { href: "/settings", icon: "settings", labelKey: "rail.settings" },
] as const;

interface GlobalRailProps {
  initialUsage?: UsageSummaryResponse;
}

/**
 * GlobalRail — 56px instrument bezel (origin of the vertical axis)
 *
 * - quiet pier on bg-surface-container-low
 * - right edge = .edge-v (vertical axis origin; stands unbroken at full screen height)
 * - active: white icon (current location = white; not cyan)
 * - inactive: on-surface-variant
 * - bottom: cost(.data) / ●ONLINE (cyan dot + small label) / JA|EN (quiet, like an instrument)
 * - 44px hit target / tooltip / focus maintained
 */
export function GlobalRail({ initialUsage }: GlobalRailProps) {
  const pathname = usePathname();
  const t = useT();

  return (
    <nav
      aria-label={t("nav.globalNav")}
      className="fixed left-0 top-0 z-30 hidden h-full flex-col bg-surface-container-low edge-v md:flex"
      style={{ width: "var(--rail-w, 56px)" }}
    >
      {/* Brand mark */}
      <Link
        href="/projects"
        aria-label={t("nav.appHome")}
        title="yukar"
        className="flex h-14 w-full items-center justify-center text-on-surface-variant transition-colors hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface-container-low)]"
      >
        <Icon name="terminal" className="text-[22px]" />
      </Link>

      {/* tone boundary — separated by surface lightness contrast (no hard line) */}
      <div
        className="mx-3"
        style={{ height: "1px", background: "var(--edge-shadow)" }}
        aria-hidden
      />

      {/* Nav icons */}
      <div className="flex flex-1 flex-col items-center gap-1 py-3">
        {NAV_ITEMS.map((item) => {
          const active = pathname.startsWith(item.href);
          const label = t(item.labelKey);
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-label={label}
              title={label}
              className={cn(
                "relative flex h-11 w-11 items-center justify-center rounded transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface-container-low)]",
                active
                  ? // active: white icon (current location = white)
                    "text-on-surface"
                  : "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
              )}
            >
              <Icon name={item.icon} filled={active} className="text-[22px]" />
            </Link>
          );
        })}
      </div>

      {/* Footer: instrument stack */}
      <div className="flex flex-col items-center gap-1 pb-3 pt-2">
        {/* tone boundary */}
        <div
          className="mb-1 w-8"
          style={{ height: "1px", background: "var(--edge-shadow)" }}
          aria-hidden
        />

        {/* CostTicker */}
        {initialUsage && (
          <div className="w-full px-1">
            <CostTicker initialData={initialUsage} />
          </div>
        )}

        {/* ThemeToggle */}
        <ThemeToggle />

        {/* LanguageToggle */}
        <LanguageToggle />

        {/* ONLINE indicator — cyan dot + small label */}
        <div className="flex items-center gap-1" title={`yukar ${APP_VERSION}`}>
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ backgroundColor: "var(--color-light)" }}
            aria-hidden
          />
          <span
            className="font-mono uppercase"
            style={{
              fontSize: "9px",
              letterSpacing: "0.06em",
              color: "var(--color-on-surface-variant)",
            }}
          >
            ONLINE
          </span>
        </div>
      </div>
    </nav>
  );
}
