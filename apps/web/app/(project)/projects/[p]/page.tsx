import { EpicsBoardClient } from "@/components/features/epics/epics-board-client";
import { NewEpicModal } from "@/components/features/epics/new-epic-modal";
import { Icon } from "@/components/icon";
import type { EpicWithRunSummary } from "@/lib/api/endpoints";
import { getProject, listEpics } from "@/lib/api/endpoints";

export default async function ProjectPage({ params }: { params: Promise<{ p: string }> }) {
  const { p } = await params;

  const [project, epics] = await Promise.all([
    getProject(p).catch(() => null),
    // include_completed=true — the board shows open and completed epics alike
    // (matches the client-side refetch in EpicsBoardClient).
    listEpics(p, true).catch(() => [] as EpicWithRunSummary[]),
  ]);

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

      {/* The epic board IS the overview: one interactive list with filter,
          complete/reopen, multi-select merge and archive — no separate
          featured block, no separate /epics page. */}
      <EpicsBoardClient projectId={p} initialEpics={epics} />
    </div>
  );
}
