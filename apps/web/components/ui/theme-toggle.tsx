"use client";

import { useEffect, useState } from "react";
import { Icon } from "@/components/icon";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import { useTheme } from "@/lib/theme/use-theme";

/**
 * ThemeToggle — light/dark theme toggle button.
 * A small toggle similar to LanguageToggle.
 * - light theme: sun icon
 * - dark theme: moon icon
 * - minimum 44px tap target
 * - has aria-label
 *
 * Before mounting, show a neutral dark-assumed display that matches SSR (avoids hydration mismatch).
 * After useEffect (client mount complete), reflect the actual theme.
 */
export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const t = useT();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Before mount: neutral dark-assumed display matching SSR (aria-label/icon fixed)
  const isDark = mounted ? theme === "dark" : true;

  return (
    <button
      type="button"
      aria-label={isDark ? t("common.theme.switchToLight") : t("common.theme.switchToDark")}
      title={isDark ? t("common.theme.switchToLight") : t("common.theme.switchToDark")}
      onClick={toggleTheme}
      className={cn(
        "flex h-11 w-11 items-center justify-center rounded transition-colors",
        "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface-container-low)]",
      )}
    >
      <Icon name={isDark ? "light_mode" : "dark_mode"} className="text-[22px]" />
    </button>
  );
}
