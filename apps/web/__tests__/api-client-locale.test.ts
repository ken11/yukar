/**
 * apiFetch attaches the UI locale header in the browser so the backend can
 * tell the Manager / Reviewer which language the user is reading in.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch, browserLocale } from "../lib/api/client";
import { LOCALE_COOKIE, LOCALE_HEADER } from "../lib/i18n/constants";

function mockFetchOk(): ReturnType<typeof vi.fn> {
  const fn = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ok: true }),
  }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

function setCookie(value: string) {
  // biome-ignore lint/suspicious/noDocumentCookie: jsdom test fixture, no Cookie Store API
  document.cookie = `${LOCALE_COOKIE}=${value}; path=/`;
}

function clearCookie() {
  // biome-ignore lint/suspicious/noDocumentCookie: jsdom test fixture, no Cookie Store API
  document.cookie = `${LOCALE_COOKIE}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
}

beforeEach(() => {
  clearCookie();
});

afterEach(() => {
  clearCookie();
  vi.unstubAllGlobals();
});

describe("browserLocale", () => {
  it("defaults to ja when the cookie is absent", () => {
    expect(browserLocale()).toBe("ja");
  });

  it("reads en from the locale cookie", () => {
    setCookie("en");
    expect(browserLocale()).toBe("en");
  });

  it("falls back to ja for unknown values", () => {
    setCookie("fr");
    expect(browserLocale()).toBe("ja");
  });
});

describe("apiFetch locale header", () => {
  it("sends X-Yukar-Locale matching the cookie", async () => {
    setCookie("en");
    const fetchMock = mockFetchOk();
    await apiFetch("/api/projects");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)[LOCALE_HEADER]).toBe("en");
  });

  it("sends ja when no cookie is set", async () => {
    const fetchMock = mockFetchOk();
    await apiFetch("/api/projects");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)[LOCALE_HEADER]).toBe("ja");
  });

  it("lets an explicit header override the locale header", async () => {
    setCookie("en");
    const fetchMock = mockFetchOk();
    await apiFetch("/api/projects", { headers: { [LOCALE_HEADER]: "ja" } });
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)[LOCALE_HEADER]).toBe("ja");
  });
});
