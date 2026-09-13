/**
 * Fetch wrapper for both RSC and client use.
 * - Server side (RSC): hits FastAPI directly (YUKAR_API_BASE_URL, default http://127.0.0.1:8000)
 * - Client side (browser): uses relative /api (via next.config.ts rewrite)
 *
 * In the browser every request also carries the UI locale (`X-Yukar-Locale`,
 * read from the locale cookie) so the backend can tell the user-facing agents
 * which language to write in.
 */

import { LOCALE_COOKIE, LOCALE_HEADER } from "@/lib/i18n/constants";

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

/** Locale the browser is currently rendering in (cookie), defaulting like `getLocale`. */
export function browserLocale(): "ja" | "en" {
  if (typeof document === "undefined") return "ja";
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${LOCALE_COOKIE}=(ja|en)(?:;|$)`));
  return match?.[1] === "en" ? "en" : "ja";
}

function localeHeaders(): Record<string, string> {
  // RSC/Node has no document; server-side reads are render-only and never
  // start a run, so the header is a browser concern.
  if (typeof document === "undefined") return {};
  return { [LOCALE_HEADER]: browserLocale() };
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
      ...localeHeaders(),
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
