import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `yukar serve` supervises node .next/standalone/server.js as a child process (architecture §3.1)
  output: "standalone",
  // SSE invariant: disable Next.js gzip compression.
  // The default compress:true also gzip-compresses `text/event-stream` responses routed
  // through the /api/* rewrite, buffering the stream (browsers always send
  // Accept-Encoding: gzip, so all live events such as token/run_started never
  // arrive; curl does not request gzip so this goes unnoticed). yukar is local-only,
  // so compression brings little benefit — disable it for both dev and standalone (`yukar serve`).
  compress: false,
  // Same-origin proxying: the browser sees only :3000 and /api/* is forwarded to FastAPI
  // (docs/architecture.md §3.1)
  // YUKAR_API_BASE_URL env var overrides the rewrite target (used in E2E with a different-port FastAPI)
  async rewrites() {
    const apiBase = process.env.YUKAR_API_BASE_URL ?? "http://127.0.0.1:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${apiBase}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
