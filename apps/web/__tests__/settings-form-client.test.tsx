/**
 * settings-form-client: blank→null conversion for nullable fields and verification of PUT on save
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SettingsFormClient } from "@/components/features/settings/settings-form-client";
import type { Settings } from "@/lib/api/endpoints";
import { getSettings, putSettings } from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    getSettings: vi.fn(),
    putSettings: vi.fn(),
  };
});

afterEach(() => {
  vi.restoreAllMocks();
});

function makeSettings(overrides: Partial<Settings> = {}): Settings {
  return {
    workspace_root: "~/yukar-projects",
    llm: {
      provider: "bedrock",
      model_id: "us.anthropic.claude-sonnet-4-6-20251201-v1:0",
      max_tokens: 8192,
      prompt_caching: true,
      request_timeout: 900,
      summarization: {
        enabled: true,
        summary_ratio: 0.3,
        preserve_recent_messages: 10,
        proactive_compression_threshold: null,
      },
    },
    embedding: {
      provider: "bedrock",
      model_id: "amazon.titan-embed-text-v2:0",
      region: null,
      dimensions: null,
    },
    agent: {
      max_parallel_epics: 2,
      max_parallel_workers: 4,
      worker_max_turns: 60,
      evaluator_max_turns: 20,
      worker_max_total_tokens: null,
      evaluator_max_total_tokens: null,
    },
    git: { author_name: "yukar", author_email: "yukar@localhost" },
    indexer: { watch: true },
    ...overrides,
  };
}

function wrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      </I18nProvider>
    );
  };
}

describe("SettingsFormClient — blank→null conversion for nullable fields", () => {
  it("PUTs null when embedding.region is blank", async () => {
    const settings = makeSettings({
      embedding: { provider: "bedrock", model_id: "m", region: "ap-northeast-1", dimensions: null },
    });
    vi.mocked(getSettings).mockResolvedValue(settings);
    vi.mocked(putSettings).mockResolvedValue(settings);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<SettingsFormClient initialSettings={settings} />, { wrapper: wrapper(qc) });

    const regionInput = screen.getByRole<HTMLInputElement>("textbox", {
      name: /リージョン/i,
    });
    await user.clear(regionInput);

    await user.click(screen.getByRole("button", { name: /保存する/i }));

    await waitFor(() => {
      expect(putSettings).toHaveBeenCalledOnce();
    });

    const calledWith = vi.mocked(putSettings).mock.calls[0][0];
    expect(calledWith.embedding?.region).toBeNull();
  });

  it("PUTs null when embedding.dimensions is blank", async () => {
    const settings = makeSettings({
      embedding: { provider: "bedrock", model_id: "m", region: null, dimensions: 1024 },
    });
    vi.mocked(getSettings).mockResolvedValue(settings);
    vi.mocked(putSettings).mockResolvedValue(settings);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<SettingsFormClient initialSettings={settings} />, { wrapper: wrapper(qc) });

    const dimInput = screen.getByRole<HTMLInputElement>("spinbutton", {
      name: /次元数/i,
    });
    await user.clear(dimInput);

    await user.click(screen.getByRole("button", { name: /保存する/i }));

    await waitFor(() => {
      expect(putSettings).toHaveBeenCalledOnce();
    });

    const calledWith = vi.mocked(putSettings).mock.calls[0][0];
    expect(calledWith.embedding?.dimensions).toBeNull();
  });

  it("PUTs null when summarization.proactive_compression_threshold is blank", async () => {
    const settings = makeSettings({
      llm: {
        provider: "bedrock",
        model_id: "m",
        max_tokens: 8192,
        prompt_caching: true,
        request_timeout: 900,
        summarization: {
          enabled: true,
          summary_ratio: 0.3,
          preserve_recent_messages: 10,
          proactive_compression_threshold: 0.8,
        },
      },
    });
    vi.mocked(getSettings).mockResolvedValue(settings);
    vi.mocked(putSettings).mockResolvedValue(settings);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<SettingsFormClient initialSettings={settings} />, { wrapper: wrapper(qc) });

    const thresholdInput = screen.getByRole<HTMLInputElement>("spinbutton", {
      name: /先行圧縮の閾値/i,
    });
    await user.clear(thresholdInput);

    await user.click(screen.getByRole("button", { name: /保存する/i }));

    await waitFor(() => {
      expect(putSettings).toHaveBeenCalledOnce();
    });

    const calledWith = vi.mocked(putSettings).mock.calls[0][0];
    expect(calledWith.llm?.summarization?.proactive_compression_threshold).toBeNull();
  });

  it("putSettings is called when Save is clicked with all fields filled", async () => {
    const settings = makeSettings();
    vi.mocked(getSettings).mockResolvedValue(settings);
    vi.mocked(putSettings).mockResolvedValue(settings);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<SettingsFormClient initialSettings={settings} />, { wrapper: wrapper(qc) });

    await user.click(screen.getByRole("button", { name: /保存する/i }));

    await waitFor(() => {
      expect(putSettings).toHaveBeenCalledOnce();
    });

    const calledWith = vi.mocked(putSettings).mock.calls[0][0];
    // Confirm all sections are included
    expect(calledWith.llm).toBeDefined();
    expect(calledWith.embedding).toBeDefined();
    expect(calledWith.indexer).toBeDefined();
    expect(calledWith.agent).toBeDefined();
    expect(calledWith.git).toBeDefined();
    // summarization is included inside llm
    expect(calledWith.llm?.summarization).toBeDefined();
  });

  it("form is shown with defaultSettings when initialSettings is null", () => {
    vi.mocked(getSettings).mockResolvedValue(makeSettings());

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<SettingsFormClient initialSettings={null} />, { wrapper: wrapper(qc) });

    // The default value for workspace_root is displayed
    expect(screen.getByDisplayValue("~/yukar-projects")).toBeInTheDocument();
  });

  it("renders a Reviewer per-role model-override input and PUTs roles.reviewer.model_id", async () => {
    const settings = makeSettings();
    vi.mocked(getSettings).mockResolvedValue(settings);
    vi.mocked(putSettings).mockResolvedValue(settings);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<SettingsFormClient initialSettings={settings} />, { wrapper: wrapper(qc) });

    // The Reviewer role override input exists (regression: it used to be missing,
    // so the Reviewer's model could not be configured from the UI).
    const reviewerInput = screen.getByRole<HTMLInputElement>("textbox", { name: /reviewer/i });
    await user.type(reviewerInput, "us.anthropic.claude-opus-4-8-v1:0");

    await user.click(screen.getByRole("button", { name: /保存する/i }));

    await waitFor(() => {
      expect(putSettings).toHaveBeenCalledOnce();
    });
    const calledWith = vi.mocked(putSettings).mock.calls[0][0];
    expect(calledWith.llm?.roles?.reviewer?.model_id).toBe("us.anthropic.claude-opus-4-8-v1:0");
  });
});
