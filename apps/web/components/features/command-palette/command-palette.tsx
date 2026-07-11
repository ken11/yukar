"use client";

/**
 * CommandPalette — global ⌘K command palette (2 modes)
 *
 * - Nav mode (default): fuzzy-filtered destination list. Enter triggers router.push.
 * - Code search mode: activated when the query starts with ">". Reuses existing searchCodebase.
 *
 * Mount: place exactly one instance inside <I18nProvider> in app/layout.tsx.
 * Open/close: global keydown ⌘K/Ctrl+K + custom event yukar:open-palette toggle.
 *
 * Design: container-highest + sole sharp shadow. Shadow separator (.edge-h),
 * items are rows (not cards). Current location/selection uses white tick. Cyan only for selected/live.
 * focus-trap / Esc focus-return / aria-hidden on background while open / reduced-motion support.
 */

import * as RadixDialog from "@radix-ui/react-dialog";
import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/icon";
import { listEpics, listProjects, searchCodebase } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { isTerminalStatus } from "@/lib/epic-utils";
import { useDebounce } from "@/lib/hooks/use-debounce";
import { useT } from "@/lib/i18n/provider";
import type { NavItem } from "./palette-rows";
import { NavRow, SearchResultRow } from "./palette-rows";

// ---- types ----

type PaletteMode = "nav" | "search";

// ---- helpers ----

/** Parse projectId / epicId from /projects/[p]/epics/[e]/... */
function parsePathContext(pathname: string): { projectId: string | null; epicId: string | null } {
  const m = pathname.match(/^\/projects\/([^/]+)(?:\/epics\/([^/]+))?/);
  if (!m) return { projectId: null, epicId: null };
  return { projectId: m[1] ?? null, epicId: m[2] ?? null };
}

/** Fuzzy filter (case-insensitive, substring match) */
function fuzzyMatch(text: string, query: string): boolean {
  if (!query) return true;
  const lq = query.toLowerCase();
  const lt = text.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < lt.length && qi < lq.length; ti++) {
    if (lt[ti] === lq[qi]) qi++;
  }
  return qi === lq.length;
}

// ---- main palette ----

export function CommandPalette() {
  const t = useT();
  const router = useRouter();
  const pathname = usePathname();
  const { projectId } = parsePathContext(pathname);

  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<PaletteMode>("nav");
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<Element | null>(null);

  // Whether code search mode is active: query starts with ">"
  const isSearchMode = mode === "search" || query.startsWith(">");
  const searchQuery = query.startsWith(">") ? query.slice(1).trimStart() : query;
  const debouncedSearchQuery = useDebounce(searchQuery.trim(), 300);

  // ---- data fetching ----

  const { data: projectsData } = useQuery({
    queryKey: queryKeys.projects.list(),
    queryFn: listProjects,
    enabled: open && !isSearchMode,
    staleTime: 60_000,
  });

  const { data: epicsData } = useQuery({
    queryKey: queryKeys.epics.list(projectId ?? ""),
    // includeCompleted=true: the epics.list query key is shared with the board
    // (which fetches all epics) — a completed-less fetch here would clobber
    // that cache. Completed epics sort to the bottom of the palette anyway.
    queryFn: () => listEpics(projectId ?? "", true),
    enabled: open && !!projectId && !isSearchMode,
    staleTime: 30_000,
  });

  const { data: searchData, isFetching: isSearchFetching } = useQuery({
    // #18: consolidate key with queryKeys.search.results
    queryKey: queryKeys.search.results(projectId ?? "", debouncedSearchQuery),
    queryFn: () =>
      searchCodebase(projectId ?? "", {
        query: debouncedSearchQuery,
        top_k: 8,
      }),
    enabled: open && !!projectId && isSearchMode && debouncedSearchQuery.length >= 2,
    staleTime: 30_000,
  });

  // ---- nav items ----

  const navItems = useMemo<NavItem[]>(() => {
    if (isSearchMode) return [];

    const items: NavItem[] = [];
    const g = t("commandPalette.globalHeading");
    const pHeading = t("commandPalette.projectHeading");
    const eHeading = t("commandPalette.epicsHeading");

    // Global destinations
    items.push({
      id: "nav-projects",
      label: t("commandPalette.projects"),
      href: "/projects",
      icon: "folder",
      group: g,
    });
    items.push({
      id: "nav-usage",
      label: t("commandPalette.usage"),
      href: "/usage",
      icon: "bar_chart",
      group: g,
    });
    items.push({
      id: "nav-settings",
      label: t("commandPalette.settings"),
      href: "/settings",
      icon: "settings",
      group: g,
    });

    // Project-scoped destinations
    if (projectId) {
      items.push({
        id: "nav-project-overview",
        label: t("commandPalette.projectOverview"),
        href: `/projects/${projectId}`,
        icon: "home",
        group: pHeading,
      });
      items.push({
        id: "nav-project-epics",
        label: t("commandPalette.projectEpics"),
        href: `/projects/${projectId}/epics`,
        icon: "rocket_launch",
        group: pHeading,
      });
      items.push({
        id: "nav-project-docs",
        label: t("commandPalette.projectDocs"),
        href: `/projects/${projectId}/docs`,
        icon: "description",
        group: pHeading,
      });
      items.push({
        id: "nav-project-repos",
        label: t("commandPalette.projectRepos"),
        href: `/projects/${projectId}/repos`,
        icon: "source",
        group: pHeading,
      });
      items.push({
        id: "nav-project-settings",
        label: t("commandPalette.projectSettings"),
        href: `/projects/${projectId}/settings`,
        icon: "tune",
        group: pHeading,
      });
    }

    // Epic list — place completed epics at the end (de-emphasize finished work)
    if (projectId && epicsData) {
      // #5: unify completed detection with isTerminalStatus()
      const activeEpics = epicsData.filter((e) => !isTerminalStatus(e.status));
      const terminalEpics = epicsData.filter((e) => isTerminalStatus(e.status));
      for (const epic of [...activeEpics, ...terminalEpics]) {
        items.push({
          id: `nav-epic-${epic.id}`,
          label: epic.title,
          sublabel: epic.id,
          href: `/projects/${projectId}/epics/${epic.id}/threads/${epic.active_thread_id ?? "manager"}`,
          icon: "bolt",
          group: eHeading,
        });
      }
    }

    // Project switcher (when multiple projects exist)
    if (projectsData && projectsData.length > 1) {
      for (const proj of projectsData) {
        if (proj.id === projectId) continue;
        items.push({
          id: `nav-switch-proj-${proj.id}`,
          label: proj.name,
          sublabel: t("commandPalette.switchProject"),
          href: `/projects/${proj.id}`,
          icon: "swap_horiz",
          group: t("commandPalette.switchProject"),
        });
      }
    }

    return items;
  }, [isSearchMode, projectId, epicsData, projectsData, t]);

  // Fuzzy filter
  const filteredNavItems = useMemo(() => {
    if (!query || query.startsWith(">")) return navItems;
    return navItems.filter(
      (item) =>
        fuzzyMatch(item.label, query) || (item.sublabel ? fuzzyMatch(item.sublabel, query) : false),
    );
  }, [navItems, query]);

  const searchResults = searchData?.results ?? [];

  // Reset selection index
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset on query/mode change
  useEffect(() => {
    setSelectedIndex(0);
  }, [query, mode]);

  // ---- open/close ----

  const openPalette = useCallback((initialMode: PaletteMode = "nav") => {
    triggerRef.current = document.activeElement;
    setMode(initialMode);
    setQuery(initialMode === "search" ? ">" : "");
    setSelectedIndex(0);
    setOpen(true);
  }, []);

  const closePalette = useCallback(() => {
    setOpen(false);
    // Return focus to the caller after Escape
    const el = triggerRef.current;
    if (el && "focus" in el) {
      requestAnimationFrame(() => (el as HTMLElement).focus());
    }
  }, []);

  // Global keybinding ⌘K / Ctrl+K
  useEffect(() => {
    function handler(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        if (open) {
          closePalette();
        } else {
          openPalette("nav");
        }
      }
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, openPalette, closePalette]);

  // Custom event yukar:open-palette
  useEffect(() => {
    function handler(e: Event) {
      const detail = (e as CustomEvent<{ mode?: PaletteMode }>).detail;
      openPalette(detail?.mode ?? "nav");
    }
    window.addEventListener("yukar:open-palette", handler);
    return () => window.removeEventListener("yukar:open-palette", handler);
  }, [openPalette]);

  // focus input on open
  useEffect(() => {
    if (open) {
      const timer = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(timer);
    }
  }, [open]);

  // ---- keyboard nav ----

  const totalItems = isSearchMode ? searchResults.length : filteredNavItems.length;

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((i) => Math.min(i + 1, totalItems - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (!isSearchMode) {
        const item = filteredNavItems[selectedIndex];
        if (item) {
          router.push(item.href);
          closePalette();
        }
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      closePalette();
    }
  }

  // Switch to search mode when query starts with ">"
  function handleQueryChange(value: string) {
    setQuery(value);
    if (value.startsWith(">") && mode !== "search") {
      setMode("search");
    } else if (!value.startsWith(">") && mode === "search" && !value) {
      setMode("nav");
    }
  }

  // ---- group headers for nav mode ----
  const groups = useMemo(() => {
    const seen = new Map<string, NavItem[]>();
    for (const item of filteredNavItems) {
      if (!seen.has(item.group)) seen.set(item.group, []);
      seen.get(item.group)?.push(item);
    }
    return seen;
  }, [filteredNavItems]);

  return (
    <RadixDialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!v) closePalette();
      }}
    >
      <RadixDialog.Portal>
        {/* Overlay: while open, Radix Dialog automatically applies aria-hidden to background content */}
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />

        {/* Panel: container-highest + sole sharp shadow */}
        <RadixDialog.Content
          aria-label={isSearchMode ? t("commandPalette.modeSearch") : t("commandPalette.modeNav")}
          aria-describedby={undefined}
          onKeyDown={handleKeyDown}
          style={{
            backgroundColor: "var(--color-surface-container-highest)",
            boxShadow: "0 20px 60px 0 rgba(0,0,0,0.8)",
          }}
          className="fixed left-1/2 top-[10vh] z-50 w-full max-w-2xl -translate-x-1/2 rounded-none border border-outline-variant/50"
        >
          <RadixDialog.Title className="sr-only">
            {isSearchMode ? t("commandPalette.modeSearch") : t("commandPalette.modeNav")}
          </RadixDialog.Title>
          <RadixDialog.Description className="sr-only">
            {isSearchMode ? t("commandPalette.searchDesc") : t("commandPalette.navDesc")}
          </RadixDialog.Description>

          {/* Input row — datum line via edge-h */}
          <div className="edge-h flex items-center gap-3 px-4 py-3">
            {/* Mode icon / spinner */}
            {isSearchMode && isSearchFetching ? (
              <Icon name="sync" className="shrink-0 animate-spin text-[18px] text-outline" />
            ) : isSearchMode ? (
              <Icon name="search" className="shrink-0 text-[18px] text-[var(--color-light)]" />
            ) : (
              <Icon name="terminal" className="shrink-0 text-[18px] text-outline" />
            )}

            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => handleQueryChange(e.target.value)}
              placeholder={
                isSearchMode
                  ? t("commandPalette.searchPlaceholder")
                  : t("commandPalette.placeholder")
              }
              className="flex-1 bg-transparent text-[14px] text-on-surface placeholder:text-outline focus:outline-none"
              aria-autocomplete="list"
              aria-controls="command-palette-listbox"
            />

            {/* Mode chip */}
            <span
              className={cn(
                "hidden shrink-0 items-center gap-1 rounded border px-2 py-0.5 font-mono text-[10px] sm:inline-flex",
                isSearchMode
                  ? "border-outline-variant/60 text-on-surface-variant"
                  : "border-transparent text-outline",
              )}
            >
              {isSearchMode ? t("commandPalette.modeSearch") : t("commandPalette.searchHint")}
            </span>

            <kbd className="hidden rounded border border-outline-variant/50 px-1.5 py-0.5 font-mono text-[10px] text-outline sm:block">
              ESC
            </kbd>
          </div>

          {/* Content */}
          <div
            id="command-palette-listbox"
            role="listbox"
            aria-label={isSearchMode ? t("commandPalette.modeSearch") : t("commandPalette.modeNav")}
            className="max-h-[60vh] overflow-y-auto py-1"
          >
            {/* === Code search mode === */}
            {isSearchMode && (
              <>
                {!projectId && (
                  <p className="px-4 py-8 text-center text-[13px] text-outline">
                    {t("commandPalette.noProjectContext")}
                  </p>
                )}

                {projectId && debouncedSearchQuery.length < 2 && (
                  <p className="px-4 py-8 text-center text-[13px] text-outline">
                    {t("commandPalette.searchPlaceholder")}
                  </p>
                )}

                {projectId &&
                  debouncedSearchQuery.length >= 2 &&
                  !isSearchFetching &&
                  searchResults.length === 0 && (
                    <p className="px-4 py-8 text-center text-[13px] text-outline">
                      No results found.
                    </p>
                  )}

                {searchResults.map((item, i) => (
                  <SearchResultRow
                    key={`${item.repo}:${item.path}:${item.start_line}`}
                    item={item}
                    isSelected={i === selectedIndex}
                    onSelect={() => setSelectedIndex(i)}
                  />
                ))}
              </>
            )}

            {/* === Nav mode === */}
            {!isSearchMode && (
              <>
                {filteredNavItems.length === 0 && (
                  <p className="px-4 py-8 text-center text-[13px] text-outline">No results.</p>
                )}

                {Array.from(groups.entries()).map(([groupName, items]) => (
                  <div key={groupName}>
                    {/* Group header */}
                    <div className="px-4 pb-1 pt-3">
                      <span className="font-mono text-[10px] font-medium uppercase tracking-[0.05em] text-outline">
                        {groupName}
                      </span>
                    </div>

                    {items.map((item) => {
                      const flatIdx = filteredNavItems.indexOf(item);
                      return (
                        <NavRow
                          key={item.id}
                          item={item}
                          isSelected={flatIdx === selectedIndex}
                          onSelect={() => {
                            router.push(item.href);
                            closePalette();
                          }}
                        />
                      );
                    })}
                  </div>
                ))}
              </>
            )}
          </div>

          {/* Footer: keyboard hint */}
          {totalItems > 0 && (
            <div
              className="edge-h flex items-center gap-4 px-4 py-2"
              style={{
                boxShadow: "0 -1px 0 0 var(--edge-shadow), inset 0 1px 0 0 var(--edge-lit)",
              }}
            >
              <span className="font-mono text-[10px] text-outline">
                {t("commandPalette.keyboardHint")}
              </span>
              {!isSearchMode && (
                <span className="font-mono text-[10px]" style={{ color: "var(--color-light)" }}>
                  {">"} {t("commandPalette.searchHint")}
                </span>
              )}
            </div>
          )}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
