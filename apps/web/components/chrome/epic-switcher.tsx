"use client";

/**
 * EpicSwitcher — clicking the nameplate [EP-id │ epic title] opens
 * a dropdown switcher for sibling Epics.
 *
 * - Trigger: <button aria-haspopup="listbox" aria-expanded>
 * - Dropdown: createPortal + position:fixed (not clipped by sticky header)
 * - View-preserving navigation: carries the current sub-path to the next epic
 *   threads/[t] → converted to threads/manager; others (tasks/diff/docs) are passed through as-is
 * - Keyboard: ↑↓/Enter/Esc, focus restoration
 * - a11y: aria-haspopup="listbox" / aria-expanded / role="listbox" / role="option"
 */

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import {
  type KeyboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Icon } from "@/components/icon";
import type { StatusValue } from "@/components/ui/status-badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { listEpics } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { isTerminalStatus } from "@/lib/epic-utils";
import { useT } from "@/lib/i18n/provider";
import { useEpicRun } from "./epic-run-context";

// ---- helpers ----

function resolveEpicStatus(status: string | undefined): StatusValue {
  switch (status) {
    case "in_progress":
      return "in_progress";
    case "completed":
      return "completed";
    case "merged":
      return "merged";
    case "closed":
      return "closed";
    case "failed":
      return "error";
    case "planned":
      return "planned";
    case "blocked":
      return "blocked";
    default:
      return "idle";
  }
}

/** Computes the URL for view-preserving navigation */
function buildTargetUrl(
  pathname: string,
  projectId: string,
  currentEpicId: string,
  targetEpicId: string,
  targetActiveThreadId?: string | null,
): string {
  const after = pathname.split(`/epics/${currentEpicId}/`)[1] ?? "";
  // Individual thread page (threads/[t]) → land on the active manager trial
  // tasks/diff/docs are passed through as-is
  let view: string;
  if (!after || after.startsWith("threads")) {
    // Use active_thread_id if known; otherwise fall back to "manager"
    const managerSeg = targetActiveThreadId ?? "manager";
    view = `threads/${managerSeg}`;
  } else {
    view = after;
  }
  return `/projects/${projectId}/epics/${targetEpicId}/${view}`;
}

// ---- dropdown portal ----

interface DropdownRect {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
  openUpward: boolean;
}

function calcDropdownRect(triggerEl: HTMLElement): DropdownRect {
  const rect = triggerEl.getBoundingClientRect();
  const vpH = window.innerHeight;
  const vpW = window.innerWidth;

  const DROPDOWN_MAX_H = 320;
  const DROPDOWN_W = 320;
  const MARGIN = 8;

  const spaceBelow = vpH - rect.bottom - MARGIN;
  const spaceAbove = rect.top - MARGIN;

  const openUpward = spaceBelow < DROPDOWN_MAX_H && spaceAbove > spaceBelow;

  let top: number;
  let maxHeight: number;

  if (openUpward) {
    maxHeight = Math.min(DROPDOWN_MAX_H, spaceAbove);
    top = rect.top - maxHeight - MARGIN;
  } else {
    top = rect.bottom + MARGIN;
    maxHeight = Math.min(DROPDOWN_MAX_H, spaceBelow);
  }

  // Flip at the right edge
  let left = rect.left;
  if (left + DROPDOWN_W > vpW - MARGIN) {
    left = Math.max(MARGIN, vpW - DROPDOWN_W - MARGIN);
  }

  return { top, left, width: DROPDOWN_W, maxHeight, openUpward };
}

interface EpicDropdownProps {
  currentEpicId: string;
  triggerEl: HTMLElement;
  onClose: () => void;
  onSelect: (epicId: string) => void;
  filterValue: string;
  onFilterChange: (v: string) => void;
  selectedIndex: number;
  onSelectedIndexChange: (i: number) => void;
  epicOptions: Array<{
    id: string;
    title: string;
    status?: string;
    active_thread_id?: string | null;
  }>;
  isLoading: boolean;
}

function EpicDropdown({
  currentEpicId,
  triggerEl,
  onClose,
  onSelect,
  filterValue,
  onFilterChange,
  selectedIndex,
  onSelectedIndexChange,
  epicOptions,
  isLoading,
}: EpicDropdownProps) {
  const t = useT();
  const filterRef = useRef<HTMLInputElement>(null);
  const [rect, setRect] = useState<DropdownRect>(() => calcDropdownRect(triggerEl));

  // Recalculate position on window resize / scroll
  useLayoutEffect(() => {
    function update() {
      setRect(calcDropdownRect(triggerEl));
    }
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [triggerEl]);

  // Initial focus on the filter input
  useEffect(() => {
    const timer = setTimeout(() => filterRef.current?.focus(), 30);
    return () => clearTimeout(timer);
  }, []);

  // Close on click outside
  useEffect(() => {
    function handler(e: MouseEvent) {
      const target = e.target as Node;
      if (!triggerEl.contains(target)) {
        // Allow clicks inside the dropdown (mounted directly on document as a portal)
        const portal = document.getElementById("epic-switcher-portal-root");
        if (!portal?.contains(target)) {
          onClose();
        }
      }
    }
    // Handle in capture phase first
    document.addEventListener("mousedown", handler, true);
    return () => document.removeEventListener("mousedown", handler, true);
  }, [triggerEl, onClose]);

  const filtered = epicOptions
    .filter((e) =>
      filterValue
        ? e.title.toLowerCase().includes(filterValue.toLowerCase()) ||
          e.id.toLowerCase().includes(filterValue.toLowerCase())
        : true,
    )
    // Active epics first, terminal (closed/merged) at the bottom
    .sort((a, b) => {
      const aT = isTerminalStatus(a.status) ? 1 : 0;
      const bT = isTerminalStatus(b.status) ? 1 : 0;
      return aT - bT;
    });

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      onSelectedIndexChange(Math.min(selectedIndex + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      onSelectedIndexChange(Math.max(selectedIndex - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = filtered[selectedIndex];
      if (item && item.id !== currentEpicId) onSelect(item.id);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  }

  const content = (
    <div
      id="epic-switcher-portal-root"
      style={{
        position: "fixed",
        top: rect.top,
        left: rect.left,
        width: rect.width,
        zIndex: 9999,
      }}
    >
      <div
        role="listbox"
        aria-label={t("epicSwitcher.switchEpic")}
        onKeyDown={handleKeyDown}
        style={{
          backgroundColor: "var(--color-surface-container-highest)",
          boxShadow: "0 8px 32px 0 rgba(0,0,0,0.7)",
          border: "1px solid var(--color-outline-variant)",
          maxHeight: rect.maxHeight,
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* Filter input */}
        <div className="edge-h shrink-0 flex items-center gap-2 px-3 py-2">
          <Icon name="search" className="shrink-0 text-[14px] text-outline" />
          <input
            ref={filterRef}
            type="text"
            value={filterValue}
            onChange={(e) => {
              onFilterChange(e.target.value);
              onSelectedIndexChange(0);
            }}
            placeholder={t("epicSwitcher.filterPlaceholder")}
            className="flex-1 bg-transparent text-[13px] text-on-surface placeholder:text-outline focus:outline-none"
          />
        </div>

        {/* Option list */}
        <div className="overflow-y-auto">
          {isLoading && (
            <p className="px-3 py-4 text-center font-mono text-[11px] text-outline">
              {t("epicSwitcher.loadingEpics")}
            </p>
          )}
          {!isLoading && filtered.length === 0 && (
            <p className="px-3 py-4 text-center font-mono text-[11px] text-outline">
              {t("epicSwitcher.noEpics")}
            </p>
          )}
          {filtered.map((epic, idx) => {
            const isCurrent = epic.id === currentEpicId;
            const isSelected = idx === selectedIndex;
            const status = resolveEpicStatus(epic.status);
            const isTerminal = isTerminalStatus(epic.status);

            return (
              <button
                type="button"
                key={epic.id}
                role="option"
                aria-selected={isCurrent}
                onClick={() => {
                  if (!isCurrent) onSelect(epic.id);
                }}
                className={cn(
                  "flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors",
                  isTerminal && !isCurrent ? "opacity-50" : undefined,
                  isSelected && !isCurrent
                    ? "bg-surface-container-highest text-on-surface"
                    : isCurrent
                      ? "text-on-surface"
                      : "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
                )}
              >
                {/* Current epic gets a white tick */}
                <span className="flex w-4 shrink-0 items-center justify-center">
                  {isCurrent ? (
                    <Icon name="check" className="text-[14px] text-on-surface" />
                  ) : (
                    <StatusBadge status={status} className="pointer-events-none" />
                  )}
                </span>

                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-[13px] font-medium leading-tight">
                    {epic.title}
                  </span>
                  <span className="mt-0.5 font-mono text-[11px] text-outline">{epic.id}</span>
                </span>

                {isCurrent && (
                  <span className="shrink-0 font-mono text-[10px] text-outline">
                    {t("epicSwitcher.current")}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );

  if (typeof document === "undefined") return null;
  return createPortal(content, document.body);
}

// ---- main component ----

export function EpicSwitcher() {
  const t = useT();
  const router = useRouter();
  const pathname = usePathname();
  const { projectId, epicId, epic } = useEpicRun();

  const [open, setOpen] = useState(false);
  const [filterValue, setFilterValue] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const { data: epicsData, isFetching } = useQuery({
    queryKey: queryKeys.epics.list(projectId),
    queryFn: () => listEpics(projectId),
    enabled: open,
    staleTime: 30_000,
  });

  const epicOptions = epicsData ?? [];

  const handleOpen = useCallback(() => {
    setFilterValue("");
    setSelectedIndex(0);
    setOpen(true);
  }, []);

  const handleClose = useCallback(() => {
    setOpen(false);
    // Restore focus to trigger after Esc/close
    requestAnimationFrame(() => triggerRef.current?.focus());
  }, []);

  const handleSelect = useCallback(
    (targetEpicId: string) => {
      const targetEpic = epicOptions.find((e) => e.id === targetEpicId);
      const url = buildTargetUrl(
        pathname,
        projectId,
        epicId,
        targetEpicId,
        targetEpic?.active_thread_id,
      );
      setOpen(false);
      router.push(url);
    },
    [pathname, projectId, epicId, router, epicOptions],
  );

  // Close on Esc key (even outside the dropdown)
  useEffect(() => {
    if (!open) return;
    function handler(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        handleClose();
      }
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, handleClose]);

  return (
    <>
      {/* Trigger: [EP-id │ title ∨] */}
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${t("epicSwitcher.switchEpic")}: ${epicId}`}
        onClick={handleOpen}
        className={cn(
          "flex min-w-0 items-center gap-1 overflow-hidden rounded px-1 py-0.5 transition-colors",
          "hover:bg-surface-container focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface)]",
          open && "bg-surface-container",
        )}
      >
        {/* EP-id (mono) */}
        <span
          className="shrink-0 font-mono tabular-nums"
          style={{
            fontSize: "13px",
            lineHeight: "1",
            fontWeight: 500,
            letterSpacing: "0.04em",
            color: "var(--color-on-surface-variant)",
          }}
        >
          {epicId}
        </span>

        {/* Vertical rule */}
        <span
          aria-hidden="true"
          className="mx-2 h-6 w-px shrink-0"
          style={{ backgroundColor: "var(--color-outline-variant)" }}
        />

        {/* epic title */}
        <span
          className="min-w-0 line-clamp-1 font-sans font-semibold text-on-surface text-[17px] leading-[24px] md:line-clamp-2 md:text-[length:var(--text-title,26px)] md:leading-[length:var(--text-title--line-height,32px)]"
          style={{
            letterSpacing: "var(--text-title--letter-spacing, -0.02em)",
            wordBreak: "break-word",
          }}
        >
          {epic?.title ?? epicId}
        </span>

        {/* expand_more glyph: suggests the item is switchable */}
        <Icon
          name={open ? "expand_less" : "expand_more"}
          className={cn(
            "ml-1 shrink-0 text-[18px] transition-colors",
            open ? "text-on-surface" : "text-outline",
          )}
          aria-hidden="true"
        />
      </button>

      {/* Dropdown (portal) */}
      {open && triggerRef.current && (
        <EpicDropdown
          currentEpicId={epicId}
          triggerEl={triggerRef.current}
          onClose={handleClose}
          onSelect={handleSelect}
          filterValue={filterValue}
          onFilterChange={setFilterValue}
          selectedIndex={selectedIndex}
          onSelectedIndexChange={setSelectedIndex}
          epicOptions={epicOptions}
          isLoading={isFetching && epicOptions.length === 0}
        />
      )}
    </>
  );
}
