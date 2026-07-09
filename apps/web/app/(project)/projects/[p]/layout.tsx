import { GlobalRail } from "@/components/chrome/global-rail";
import { MobileNavDrawer } from "@/components/chrome/mobile-nav-drawer";
import { ProjectChromeShell } from "@/components/chrome/project-chrome-shell";
import { ProjectContentFrame } from "@/components/chrome/project-content-frame";
import { getProject, getUsageSummary } from "@/lib/api/endpoints";

export default async function ProjectLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ p: string }>;
}) {
  const { p } = await params;
  const [initialUsage, project] = await Promise.all([
    getUsageSummary().catch(() => null),
    getProject(p).catch(() => null),
  ]);

  const projectName = project?.name ?? p;

  return (
    <div className="flex h-[100dvh] overflow-hidden">
      {/* Mobile: hamburger drawer (md:hidden) */}
      <MobileNavDrawer />
      {/* Desktop: fixed 56px vertical rail (hidden md:flex) */}
      <GlobalRail initialUsage={initialUsage ?? undefined} />
      {/*
       * Content column. The mobile top-bar offset (pt-12) lives inside
       * ProjectContentFrame and is dropped on epic detail routes, where the
       * mobile top bar itself is hidden (see MobileNavDrawer).
       */}
      <ProjectContentFrame>
        <ProjectChromeShell projectId={p} projectName={projectName}>
          {children}
        </ProjectChromeShell>
      </ProjectContentFrame>
    </div>
  );
}
