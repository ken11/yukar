/**
 * Slide template gallery + project docs tabs:
 * - gallery: metadata row, two thumbnails per template, delete flow
 *   (confirm → deleteSlideTemplate → onDeleted), empty state
 * - ProjectDocsClient: templates tab appears and is the default when the
 *   project has templates but no documents (no editor mount involved)
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectDocsClient } from "@/components/features/project-docs/project-docs-client";
import { SlideTemplateGallery } from "@/components/features/project-docs/slide-template-gallery";
import type { SlideTemplateMeta } from "@/lib/api/endpoints";
import { deleteSlideTemplate } from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    deleteSlideTemplate: vi.fn(),
  };
});

const mockedDelete = vi.mocked(deleteSlideTemplate);

function wrap(children: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <I18nProvider dict={ja} locale="ja">
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </I18nProvider>,
  );
}

const corp: SlideTemplateMeta = {
  name: "corp",
  description: "コーポレート調",
  slide_count: 5,
  size: "16:9",
  created_at: "2026-07-20T10:00:00+09:00",
  previews: ["slide-01.jpg", "slide-02.jpg"],
  has_notes: true,
};

afterEach(() => {
  mockedDelete.mockReset();
});

describe("SlideTemplateGallery", () => {
  it("renders name, description, and both thumbnails", () => {
    wrap(<SlideTemplateGallery projectId="p1" templates={[corp]} onDeleted={() => {}} />);
    expect(screen.getByText("corp")).toBeInTheDocument();
    expect(screen.getByText("コーポレート調")).toBeInTheDocument();
    const images = screen.getAllByRole("img");
    expect(images).toHaveLength(2);
    expect(images[0]).toHaveAttribute(
      "src",
      "/api/projects/p1/slide-templates/corp/previews/slide-01.jpg",
    );
  });

  it("deletes after confirm and reports the name", async () => {
    const user = userEvent.setup();
    const onDeleted = vi.fn();
    mockedDelete.mockResolvedValueOnce(undefined);
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    wrap(<SlideTemplateGallery projectId="p1" templates={[corp]} onDeleted={onDeleted} />);
    await user.click(screen.getByRole("button", { name: "テンプレートを削除" }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith("corp"));
    expect(mockedDelete).toHaveBeenCalledWith("p1", "corp");
  });

  it("does not delete when the confirm is declined", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    wrap(<SlideTemplateGallery projectId="p1" templates={[corp]} onDeleted={() => {}} />);
    await user.click(screen.getByRole("button", { name: "テンプレートを削除" }));
    expect(mockedDelete).not.toHaveBeenCalled();
  });

  it("shows the empty message without templates", () => {
    wrap(<SlideTemplateGallery projectId="p1" templates={[]} onDeleted={() => {}} />);
    expect(screen.getByText(/まだスライドテンプレートはありません/)).toBeInTheDocument();
  });
});

describe("ProjectDocsClient tabs", () => {
  it("defaults to the templates tab when there are templates but no docs", async () => {
    const user = userEvent.setup();
    wrap(<ProjectDocsClient projectId="p1" initialDocs={[]} initialTemplates={[corp]} />);
    // Gallery visible by default…
    expect(screen.getByText("corp")).toBeInTheDocument();
    // …and switching to the documents tab shows the docs empty state.
    await user.click(screen.getByRole("button", { name: /ドキュメント/ }));
    expect(screen.getByText(/このプロジェクトにはドキュメントがありません/)).toBeInTheDocument();
  });
});
