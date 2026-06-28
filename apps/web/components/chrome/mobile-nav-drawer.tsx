"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/icon";
import { LanguageToggle } from "@/components/ui/language-toggle";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import { NAV_ITEMS } from "./global-rail";

/** Height of the mobile top bar in px — kept in sync with layout padding-top */
export const MOBILE_TOPBAR_HEIGHT = 48;

interface MobileNavDrawerProps {
  /** Optional slot for additional controls (e.g. theme toggle added in T6) */
  extraControls?: React.ReactNode;
}

/**
 * MobileNavDrawer
 *
 * Mobile-only (`md:hidden`).
 * - Fixed top bar: hamburger button + app name
 * - Slide-in drawer from the left on hamburger press
 * - Close on overlay click / Esc
 * - Auto-close on route transition
 * - safe-area-inset support
 * - Respects prefers-reduced-motion
 */
export function MobileNavDrawer({ extraControls }: MobileNavDrawerProps) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const t = useT();
  const closeBtnRef = useRef<HTMLButtonElement>(null);

  // Close the drawer on route transition
  // biome-ignore lint/correctness/useExhaustiveDependencies: close on route change
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  // Close on Esc key
  useEffect(() => {
    if (!open) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setOpen(false);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  // When the drawer opens, focus the close button inside it
  useEffect(() => {
    if (open) {
      const id = setTimeout(() => {
        closeBtnRef.current?.focus();
      }, 50);
      return () => clearTimeout(id);
    }
  }, [open]);

  // Prevent body scroll while the drawer is open
  useEffect(() => {
    if (open) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  return (
    <>
      {/* ===== Mobile top bar (md:hidden) ===== */}
      <header
        className="fixed left-0 right-0 top-0 z-30 flex h-12 items-center bg-surface-container-low px-2 edge-h md:hidden"
        style={{
          paddingTop: "env(safe-area-inset-top)",
          paddingLeft: "max(env(safe-area-inset-left), 0.5rem)",
          paddingRight: "max(env(safe-area-inset-right), 0.5rem)",
        }}
      >
        {/* Hamburger button */}
        <button
          type="button"
          data-testid="hamburger-btn"
          aria-label={open ? t("mobileNav.closeMenu") : t("mobileNav.openMenu")}
          aria-expanded={open}
          aria-controls="mobile-nav-drawer"
          onClick={() => setOpen((v) => !v)}
          className={cn(
            "flex h-11 w-11 items-center justify-center rounded transition-colors",
            "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface-container-low)]",
          )}
        >
          <Icon name={open ? "close" : "menu"} className="text-[22px]" />
        </button>

        {/* App name */}
        <Link
          href="/projects"
          aria-label={t("nav.appHome")}
          className="ml-1 flex items-center gap-1.5 text-on-surface-variant transition-colors hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface-container-low)]"
        >
          <Icon name="terminal" className="text-[18px]" />
          <span className="font-mono text-sm font-semibold tracking-tight text-on-surface">
            {t("mobileNav.appName")}
          </span>
        </Link>
      </header>

      {/* ===== Overlay ===== */}
      {open && (
        <div
          data-testid="mobile-nav-overlay"
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
          aria-hidden="true"
          onClick={() => setOpen(false)}
        />
      )}

      {/* ===== Drawer ===== */}
      <div
        id="mobile-nav-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={t("mobileNav.openMenu")}
        className={cn(
          "fixed left-0 top-0 z-50 flex h-full w-72 flex-col bg-surface-container-low md:hidden",
          "transition-transform motion-reduce:transition-none",
          open ? "translate-x-0" : "-translate-x-full",
        )}
        style={{
          paddingTop: "env(safe-area-inset-top)",
          paddingBottom: "env(safe-area-inset-bottom)",
          paddingLeft: "env(safe-area-inset-left)",
        }}
      >
        {/* Drawer header */}
        <div className="flex h-12 items-center justify-between px-4 edge-h">
          <Link
            href="/projects"
            aria-label={t("nav.appHome")}
            className="flex items-center gap-2 text-on-surface-variant transition-colors hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
          >
            <Icon name="terminal" className="text-[20px]" />
            <span className="font-mono text-sm font-semibold tracking-tight text-on-surface">
              {t("mobileNav.appName")}
            </span>
          </Link>
          <button
            ref={closeBtnRef}
            type="button"
            aria-label={t("mobileNav.closeMenu")}
            onClick={() => setOpen(false)}
            className={cn(
              "flex h-11 w-11 items-center justify-center rounded transition-colors",
              "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white",
            )}
          >
            <Icon name="close" className="text-[22px]" />
          </button>
        </div>

        {/* Navigation links */}
        <nav aria-label={t("nav.globalNav")} className="flex flex-1 flex-col gap-1 px-2 py-3">
          {NAV_ITEMS.map((item) => {
            const active = pathname.startsWith(item.href);
            const label = t(item.labelKey);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-label={label}
                className={cn(
                  "flex h-11 items-center gap-3 rounded px-3 transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface-container-low)]",
                  active
                    ? "bg-surface-container-high text-on-surface"
                    : "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
                )}
              >
                <Icon name={item.icon} filled={active} className="text-[22px]" />
                <span className="text-sm font-medium">{label}</span>
              </Link>
            );
          })}
        </nav>

        {/* Drawer footer: LanguageToggle + slot for future theme toggle */}
        <div className="flex flex-col gap-3 px-4 py-4">
          {/* tone boundary */}
          <div
            className="w-full"
            style={{ height: "1px", background: "var(--edge-shadow)" }}
            aria-hidden
          />
          <div className="flex items-center gap-3">
            <LanguageToggle />
            {/* T6 theme toggle slot — gracefully handles missing extraControls */}
            {extraControls}
          </div>
        </div>
      </div>
    </>
  );
}
