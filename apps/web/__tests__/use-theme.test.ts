/**
 * Unit tests for the useTheme hook
 * - Runs with a localStorage mock in a jsdom environment
 * - toggleTheme updates the .dark class and localStorage
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTheme } from "@/lib/theme/use-theme";

// ----- localStorage mock -----
let _store: Record<string, string> = {};

const localStorageMock = {
  getItem: vi.fn((key: string): string | null => _store[key] ?? null),
  setItem: vi.fn((key: string, value: string) => {
    _store[key] = value;
  }),
  removeItem: vi.fn((key: string) => {
    delete _store[key];
  }),
  clear: vi.fn(() => {
    _store = {};
  }),
};

Object.defineProperty(window, "localStorage", {
  value: localStorageMock,
  writable: true,
  configurable: true,
});

// ----- matchMedia mock -----
function setupMatchMedia(prefersDark: boolean) {
  Object.defineProperty(window, "matchMedia", {
    value: vi.fn((query: string) => ({
      matches: prefersDark ? query === "(prefers-color-scheme: dark)" : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
    writable: true,
    configurable: true,
  });
}

beforeEach(() => {
  // Reset the store
  _store = {};
  // Reset vi mock call history / implementations
  vi.clearAllMocks();
  // Re-define getItem implementation each time (clearAllMocks wipes it)
  localStorageMock.getItem.mockImplementation((key: string): string | null => _store[key] ?? null);
  localStorageMock.setItem.mockImplementation((key: string, value: string) => {
    _store[key] = value;
  });
  localStorageMock.removeItem.mockImplementation((key: string) => {
    delete _store[key];
  });
  localStorageMock.clear.mockImplementation(() => {
    _store = {};
  });
  // Default: dark
  setupMatchMedia(true);
  // Reset html class
  document.documentElement.className = "";
});

afterEach(() => {
  document.documentElement.className = "";
});

describe("useTheme", () => {
  it("initializes with the dark theme when 'dark' is saved in localStorage", async () => {
    _store["yukar-theme"] = "dark";

    const { result } = renderHook(() => useTheme());

    await act(async () => {});

    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("initializes with the light theme when 'light' is saved in localStorage", async () => {
    _store["yukar-theme"] = "light";

    const { result } = renderHook(() => useTheme());

    await act(async () => {});

    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("defaults to dark when localStorage is empty and prefers-color-scheme is dark", async () => {
    setupMatchMedia(true);
    // _store is empty

    const { result } = renderHook(() => useTheme());

    await act(async () => {});

    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("defaults to light when localStorage is empty and prefers-color-scheme is light", async () => {
    setupMatchMedia(false);
    // _store is empty

    const { result } = renderHook(() => useTheme());

    await act(async () => {});

    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("toggleTheme: switches from dark to light", async () => {
    _store["yukar-theme"] = "dark";
    document.documentElement.classList.add("dark");

    const { result } = renderHook(() => useTheme());

    await act(async () => {
      result.current.toggleTheme();
    });

    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(localStorageMock.setItem).toHaveBeenCalledWith("yukar-theme", "light");
  });

  it("toggleTheme: switches from light to dark", async () => {
    _store["yukar-theme"] = "light";

    const { result } = renderHook(() => useTheme());

    await act(async () => {
      result.current.toggleTheme();
    });

    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorageMock.setItem).toHaveBeenCalledWith("yukar-theme", "dark");
  });

  it("setTheme('dark') adds the dark class and saves to localStorage", async () => {
    _store["yukar-theme"] = "light";

    const { result } = renderHook(() => useTheme());

    await act(async () => {
      result.current.setTheme("dark");
    });

    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorageMock.setItem).toHaveBeenCalledWith("yukar-theme", "dark");
  });

  it("setTheme('light') removes the dark class and saves to localStorage", async () => {
    _store["yukar-theme"] = "dark";
    document.documentElement.classList.add("dark");

    const { result } = renderHook(() => useTheme());

    await act(async () => {
      result.current.setTheme("light");
    });

    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(localStorageMock.setItem).toHaveBeenCalledWith("yukar-theme", "light");
  });
});
