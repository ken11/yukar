/**
 * Fetch wrapper for both RSC and client use.
 * - Server side (RSC): hits FastAPI directly (YUKAR_API_BASE_URL, default http://127.0.0.1:8000)
 * - Client side (browser): uses relative /api (via next.config.ts rewrite)
 */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function baseUrl(): string {
  if (typeof window === "undefined") {
    // RSC / Node — hit FastAPI directly. Mirror next.config.ts's rewrite target so
    // server-side fetches reach the same FastAPI the browser's /api rewrite forwards to
    // (E2E points YUKAR_API_BASE_URL at a different-port FastAPI).
    return process.env.YUKAR_API_BASE_URL ?? "http://127.0.0.1:8000";
  }
  // browser — same-origin /api rewrite
  return "";
}

export interface FetchOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

export async function apiFetch<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const { body, headers, ...rest } = options;

  const url = `${baseUrl()}${path}`;

  const res = await fetch(url, {
    ...rest,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let errorBody: unknown;
    try {
      errorBody = await res.json();
    } catch {
      errorBody = null;
    }
    throw new ApiError(res.status, errorBody, `API ${res.status}: ${res.statusText} — ${path}`);
  }

  if (res.status === 204) {
    return undefined as unknown as T;
  }

  return res.json() as Promise<T>;
}
