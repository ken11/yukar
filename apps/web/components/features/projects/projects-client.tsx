"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { NewProjectModal } from "@/components/features/projects/new-project-modal";
import { Icon } from "@/components/icon";
import { StatusBadge } from "@/components/ui/status-badge";
import { deleteProject, listProjects, type Project } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useLocale, useT } from "@/lib/i18n/provider";

// ---- Row menu ----

function ProjectRowMenu({ project }: { project: Project }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties>({});
  const qc = useQueryClient();
  const triggerRef = useRef<HTMLButtonElement>(null);

  // Compute fixed position from trigger's bounding rect
  function openMenu() {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;

    const menuWidth = 148;
    const menuHeight = 148; // approximate
    const viewportW = window.innerWidth;
    const viewportH = window.innerHeight;

    // Horizontal: prefer right-align to trigger, flip left if too close to right edge
    const rightAligned = rect.right - menuWidth;
    const left = rightAligned < 0 ? rect.left : rightAligned;

    // Vertical: prefer below trigger, flip above if near bottom
    const belowTop = rect.bottom + 4;
    const aboveTop = rect.top - menuHeight - 4;
    const top = belowTop + menuHeight > viewportH && aboveTop > 0 ? aboveTop : belowTop;

    setMenuStyle({
      position: "fixed",
      top,
      left: Math.max(4, Math.min(left, viewportW - menuWidth - 4)),
      zIndex: 9999,
      minWidth: menuWidth,
    });
    setOpen(true);
  }

  // Escape closes the menu and returns focus to the trigger
  useEffect(() => {
    if (!open) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  const deleteMutation = useMutation({
    mutationFn: () => deleteProject(project.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.projects.list() });
      setOpen(false);
    },
  });

  const menu = open ? (
    <>
      {/* backdrop dismiss */}
      {/* biome-ignore lint/a11y/noStaticElementInteractions: backdrop dismiss overlay */}
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: backdrop dismiss overlay */}
      <div
        className="fixed inset-0"
        style={{ zIndex: 9998 }}
        onClick={(e) => {
          e.stopPropagation();
          setOpen(false);
        }}
      />
      <div
        className="rounded border border-outline-variant bg-surface-container-highest py-1 shadow-[0_4px_16px_0_rgba(0,0,0,0.4)]"
        style={menuStyle}
        role="menu"
        aria-label={t("projects.projectMenu")}
      >
        <Link
          href={`/projects/${project.id}`}
          onClick={() => setOpen(false)}
          role="menuitem"
          className="flex w-full items-center gap-2.5 px-3 py-2 text-[13px] text-on-surface transition-colors hover:bg-container-high"
        >
          <Icon name="open_in_new" className="text-[14px] text-on-surface-variant" />
          {t("projects.menu.open")}
        </Link>
        <Link
          href={`/projects/${project.id}/settings`}
          onClick={() => setOpen(false)}
          role="menuitem"
          className="flex w-full items-center gap-2.5 px-3 py-2 text-[13px] text-on-surface transition-colors hover:bg-container-high"
        >
          <Icon name="settings" className="text-[14px] text-on-surface-variant" />
          {t("projects.menu.settings")}
        </Link>
        <div className="my-1 border-t border-outline-variant/30" />
        <button
          type="button"
          role="menuitem"
          onClick={(e) => {
            e.stopPropagation();
            deleteMutation.mutate();
          }}
          disabled={deleteMutation.isPending}
          className="flex w-full items-center gap-2.5 px-3 py-2 text-[13px] text-error transition-colors hover:bg-error/10 disabled:opacity-50"
        >
          <Icon name="delete" className="text-[14px]" />
          {deleteMutation.isPending ? t("projects.menu.deleting") : t("projects.menu.delete")}
        </button>
      </div>
    </>
  ) : null;

  return (
    <div>
      <button
        ref={triggerRef}
        type="button"
        aria-label={t("projects.rowMenuLabel")}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          if (open) {
            setOpen(false);
          } else {
            openMenu();
          }
        }}
        className="flex h-8 w-8 items-center justify-center rounded text-on-surface-variant transition-colors hover:bg-container-high hover:text-on-surface focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
      >
        <Icon name="more_horiz" className="text-[18px]" />
      </button>

      {typeof document !== "undefined" && menu ? createPortal(menu, document.body) : null}
    </div>
  );
}

// ---- Single project row ----

function ProjectRow({ project }: { project: Project }) {
  const repos = project.repos ?? [];
  const primaryRepo = repos[0];
  const locale = useLocale();

  const statusValue: "active" | "idle" | "archived" =
    project.status === "active" ? "active" : "idle";

  const updatedAt = project.updated_at
    ? new Date(project.updated_at).toLocaleDateString(locale === "ja" ? "ja-JP" : "en-US", {
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : "—";

  return (
    <div
      data-testid={`project-row-${project.id}`}
      className="group relative flex items-center gap-3 px-3 py-4 transition-colors hover:bg-surface-container-high edge-h md:gap-4 md:px-6"
    >
      {/* Project name — primary focus */}
      <Link
        href={`/projects/${project.id}`}
        className="min-w-0 flex-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
        tabIndex={0}
      >
        <span className="block text-[15px] font-semibold leading-snug text-on-surface transition-colors group-hover:text-white">
          {project.name}
        </span>
        <span
          className={cn(
            "data mt-0.5 block truncate",
            primaryRepo ? "text-on-surface-variant" : "text-outline",
          )}
          title={primaryRepo}
        >
          {primaryRepo ?? "—"}
        </span>
      </Link>

      {/* Status — glyph+label, no color alone */}
      <div className="shrink-0 md:w-28">
        <StatusBadge status={statusValue} />
      </div>

      {/* Updated — hidden on mobile */}
      <div className="hidden w-28 shrink-0 text-right md:block">
        <span className="data">{updatedAt}</span>
      </div>

      {/* Context menu */}
      <div className="shrink-0">
        <ProjectRowMenu project={project} />
      </div>
    </div>
  );
}

// ---- Table header ----

function ProjectListHeader() {
  const t = useT();
  return (
    <div className="flex items-center gap-3 px-3 pb-2 pt-1 md:gap-4 md:px-6">
      <div className="min-w-0 flex-1">
        <span className="label uppercase text-outline">{t("projects.columns.project")}</span>
      </div>
      <div className="shrink-0 md:w-28">
        <span className="label uppercase text-outline">{t("projects.columns.status")}</span>
      </div>
      <div className="hidden w-28 shrink-0 text-right md:block">
        <span className="label uppercase text-outline">{t("projects.columns.updated")}</span>
      </div>
      {/* spacer for menu column */}
      <div className="w-8 shrink-0" />
    </div>
  );
}

// ---- Empty state ----

function ProjectsEmptyState() {
  const t = useT();
  return (
    <div
      className="flex flex-col items-start px-4 py-12 md:px-6 md:py-16"
      style={{ gap: "var(--spacing-void, 40px)" }}
    >
      <div>
        <p className="font-mono text-[13px] font-medium text-on-surface-variant">
          {t("projects.noProjects")}
        </p>
        <p className="mt-2 text-[14px] leading-relaxed text-outline">
          {t("projects.noProjectsMessage")}
        </p>
      </div>
      <NewProjectModal
        trigger={
          <button
            type="button"
            className="flex items-center gap-2 rounded border border-outline-variant bg-transparent px-4 py-2 text-[13px] text-on-surface-variant transition-colors hover:border-outline hover:text-on-surface focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
          >
            <Icon name="add" className="text-[16px]" />
            {t("projects.registerExisting")}
          </button>
        }
      />
    </div>
  );
}

// ---- Main ----

interface ProjectsClientProps {
  initialProjects: Project[];
}

export function ProjectsClient({ initialProjects }: ProjectsClientProps) {
  const { data: projects = initialProjects } = useQuery({
    queryKey: queryKeys.projects.list(),
    queryFn: listProjects,
    initialData: initialProjects,
    staleTime: 30_000,
  });

  if (projects.length === 0) {
    return <ProjectsEmptyState />;
  }

  return (
    <div
      className="w-full rounded-none"
      style={{
        boxShadow:
          "0 1px 0 0 var(--edge-shadow, rgba(0,0,0,0.28)), inset 0 -1px 0 0 var(--edge-lit, rgba(255,255,255,0.05))",
      }}
    >
      {/* Column header */}
      <div
        className="border-b text-on-surface-variant"
        style={{
          borderColor: "var(--color-outline-variant, #444748)",
        }}
      >
        <ProjectListHeader />
      </div>

      {/* Rows */}
      <div>
        {projects.map((project) => (
          <ProjectRow key={project.id} project={project} />
        ))}
      </div>
    </div>
  );
}
