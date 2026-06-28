"use client";

import { Icon } from "@/components/icon";
import type { SearchResultItem } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";

// ---- types ----

export interface NavItem {
  id: string;
  label: string;
  sublabel?: string;
  href: string;
  icon: string;
  group: string;
}

// ---- NavRow ----

export function NavRow({
  item,
  isSelected,
  onSelect,
}: {
  item: NavItem;
  isSelected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={isSelected}
      onClick={onSelect}
      className={cn(
        "flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors",
        isSelected
          ? "bg-surface-container-highest text-on-surface"
          : "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
      )}
    >
      {/* Selection indicator: white tick */}
      <span className="flex w-4 shrink-0 items-center justify-center">
        {isSelected ? (
          <Icon name="check" className="text-[14px] text-on-surface" />
        ) : (
          <Icon name={item.icon} className="text-[14px] text-outline" />
        )}
      </span>

      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-[13px] font-medium leading-tight">{item.label}</span>
        {item.sublabel && (
          <span className="mt-0.5 truncate font-mono text-[11px] text-outline">
            {item.sublabel}
          </span>
        )}
      </span>

      <span className="shrink-0 font-mono text-[10px] text-outline/60">{item.group}</span>
    </button>
  );
}

// ---- SearchResultRow ----

export function SearchResultRow({
  item,
  isSelected,
  onSelect,
}: {
  item: SearchResultItem;
  isSelected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={isSelected}
      onClick={onSelect}
      className={cn(
        "flex w-full items-start gap-3 px-4 py-2.5 text-left transition-colors",
        isSelected
          ? "bg-surface-container-highest text-on-surface"
          : "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
      )}
    >
      <span className="flex w-4 shrink-0 items-center justify-center pt-0.5">
        {isSelected ? (
          <Icon name="check" className="text-[14px] text-on-surface" />
        ) : (
          <Icon name="code" className="text-[14px] text-outline" />
        )}
      </span>

      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-mono text-[12px] leading-tight text-on-surface">
          {item.path}
        </span>
        <span className="mt-0.5 truncate font-mono text-[11px] text-outline">
          {item.snippet.split("\n")[0]}
        </span>
      </span>

      <span className="flex shrink-0 items-center gap-2">
        <span className="inline-flex items-center rounded border border-outline-variant/50 bg-surface-container-high px-1.5 py-0.5 font-mono text-[10px] text-on-surface-variant">
          {item.repo}
        </span>
        <span className="font-mono text-[10px]" style={{ color: "var(--color-light)" }}>
          {(item.score * 100).toFixed(0)}%
        </span>
      </span>
    </button>
  );
}
