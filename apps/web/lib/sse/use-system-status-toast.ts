"use client";

/**
 * useSystemStatusToast — fires a one-time warning toast on mount when the
 * indexer file watcher failed to start.
 *
 * Design rationale: watcher startup failure is a one-shot event that happens
 * before any SSE connection is established. Polling via SSE would miss it.
 * A REST fetch on mount, guarded by a module-level flag so it runs at most
 * once per session, is the correct approach per architecture.md §2.2/§3.3.
 */

import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { getSystemStatus } from "@/lib/api/endpoints";
import { useT } from "@/lib/i18n/provider";

/** Module-level flag so the check runs at most once per browser session. */
let checkedThisSession = false;

/** Reset the session guard. Exposed for testing purposes only. */
export function _resetSystemStatusSessionGuard(): void {
  checkedThisSession = false;
}

export function useSystemStatusToast(): void {
  const t = useT();
  // Keep latest translator in ref so the effect closure stays stable.
  const tRef = useRef(t);
  tRef.current = t;

  useEffect(() => {
    if (checkedThisSession) return;
    checkedThisSession = true;

    getSystemStatus()
      .then((status) => {
        const watcher = status.indexer_watcher;
        if (watcher.watch_enabled && !watcher.watcher_ok) {
          const base = tRef.current("notifications.watcherFailedToStart");
          const message = watcher.reason ? `${base} (${watcher.reason})` : base;
          toast.warning(message, { duration: 8000 });
        }
      })
      .catch(() => {
        // Silently ignore — system status is best-effort; do not surface API errors as toasts.
      });
  }, []);
}
