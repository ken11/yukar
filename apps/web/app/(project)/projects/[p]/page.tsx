import Link from "next/link";
import { NewEpicModal } from "@/components/features/epics/new-epic-modal";
import { Icon } from "@/components/icon";
import { EmptyState } from "@/components/ui/empty-state";
import { StatusBadge } from "@/components/ui/status-badge";
import type { DiffSummary, EpicWithRunSummary, TasksFile } from "@/lib/api/endpoints";
import { getGitDiffSummary, getProject, getTasks, listEpics } from "@/lib/api/endpoints";
import { hasYourTurn, isTerminalStatus } from "@/lib/epic-utils";
import { type Dict, getDictionary } from "@/lib/i18n/dictionary";
import { getLocale } from "@/lib/i18n/locale";

export default async function ProjectPage({ params }: { params: Promise<{ p: string }> }) {
  const { p } = await params;

  const [project, epics, locale] = await Promise.all([
    getProject(p).catch(() => null),
    // include_completed=true — the overview also surfaces recently completed
    // epics (the "Recent" section); the open/completed split is done here.
    listEpics(p, true).catch(() => [] as EpicWithRunSummary[]),
    getLocale(),
  ]);

  const t = getDictionary(locale);

  const byUpdatedDesc = (a: EpicWithRunSummary, b: EpicWithRunSummary) =>
    (b.updated_at ?? "").localeCompare(a.updated_at ?? "");

  // The epic status is a single user-owned bit: open ⇄ completed.
  // #5: use isTerminalStatus() uniformly to detect completed
  const openEpics = epics.filter((e) => !isTerminalStatus(e.status)).sort(byUpdatedDesc);

  // featured = most recently updated open epic (shown as the large block)
  const featuredEpic = openEpics[0] ?? null;

  // open epics other than featured, shown as a list
  const activeListEpics = openEpics.slice(1);

  // Recent epics = completed work, newest first, max 5 — keeps recently
  // finished (and possibly merged) work visible on the overview.
  const recentEpics = epics
    .filter((e) => isTerminalStatus(e.status))
    .sort(byUpdatedDesc)
    .slice(0, 5);

  // featured epic telemetry (fetched in RSC; failures are ignored)
  const [featuredTasks, featuredDiff] = featuredEpic
    ? await Promise.all([
        getTasks(p, featuredEpic.id).catch(
          (): TasksFile => ({ tasks: [], progress: { done: 0, total: 0 } }),
        ),
        getGitDiffSummary(p, featuredEpic.id, "working").catch(
          (): DiffSummary => ({ repos: [], total_files: 0, total_added: 0, total_deleted: 0 }),
        ),
      ])
    : [null, null];

  const tasksDone =
    featuredTasks?.progress?.done ??
    featuredTasks?.tasks?.filter((t) => t.status === "done").length ??
    0;
  const tasksTotal = featuredTasks?.progress?.total ?? featuredTasks?.tasks?.length ?? 0;
  const diffAdded = featuredDiff?.repos?.reduce((s, r) => s + (r.added ?? 0), 0) ?? 0;
  const diffRemoved = featuredDiff?.repos?.reduce((s, r) => s + (r.deleted ?? 0), 0) ?? 0;

  return (
    /*
     * Left-anchored layout that uses full desktop width.
     * Drops mx-auto / max-w centering in favor of 32px gutter from the axis.
     * Void is intentionally left on the right side (avoids symmetric sprawl).
     */
    <div className="pl-4 pr-4 pb-[var(--spacing-bay,96px)] md:pl-8 md:pr-8">
      {/* Hero band: project name display + repos + New Epic — left-anchored to axis */}
      <div className="pt-8 pb-8 md:pt-[var(--spacing-section,64px)] md:pb-[var(--spacing-section,64px)]">
        <div className="flex items-start justify-between gap-4 md:gap-8">
          <div>
            {/* display title — large, left-anchored */}
            <h1
              className="font-sans font-semibold text-on-surface"
              style={{
                fontSize: "clamp(36px, 3.5vw, 52px)",
                lineHeight: "1.1",
                letterSpacing: "-0.02em",
              }}
            >
              {project?.name ?? p}
            </h1>

            {/* repos — subtle mono chips (minimal border) */}
            {project?.repos && project.repos.length > 0 && (
              <div className="mt-4 flex flex-wrap gap-2">
                {project.repos.map((repo) => (
                  <span
                    key={repo}
                    className="data inline-flex items-center gap-1"
                    style={{
                      border: "1px solid var(--color-outline-variant)",
                      padding: "2px 8px",
                    }}
                  >
                    <Icon name="folder_open" className="text-[11px]" />
                    {repo}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* New Epic — primary action (the single strong action) */}
          <div className="shrink-0 pt-1">
            <NewEpicModal projectId={p} />
          </div>
        </div>
      </div>

      {epics.length === 0 ? (
        <EmptyState
          address={`${p} ／ no epics`}
          message={t.empty.noEpicsProject}
          action={<NewEpicModal projectId={p} />}
        />
      ) : (
        <div>
          {/* Open-work section: the most recently updated open epic as a large
               block, remaining open epics as a list. */}
          {featuredEpic && (
            <section aria-label={t.overview.activeWork}>
              {/* Section label */}
              <div
                className="edge-h mb-8 flex items-center gap-3"
                style={{ paddingBottom: "12px" }}
              >
                <span
                  className="data uppercase"
                  style={{ letterSpacing: "0.08em", color: "var(--color-on-surface-variant)" }}
                >
                  {t.overview.open}
                </span>
              </div>

              {/* featured large block — most recent open epic. Live "running"
                  telemetry is not derivable from epic.status (1-bit) in RSC. */}
              <FeaturedEpicBlock
                epic={featuredEpic}
                projectId={p}
                isRunning={false}
                openLabel={t.overview.openEpic}
                tasksDone={tasksDone}
                tasksTotal={tasksTotal}
                diffAdded={diffAdded}
                diffRemoved={diffRemoved}
                dict={t}
              />

              {/* Active epics other than featured (remaining running + all planned) shown as a list.
                   When there is no featured epic (planned only), activeListEpics holds all planned epics. */}
              {activeListEpics.length > 0 && (
                <div
                  style={{
                    borderTop: "1px solid var(--edge-shadow)",
                    marginTop: featuredEpic ? "32px" : undefined,
                  }}
                >
                  {activeListEpics.map((epic) => (
                    <EpicStructureRow
                      key={epic.id}
                      epic={epic}
                      projectId={p}
                      openLabel={t.overview.openEpic}
                      openAriaTemplate={t.overview.openEpicAria}
                    />
                  ))}
                </div>
              )}
            </section>
          )}

          {/* void — separates the active section from recent */}
          {recentEpics.length > 0 && <div aria-hidden className="h-[var(--spacing-bay,96px)]" />}

          {/* Recent epics — completed, newest first, max 5 */}
          {recentEpics.length > 0 && (
            <section aria-label={t.overview.recentEpics}>
              <div className="mb-6 flex items-center justify-between">
                <span
                  className="data uppercase"
                  style={{ letterSpacing: "0.08em", color: "var(--color-on-surface-variant)" }}
                >
                  {t.overview.recentEpics}
                </span>
                <Link
                  href={`/projects/${p}/epics`}
                  className="data transition-colors hover:text-on-surface"
                  style={{ letterSpacing: "0.02em" }}
                >
                  {t.overview.allEpics}
                </Link>
              </div>

              {/* Full-field-width table rows — left-anchored to axis, rows separated by hairlines */}
              <div style={{ borderTop: "1px solid var(--edge-shadow)" }}>
                {recentEpics.map((epic) => (
                  <EpicStructureRow
                    key={epic.id}
                    epic={epic}
                    projectId={p}
                    openLabel={t.overview.openEpic}
                    openAriaTemplate={t.overview.openEpicAria}
                  />
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components (server)
// ---------------------------------------------------------------------------

/**
 * FeaturedEpicBlock — layout anchored to the axis (not a card).
 * Large mono EP-id + large title (title/display) + .data telemetry row + status + CTA.
 * A single cyan point on the left edge when running. Large void surrounding it.
 */
function FeaturedEpicBlock({
  epic,
  projectId,
  isRunning,
  openLabel,
  tasksDone,
  tasksTotal,
  diffAdded,
  diffRemoved,
  dict,
}: {
  epic: EpicWithRunSummary;
  projectId: string;
  isRunning: boolean;
  openLabel: string;
  tasksDone: number;
  tasksTotal: number;
  diffAdded: number;
  diffRemoved: number;
  dict: Dict;
}) {
  const managerSeg = epic.active_thread_id ?? "manager";
  const href = `/projects/${projectId}/epics/${epic.id}/threads/${managerSeg}`;

  return (
    <div
      // E2E compat: keep epic-card-* selector
      data-testid={`epic-card-${epic.id}`}
      data-epic-status={epic.status}
      // Running only: static cyan point on the left edge (pulsing is handled by EpicScopeHeader)
      // Use full desktop width: remove maxWidth and left-anchor at field width
      className="pl-4 md:pl-8"
      style={
        isRunning
          ? {
              boxShadow: "inset 2px 0 0 0 var(--color-light)",
            }
          : undefined
      }
    >
      {/* Large mono EP-id */}
      <div className="data mb-4" style={{ letterSpacing: "0.06em" }}>
        {epic.id}
      </div>

      {/* Large title — display/title as the main subject */}
      <h2
        className="font-sans font-semibold text-on-surface mb-6"
        style={{
          fontSize: "clamp(24px, 3vw, 32px)",
          lineHeight: "1.2",
          letterSpacing: "-0.02em",
        }}
      >
        {epic.title}
      </h2>

      {/* .data telemetry row */}
      <TelemetryRow
        tasksDone={tasksDone}
        tasksTotal={tasksTotal}
        diffAdded={diffAdded}
        diffRemoved={diffRemoved}
        isRunning={isRunning}
        dict={dict}
      />

      {/* status + CTA */}
      <div className="mt-8 flex items-center gap-4">
        {/* your turn (P4): the conversation run parked in "waiting" — from
            run_summary embedded in the epic list (static RSC render) */}
        {hasYourTurn(epic) && (
          <span data-testid={`your-turn-${epic.id}`} className="contents">
            <StatusBadge status="awaiting" />
          </span>
        )}
        <StatusBadge status={isRunning ? "running" : epic.status} />
        <Link
          href={href}
          // E2E compat: epic-item-* selector
          data-testid={`epic-item-${epic.id}`}
          className="inline-flex items-center gap-1.5 rounded border border-outline-variant px-4 py-2 font-sans text-[13px] font-medium text-on-surface-variant transition-colors hover:border-outline hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface)]"
        >
          <Icon name="arrow_forward" className="text-[14px]" />
          {openLabel}
        </Link>
      </div>
    </div>
  );
}

/** Telemetry row: task progress + diff +/− in .data mono */
function TelemetryRow({
  tasksDone,
  tasksTotal,
  diffAdded,
  diffRemoved,
  isRunning,
  dict,
}: {
  tasksDone: number;
  tasksTotal: number;
  diffAdded: number;
  diffRemoved: number;
  isRunning: boolean;
  dict: Dict;
}) {
  const hasTasks = tasksTotal > 0;
  const hasDiff = diffAdded > 0 || diffRemoved > 0;

  if (!hasTasks && !hasDiff && !isRunning) return null;

  return (
    <div
      className="flex flex-wrap items-center gap-4"
      style={{ borderTop: "1px solid var(--color-outline-variant)", paddingTop: "16px" }}
    >
      {hasTasks && (
        <span
          role="status"
          className="inline-flex items-center gap-1.5"
          aria-label={dict.a11y.tasksDoneOf
            .replace("{done}", String(tasksDone))
            .replace("{total}", String(tasksTotal))}
        >
          <Icon name="checklist" className="text-[12px] text-on-surface-variant" />
          <span className="data">
            {tasksDone}
            <span style={{ color: "var(--color-outline-variant)" }}>/</span>
            {tasksTotal}
          </span>
          <span
            className="font-mono uppercase"
            style={{ fontSize: "10px", color: "var(--color-outline)", letterSpacing: "0.04em" }}
          >
            tasks
          </span>
        </span>
      )}
      {hasDiff && (
        <span className="data inline-flex items-center gap-2">
          <span style={{ color: "var(--color-added)" }}>+{diffAdded}</span>
          <span style={{ color: "var(--color-removed)" }}>−{diffRemoved}</span>
        </span>
      )}
      {isRunning && (
        <span
          role="status"
          className="data inline-flex items-center gap-1 uppercase"
          style={{
            color: "var(--color-light)",
            letterSpacing: "0.06em",
          }}
          aria-label={dict.a11y.running}
        >
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ backgroundColor: "var(--color-light)" }}
            aria-hidden
          />
          running
        </span>
      )}
    </div>
  );
}

/**
 * EpicStructureRow — a row as information design. hairline separator, left-anchored to axis.
 * Aligns id, status, and data. Not wrapped in a card.
 */
function EpicStructureRow({
  epic,
  projectId,
  openLabel,
  openAriaTemplate,
}: {
  epic: EpicWithRunSummary;
  projectId: string;
  openLabel: string;
  openAriaTemplate: string;
}) {
  const managerSeg = epic.active_thread_id ?? "manager";
  const href = `/projects/${projectId}/epics/${epic.id}/threads/${managerSeg}`;

  return (
    <div
      // E2E compat: supports both epic-card-* and epic-item-* selectors
      data-testid={`epic-item-${epic.id}`}
      data-epic-status={epic.status}
      className="flex items-center gap-3 py-3 transition-colors hover:bg-surface-container md:gap-6 md:py-4"
      style={{ borderBottom: "1px solid var(--edge-shadow)" }}
    >
      {/* EP-id — fixed-width tabular (shrinks on mobile) */}
      <span className="data w-14 shrink-0 md:w-20" style={{ letterSpacing: "0.04em" }}>
        {epic.id}
      </span>

      {/* Title — flex-1 to use full desktop width */}
      <span className="min-w-0 flex-1 font-sans text-on-surface" style={{ fontSize: "14px" }}>
        {epic.title}
      </span>

      {/* your turn (P4) + StatusBadge */}
      {hasYourTurn(epic) && (
        <span data-testid={`your-turn-${epic.id}`} className="contents">
          <StatusBadge status="awaiting" />
        </span>
      )}
      <StatusBadge status={epic.status} />

      {/* open link — icon only on mobile */}
      <Link
        href={href}
        className="data shrink-0 transition-colors hover:text-on-surface focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white"
        aria-label={openAriaTemplate.replace("{epicId}", epic.id)}
      >
        <span className="hidden md:inline">{openLabel}</span>
        <Icon name="chevron_right" className="text-[18px] text-on-surface-variant md:hidden" />
      </Link>
    </div>
  );
}
