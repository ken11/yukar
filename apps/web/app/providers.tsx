"use client";

import { isServer, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { useSystemStatusToast } from "@/lib/sse/use-system-status-toast";

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        // Explicitly set a gcTime longer than the default 5 minutes so that entries
        // patched via SSE (run state, etc.) are not evicted while inactive.
        gcTime: 30 * 60_000,
        refetchOnWindowFocus: false,
        retry: 1,
      },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

function getQueryClient(): QueryClient {
  // server: create a fresh instance per request (not shared between requests). client: singleton scoped to the module.
  if (isServer) return makeQueryClient();
  if (!browserQueryClient) browserQueryClient = makeQueryClient();
  return browserQueryClient;
}

/** Fires a one-time warning toast if the indexer file watcher failed to start. */
function SystemStatusChecker() {
  useSystemStatusToast();
  return null;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => getQueryClient());
  return (
    <QueryClientProvider client={client}>
      <SystemStatusChecker />
      {children}
    </QueryClientProvider>
  );
}
