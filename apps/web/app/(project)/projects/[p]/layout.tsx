import { GlobalRail } from "@/components/chrome/global-rail";
import { MobileNavDrawer } from "@/components/chrome/mobile-nav-drawer";
import { ProjectChromeShell } from "@/components/chrome/project-chrome-shell";
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
       * ml-0: no rail on mobile
       * md:ml-[56px]: offset by rail width on desktop
       * pt-12: top padding for mobile top bar height
       * md:pt-0: no top bar on desktop
       */}
      <div className="ml-0 flex flex-1 flex-col overflow-hidden pt-12 md:ml-[56px] md:pt-0">
        {/*
         * C1: propagates h-full to children via overflow-hidden.
         * Epic routes: EpicShell manages scrolling internally.
         * Non-epic routes: ProjectChromeShell has overflow-y-auto internally.
         */}
        <div className="flex-1 overflow-hidden">
          <ProjectChromeShell projectId={p} projectName={projectName}>
            {children}
          </ProjectChromeShell>
        </div>
      </div>
    </div>
  );
}
