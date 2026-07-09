"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type KeyboardEvent, useRef } from "react";
import { cn } from "@/lib/cn";

export interface TabItem {
  href: string;
  label: string;
  badge?: React.ReactNode;
  /** URL segment (matched via pathname.includes). Falls back to exact match against href when omitted */
  segment?: string;
}

interface TabBarProps {
  items: TabItem[];
  className?: string;
}

/**
 * TabBar — bottom datum (.edge-h). The tonal contrast between surfaces creates a horizon line.
 * active = white text + 2px white under-tick (current location = white; no cyan).
 * count uses .data mono.
 * Roving tabindex + left/right arrow key navigation + visible focus ring. 44px height.
 *
 * Mobile support:
 * - `overflow-x-auto` enables horizontal scroll when tabs overflow.
 * - Scrollbar is hidden (`scrollbar-none` / webkit settings).
 * - Each tab uses `shrink-0` to prevent collapsing.
 * - On desktop (md:) the traditional `flex` layout is used.
 */
export function TabBar({ items, className }: TabBarProps) {
  const pathname = usePathname();
  const tabRefs = useRef<(HTMLAnchorElement | null)[]>([]);

  function isActive(item: TabItem): boolean {
    if (item.segment) {
      return pathname.includes(`/${item.segment}`);
    }
    return pathname === item.href;
  }

  const activeIndex = items.findIndex((item) => isActive(item));

  function handleKeyDown(e: KeyboardEvent<HTMLAnchorElement>, index: number) {
    let next = index;
    if (e.key === "ArrowRight") {
      next = (index + 1) % items.length;
    } else if (e.key === "ArrowLeft") {
      next = (index - 1 + items.length) % items.length;
    } else {
      return;
    }
    e.preventDefault();
    tabRefs.current[next]?.focus();
  }

  return (
    <div
      className={cn(
        // Mobile: horizontal scroll enabled, scrollbar hidden
        // shrink-0: keep the 44px height when placed inside a column flex that overflows
        "flex h-11 shrink-0 items-end edge-h",
        "overflow-x-auto",
        "[&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none]",
        className,
      )}
    >
      {items.map((item, i) => {
        const active = isActive(item);
        return (
          <Link
            key={item.href}
            ref={(el) => {
              tabRefs.current[i] = el;
            }}
            href={item.href}
            aria-current={active ? "page" : undefined}
            tabIndex={activeIndex === -1 ? (i === 0 ? 0 : -1) : active ? 0 : -1}
            onKeyDown={(e) => handleKeyDown(e, i)}
            className={cn(
              // shrink-0: prevent tabs from collapsing on mobile
              "inline-flex h-11 shrink-0 items-center gap-1.5 whitespace-nowrap px-4 text-label font-medium uppercase tracking-[0.05em] transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface)]",
              // active: white text + white 2px under-tick (current location = white)
              active ? "text-on-surface" : "text-on-surface-variant hover:text-on-surface",
            )}
            style={active ? { boxShadow: "inset 0 -2px 0 0 var(--color-on-surface)" } : undefined}
          >
            {item.label}
            {item.badge && <span className="data">{item.badge}</span>}
          </Link>
        );
      })}
    </div>
  );
}
