"use client";

import { TabBar } from "@/components/ui/tab-bar";
import { useT } from "@/lib/i18n/provider";

interface ProjectTabBarProps {
  projectId: string;
}

/**
 * ProjectTabBar — 4 tabs: Overview / Docs / Repos / Settings.
 * (The epic board merged into Overview; /epics redirects there.)
 */
export function ProjectTabBar({ projectId }: ProjectTabBarProps) {
  const t = useT();
  const base = `/projects/${projectId}`;

  const items = [
    {
      href: base,
      label: t("project.tabs.overview"),
      segment: undefined as string | undefined,
    },
    {
      href: `${base}/docs`,
      label: t("project.tabs.docs"),
      segment: "docs" as string | undefined,
    },
    {
      href: `${base}/repos`,
      label: t("project.tabs.repos"),
      segment: "repos" as string | undefined,
    },
    {
      href: `${base}/settings`,
      label: t("project.tabs.settings"),
      segment: "settings" as string | undefined,
    },
  ];

  return <TabBar items={items} />;
}
