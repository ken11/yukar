import { GlobalRail } from "@/components/chrome/global-rail";
import { MobileNavDrawer } from "@/components/chrome/mobile-nav-drawer";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { getUsageSummary } from "@/lib/api/endpoints";

export default async function GlobalLayout({ children }: { children: React.ReactNode }) {
  const initialUsage = await getUsageSummary().catch(() => null);

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Mobile: hamburger drawer (md:hidden) */}
      <MobileNavDrawer extraControls={<ThemeToggle />} />
      {/* Desktop: fixed 56px vertical rail (hidden md:flex) */}
      <GlobalRail initialUsage={initialUsage ?? undefined} />
      {/*
       * ml-0: no rail on mobile
       * md:ml-[56px]: offset by rail width on desktop
       * pt-12: padding for mobile top bar height
       * md:pt-0: no top bar on desktop
       */}
      <main className="ml-0 flex-1 overflow-y-auto pt-12 md:ml-[56px] md:pt-0">{children}</main>
    </div>
  );
}
