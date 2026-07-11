"use client";

import { Icon } from "@/components/icon";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";

export type StatusValue =
  | "running"
  | "preparing"
  | "idle"
  | "active"
  | "paused"
  | "awaiting"
  | "interrupted"
  | "open"
  | "completed"
  | "merged"
  | "error"
  | "archived";

interface StatusBadgeProps {
  status: StatusValue;
  className?: string;
}

type BadgeConfig = {
  icon: string;
  filled?: boolean;
  colorClass: string;
  labelKey: string;
  running?: boolean;
};

const BADGE_MAP: Record<StatusValue, BadgeConfig> = {
  running: {
    icon: "radio_button_checked",
    colorClass: "text-[var(--color-running)]",
    labelKey: "epic.status.running",
    running: true,
  },
  preparing: {
    icon: "sync",
    colorClass: "text-on-surface-variant",
    labelKey: "epic.status.preparing",
    running: true,
  },
  idle: {
    icon: "play_arrow",
    colorClass: "text-on-surface-variant",
    labelKey: "epic.status.idle",
  },
  active: {
    icon: "circle",
    colorClass: "text-on-surface-variant",
    labelKey: "projects.status.active",
  },
  archived: {
    icon: "archive",
    colorClass: "text-outline",
    labelKey: "projects.status.archived",
  },
  paused: {
    icon: "pause",
    colorClass: "text-on-surface-variant",
    labelKey: "epic.status.paused",
  },
  awaiting: {
    icon: "pending_actions",
    colorClass: "text-on-surface-variant",
    labelKey: "epic.status.awaiting",
  },
  interrupted: {
    icon: "warning",
    colorClass: "text-on-surface-variant",
    labelKey: "epic.status.interrupted",
  },
  open: {
    icon: "circle",
    colorClass: "text-on-surface-variant",
    labelKey: "epic.status.open",
  },
  completed: {
    icon: "check",
    filled: true,
    colorClass: "text-on-surface",
    labelKey: "epic.status.completed",
  },
  // "merged" is a fact attribute (epic.merged_at), not an epic status — shown
  // as an extra badge next to the open/completed status.
  merged: {
    icon: "check",
    filled: true,
    colorClass: "text-on-surface",
    labelKey: "epic.status.merged",
  },
  error: {
    icon: "error",
    colorClass: "text-error",
    labelKey: "epic.status.error",
  },
};

/**
 * StatusBadge — always a glyph + uppercase mono label. Color alone is not permitted.
 */
export function StatusBadge({ status, className }: StatusBadgeProps) {
  const t = useT();
  const cfg = BADGE_MAP[status] ?? BADGE_MAP.idle;

  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <Icon name={cfg.icon} filled={cfg.filled} className={cn("text-[14px]", cfg.colorClass)} />
      <span
        className={cn(
          "font-mono text-[12px] font-medium uppercase tracking-[0.02em] tabular-nums",
          cfg.colorClass,
        )}
      >
        {t(cfg.labelKey)}
      </span>
    </span>
  );
}
