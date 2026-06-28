"use client";

import { Icon } from "@/components/icon";
import type { Task } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";

type TaskStatus = Task["status"];

/** Returns icon name + inline style for status. Color = meaning (cyan for running only, warm for error). */
function statusIcon(status: TaskStatus): {
  name: string;
  style?: React.CSSProperties;
  className?: string;
} {
  switch (status) {
    case "done":
      // done = monochrome (neutral) check — cyan is reserved for "live/running"
      return {
        name: "check_circle",
        className: "text-on-surface-variant",
        style: { opacity: 0.5 },
      };
    case "in_progress":
      // static cyan dot — pulse is reserved for EpicScopeHeader only
      return {
        name: "pending",
        style: { color: "var(--color-running)" },
      };
    case "blocked":
      return { name: "block", className: "text-error" };
    case "todo":
      return { name: "radio_button_unchecked", className: "text-outline" };
  }
}

function TaskRow({ task, t }: { task: Task; t: (k: string) => string }) {
  const icon = statusIcon(task.status);
  const isDone = task.status === "done";
  const isBlocked = task.status === "blocked";
  const isRunning = task.status === "in_progress";

  const statusText = (() => {
    switch (task.status) {
      case "done":
        return t("tasks.status.done");
      case "in_progress":
        return t("tasks.status.inProgress");
      case "blocked":
        return t("tasks.status.blocked");
      case "todo":
        return t("tasks.status.todo");
    }
  })();

  return (
    <div
      data-testid={`task-row-${task.id}`}
      className={cn(
        "edge-h flex min-h-[44px] items-start gap-3 px-0 py-2.5 transition-colors",
        isDone && "opacity-50",
        isBlocked && "bg-error/[0.04]",
      )}
      style={
        isRunning
          ? { backgroundColor: "color-mix(in oklab, var(--color-running) 4%, transparent)" }
          : undefined
      }
    >
      {/* Status icon — glyph carries meaning, not color alone */}
      <span className="mt-0.5 shrink-0" style={icon.style} role="img" aria-label={statusText}>
        <Icon name={icon.name} filled={isDone} className={cn("text-[18px]", icon.className)} />
      </span>

      {/* Main content */}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          {/* Task ID — .data mono */}
          <span className="data shrink-0">{task.id}</span>
          {/* Title — Geist body, no line-through (done = opacity on row) */}
          <p className="text-body-sm text-on-surface">{task.title}</p>
        </div>

        {(task.depends_on?.length ?? 0) > 0 && (
          <p className="data mt-0.5 text-outline/70">depends on: {task.depends_on?.join(", ")}</p>
        )}
        {task.contract && (
          <p className="mt-0.5 text-[11px] text-outline line-clamp-2" title={task.contract}>
            <span className="text-on-surface-variant">contract:</span> {task.contract}
          </p>
        )}
      </div>

      {/* Right meta — badges, status label */}
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {/* agent badge: neutral tonal (not cyan) */}
        {task.agent && (
          <span className="data flex items-center gap-1 rounded border border-outline-variant/30 bg-surface-container-lowest px-1.5 py-0.5 text-on-surface-variant">
            <Icon name="smart_toy" className="text-[10px]" />
            {task.agent}
          </span>
        )}
        {task.repo && (
          <span className="data rounded border border-outline-variant/30 bg-surface-container-lowest px-1.5 py-0.5">
            {task.repo}
          </span>
        )}
        {task.thread && (
          <span className="data flex items-center gap-1 rounded border border-outline-variant/20 px-1.5 py-0.5">
            <Icon name="forum" className="text-[10px]" />
            {task.thread}
          </span>
        )}
        {/* Status label — always shown with the icon above (UD: color + glyph + label) */}
        <span
          className={cn(
            "data uppercase tracking-wider",
            isBlocked && "text-error",
            isRunning && "text-[var(--color-running)]",
            !isDone && !isBlocked && !isRunning && "text-outline",
          )}
        >
          {statusText}
        </span>
      </div>
    </div>
  );
}

/** Group header — label style, hairline below via edge-h on rows */
function GroupHeader({ label, count }: { label: string; count: number }) {
  return (
    <div className="mb-1 flex items-baseline gap-2 pb-1">
      <h3 className="text-[10px] font-medium uppercase tracking-wider text-outline">{label}</h3>
      <span className="data text-outline/60">{count}</span>
    </div>
  );
}

export function TaskList({ tasks }: { tasks: Task[] }) {
  const t = useT();

  const inProgress = tasks.filter((task) => task.status === "in_progress");
  const todo = tasks.filter((task) => task.status === "todo");
  const blocked = tasks.filter((task) => task.status === "blocked");
  const done = tasks.filter((task) => task.status === "done");

  const groups = [
    { label: t("tasks.groups.inProgress"), tasks: inProgress },
    { label: t("tasks.groups.todo"), tasks: todo },
    { label: t("tasks.groups.blocked"), tasks: blocked },
    { label: t("tasks.groups.done"), tasks: done },
  ].filter((g) => g.tasks.length > 0);

  if (groups.length === 0) {
    return <p className="text-body-sm text-outline">{t("tasks.noTasks")}</p>;
  }

  return (
    <div className="space-y-8">
      {groups.map((group) => (
        <div key={group.label}>
          <GroupHeader label={group.label} count={group.tasks.length} />
          <div>
            {group.tasks.map((task) => (
              <TaskRow key={task.id} task={task} t={t} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
