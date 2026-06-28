/**
 * Tests for the ThemeToggle component
 * - Theme toggles on click
 * - aria-label is set correctly
 * - Tap target size is appropriate
 */

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

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

import { ThemeToggle } from "@/components/ui/theme-toggle";

function renderToggle() {
  return render(
    <I18nProvider dict={ja} locale="ja">
      <ThemeToggle />
    </I18nProvider>,
  );
}

beforeEach(() => {
  _store = {};
  vi.clearAllMocks();
  localStorageMock.getItem.mockImplementation((key: string): string | null => _store[key] ?? null);
  localStorageMock.setItem.mockImplementation((key: string, value: string) => {
    _store[key] = value;
  });
  // Default: dark
  setupMatchMedia(true);
  document.documentElement.className = "";
});

afterEach(() => {
  cleanup();
  document.documentElement.className = "";
});

describe("ThemeToggle", () => {
  it("has 'switch to light mode' aria-label when in dark theme", async () => {
    _store["yukar-theme"] = "dark";
    document.documentElement.classList.add("dark");

    renderToggle();

    // act for useEffect in useTheme
    await new Promise((resolve) => setTimeout(resolve, 0));

    const btn = screen.getByRole("button");
    expect(btn).toHaveAttribute("aria-label", ja.common.theme.switchToLight);
  });

  it("has 'switch to dark mode' aria-label when in light theme", async () => {
    _store["yukar-theme"] = "light";
    setupMatchMedia(false);

    renderToggle();

    await new Promise((resolve) => setTimeout(resolve, 0));

    const btn = screen.getByRole("button");
    expect(btn).toHaveAttribute("aria-label", ja.common.theme.switchToDark);
  });

  it("click switches from dark to light (dark class is removed)", async () => {
    const user = userEvent.setup();
    _store["yukar-theme"] = "dark";
    document.documentElement.classList.add("dark");

    renderToggle();

    const btn = screen.getByRole("button");
    await user.click(btn);

    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(localStorageMock.setItem).toHaveBeenCalledWith("yukar-theme", "light");
  });

  it("click switches from light to dark (dark class is added)", async () => {
    const user = userEvent.setup();
    _store["yukar-theme"] = "light";
    setupMatchMedia(false);

    renderToggle();

    const btn = screen.getByRole("button");
    await user.click(btn);

    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorageMock.setItem).toHaveBeenCalledWith("yukar-theme", "dark");
  });

  it("button tap target is at least 44px (h-11 = 44px)", () => {
    renderToggle();

    const btn = screen.getByRole("button");
    expect(btn.className).toContain("h-11");
    expect(btn.className).toContain("w-11");
  });
});
