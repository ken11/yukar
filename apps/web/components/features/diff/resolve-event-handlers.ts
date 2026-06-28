/**
 * makeResolveEventHandlers — creates event handlers with replay-buffer support and run_id filtering
 *
 * Because backend SSE may replay lifecycle events (run_started/run_completed/run_failed),
 * subscribers process only events belonging to their own run by filtering on run_id.
 *
 * #42: extracted from diff-page-client.tsx into a standalone module.
 */

import type { RunEvent } from "@/lib/api/endpoints";

export function makeResolveEventHandlers({
  resolveRunId,
  onRunStarted,
  onRunCompleted,
  onRunFailed,
  onWorkerStarted,
  onWorkerCompleted,
}: {
  resolveRunId: string | null;
  onRunStarted: () => void;
  onRunCompleted: () => void;
  onRunFailed: (error: string) => void;
  onWorkerStarted: (workerId: string) => void;
  onWorkerCompleted: (workerId: string) => void;
}) {
  return (event: RunEvent) => {
    // Filter lifecycle events that carry a run_id
    if (
      event.type === "run_started" ||
      event.type === "run_completed" ||
      event.type === "run_failed"
    ) {
      const evRunId = "run_id" in event ? (event.run_id as string | undefined) : undefined;
      if (resolveRunId !== null && evRunId !== resolveRunId) return;
    }

    switch (event.type) {
      case "run_started":
        onRunStarted();
        break;
      case "worker_started":
        onWorkerStarted("worker_id" in event ? String(event.worker_id) : "");
        break;
      case "worker_completed":
        onWorkerCompleted("worker_id" in event ? String(event.worker_id) : "");
        break;
      case "run_completed":
        onRunCompleted();
        break;
      case "run_failed":
        onRunFailed(
          "error" in event ? String(event.error ?? "Resolve run failed") : "Resolve run failed",
        );
        break;
      default:
        break;
    }
  };
}
