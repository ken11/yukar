/**
 * Fetch wrapper for both RSC and client use.
 * - Server side (RSC): hits http://127.0.0.1:8000 directly
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
    // RSC / Node — hit FastAPI directly
    return "http://127.0.0.1:8000";
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
