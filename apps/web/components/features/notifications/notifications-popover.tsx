"use client";

import * as Popover from "@radix-ui/react-popover";
import { useRouter } from "next/navigation";
import { Icon } from "@/components/icon";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import type { Notification } from "@/lib/sse/use-project-notifications";

interface NotificationsPopoverProps {
  projectId: string;
  notifications: Notification[];
  unreadCount: number;
  onOpen: () => void;
}

function useRelativeTime() {
  const t = useT();
  return function relativeTime(ts: number): string {
    const diffMs = Date.now() - ts;
    const diffSec = Math.floor(diffMs / 1000);
    if (diffSec < 60) return t("notifications.justNow");
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return t("notifications.minutesAgo").replace("{n}", String(diffMin));
    const diffH = Math.floor(diffMin / 60);
    return t("notifications.hoursAgo").replace("{n}", String(diffH));
  };
}

export function NotificationsPopover({
  projectId,
  notifications,
  unreadCount,
  onOpen,
}: NotificationsPopoverProps) {
  const router = useRouter();
  const relativeTime = useRelativeTime();

  return (
    <Popover.Root
      onOpenChange={(open) => {
        if (open) onOpen();
      }}
    >
      <Popover.Trigger asChild>
        <button
          type="button"
          className="relative text-outline transition-colors hover:text-on-surface"
          aria-label={`Notifications${unreadCount > 0 ? ` (${unreadCount} unread)` : ""}`}
        >
          <Icon name="notifications" />
          {unreadCount > 0 && (
            <span
              className="absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full text-[9px] font-bold"
              style={{ backgroundColor: "var(--color-light)", color: "var(--color-on-secondary)" }}
            >
              {unreadCount > 9 ? "9+" : unreadCount}
            </span>
          )}
        </button>
      </Popover.Trigger>

      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          className={cn(
            "z-50 w-80 rounded-lg border border-outline-variant bg-surface-container-high shadow-xl",
            "outline-none",
          )}
        >
          <div className="flex items-center justify-between border-b border-outline-variant/50 px-4 py-3">
            <span className="text-body-sm font-semibold text-on-surface">Notifications</span>
            {notifications.length > 0 && (
              <span className="text-[11px] text-outline">
                {unreadCount > 0 ? `${unreadCount} unread` : "All read"}
              </span>
            )}
          </div>

          <div className="max-h-80 overflow-y-auto">
            {notifications.length === 0 ? (
              <p className="px-4 py-6 text-center text-body-sm text-outline">
                No notifications yet.
              </p>
            ) : (
              <ul>
                {notifications.map((n) => (
                  <li key={n.id}>
                    <button
                      type="button"
                      className={cn(
                        "w-full px-4 py-3 text-left transition-colors hover:bg-surface-container-highest",
                        !n.read && "border-l-2 border-[var(--color-light)]",
                      )}
                      onClick={() => {
                        router.push(`/projects/${projectId}/epics/${n.epicId}/tasks`);
                      }}
                    >
                      <div className="flex items-start gap-2">
                        <NotifIcon type={n.type} />
                        <div className="flex-1 min-w-0">
                          <p
                            className={cn(
                              "text-body-sm leading-snug",
                              n.read ? "text-on-surface-variant" : "text-on-surface",
                            )}
                          >
                            {n.message}
                          </p>
                          <p className="mt-0.5 text-[11px] text-outline">{relativeTime(n.ts)}</p>
                        </div>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function NotifIcon({ type }: { type: Notification["type"] }) {
  if (type === "run_completed") {
    return (
      <Icon
        name="check_circle"
        className="mt-0.5 shrink-0 text-[16px] text-[var(--color-success)]"
      />
    );
  }
  if (type === "run_failed") {
    return <Icon name="error" className="mt-0.5 shrink-0 text-[16px] text-error" />;
  }
  if (type === "run_paused") {
    return <Icon name="pause_circle" className="mt-0.5 shrink-0 text-[16px] text-outline" />;
  }
  if (type === "run_resumed") {
    return (
      <Icon name="play_circle" className="mt-0.5 shrink-0 text-[16px] text-on-surface-variant" />
    );
  }
  // run_started
  return (
    <Icon name="rocket_launch" className="mt-0.5 shrink-0 text-[16px] text-on-surface-variant" />
  );
}
