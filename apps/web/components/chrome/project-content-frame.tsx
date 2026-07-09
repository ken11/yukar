"use client";

import { useSelectedLayoutSegments } from "next/navigation";
import { cn } from "@/lib/cn";

/**
 * ProjectContentFrame — the content column next to the rail / below the mobile top bar.
 *
 * Mobile top-bar offset (pt-12) is conditional: epic detail routes hide the
 * mobile top bar (see MobileNavDrawer) so they get the full viewport height.
 * Desktop is unaffected (md:pt-0 + md:ml-[56px] as before).
 */
export function ProjectContentFrame({ children }: { children: React.ReactNode }) {
  const segments = useSelectedLayoutSegments();
  const isEpicDetail = segments[0] === "epics" && segments.length > 1;

  return (
    <div
      className={cn(
        "ml-0 flex flex-1 flex-col overflow-hidden md:ml-[56px] md:pt-0",
        isEpicDetail ? "pt-0" : "pt-12",
      )}
    >
      {/*
       * C1: propagates h-full to children via overflow-hidden.
       * Epic routes: EpicShell manages scrolling internally.
       * Non-epic routes: ProjectChromeShell has overflow-y-auto internally.
       */}
      <div className="flex-1 overflow-hidden">{children}</div>
    </div>
  );
}
