/**
 * Unit tests for the MobileNavDrawer component
 * - Initial state: drawer is closed
 * - Hamburger click: 3 nav links (/projects /usage /settings) are shown
 * - Click again (hamburger): drawer closes
 * - Overlay click: drawer closes
 * - Esc key: drawer closes
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

// Mock next/navigation
const mockPathname = vi.fn(() => "/projects");
vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname(),
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

// Mock next/link
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    className,
    "aria-label": ariaLabel,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
    className?: string;
    "aria-label"?: string;
    [key: string]: unknown;
  }) => (
    <a href={href} className={className} aria-label={ariaLabel} {...rest}>
      {children}
    </a>
  ),
}));

// Mock the setLocale server action for LanguageToggle
vi.mock("@/lib/i18n/actions", () => ({
  setLocale: vi.fn().mockResolvedValue(undefined),
}));

import { MobileNavDrawer } from "@/components/chrome/mobile-nav-drawer";

function renderDrawer(pathname = "/projects") {
  mockPathname.mockReturnValue(pathname);
  return render(
    <I18nProvider dict={ja} locale="ja">
      <MobileNavDrawer />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  // Reset body.overflow
  document.body.style.overflow = "";
});

describe("MobileNavDrawer", () => {
  it("initial state: drawer is closed (-translate-x-full class)", () => {
    renderDrawer();

    // dialog exists in the DOM but translate-x is -100%, so it is closed
    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(dialog.className).toContain("-translate-x-full");

    // Overlay does not exist
    expect(screen.queryByTestId("mobile-nav-overlay")).not.toBeInTheDocument();
  });

  it("hamburger button has an aria-label", () => {
    renderDrawer();
    const btn = screen.getByTestId("hamburger-btn");
    expect(btn).toHaveAttribute("aria-label", "メニューを開く");
  });

  it("hamburger click opens the drawer and shows 3 nav links", async () => {
    const user = userEvent.setup();
    renderDrawer();

    const hamburger = screen.getByTestId("hamburger-btn");
    await user.click(hamburger);

    const dialog = screen.getByRole("dialog");
    expect(dialog.className).not.toContain("-translate-x-full");
    expect(dialog.className).toContain("translate-x-0");

    // Confirm the 3 nav links (/projects /usage /settings)
    const links = screen.getAllByRole("link");
    const hrefs = links.map((l) => l.getAttribute("href"));
    expect(hrefs).toContain("/projects");
    expect(hrefs).toContain("/usage");
    expect(hrefs).toContain("/settings");
  });

  it("clicking the hamburger again closes the drawer", async () => {
    const user = userEvent.setup();
    renderDrawer();

    // Open
    const hamburger = screen.getByTestId("hamburger-btn");
    await user.click(hamburger);

    const dialog = screen.getByRole("dialog");
    expect(dialog.className).toContain("translate-x-0");

    // Click again (aria-label should now be "close")
    await user.click(hamburger);

    expect(dialog.className).toContain("-translate-x-full");
  });

  it("overlay click closes the drawer", async () => {
    const user = userEvent.setup();
    renderDrawer();

    const hamburger = screen.getByTestId("hamburger-btn");
    await user.click(hamburger);

    const overlay = screen.getByTestId("mobile-nav-overlay");
    expect(overlay).toBeInTheDocument();

    await user.click(overlay);

    const dialog = screen.getByRole("dialog");
    expect(dialog.className).toContain("-translate-x-full");
    expect(screen.queryByTestId("mobile-nav-overlay")).not.toBeInTheDocument();
  });

  it("Esc key closes the drawer", async () => {
    const user = userEvent.setup();
    renderDrawer();

    const hamburger = screen.getByTestId("hamburger-btn");
    await user.click(hamburger);

    const dialog = screen.getByRole("dialog");
    expect(dialog.className).toContain("translate-x-0");

    fireEvent.keyDown(document, { key: "Escape" });

    expect(dialog.className).toContain("-translate-x-full");
  });

  it("has role=dialog and aria-modal=true", () => {
    renderDrawer();

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("body.overflow is hidden while the drawer is open", async () => {
    const user = userEvent.setup();
    renderDrawer();

    const hamburger = screen.getByTestId("hamburger-btn");
    await user.click(hamburger);

    expect(document.body.style.overflow).toBe("hidden");
  });

  it("the current path is shown as active (when on /usage)", async () => {
    const user = userEvent.setup();
    renderDrawer("/usage");

    const hamburger = screen.getByTestId("hamburger-btn");
    await user.click(hamburger);

    // The /usage link has the active class (bg-surface-container-high)
    const usageLinks = screen
      .getAllByRole("link")
      .filter((l) => l.getAttribute("href") === "/usage");
    expect(usageLinks.some((l) => l.className.includes("bg-surface-container-high"))).toBe(true);
  });
});
