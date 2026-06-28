"use client";

import { useMutation } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useState } from "react";
import { EmptyState } from "@/components/ui/empty-state";
import type { DocResponse } from "@/lib/api/endpoints";
import { putProjectDoc } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";
import { useResetTimer } from "@/lib/hooks/use-reset-timer";
import { useT } from "@/lib/i18n/provider";
import { langFor } from "@/lib/lang-for";

const CodeMirrorEditor = dynamic(
  () => import("@/components/features/editor/code-mirror-editor").then((m) => m.CodeMirrorEditor),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-full items-center justify-center text-body-sm text-outline">
        Loading editor…
      </div>
    ),
  },
);

interface ProjectDocsClientProps {
  projectId: string;
  initialDocs: DocResponse[];
}

export function ProjectDocsClient({ projectId, initialDocs }: ProjectDocsClientProps) {
  const t = useT();
  const scheduleReset = useResetTimer();
  const [docs, setDocs] = useState(initialDocs);
  const [activeFilename, setActiveFilename] = useState(initialDocs[0]?.filename ?? "");
  const [savedFilename, setSavedFilename] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const activeDoc = docs.find((d) => d.filename === activeFilename) ?? docs[0];

  function handleChange(value: string) {
    setDocs((prev) =>
      prev.map((d) => (d.filename === activeFilename ? { ...d, content: value } : d)),
    );
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!activeDoc) return;
      return putProjectDoc(projectId, activeDoc.filename, { content: activeDoc.content });
    },
    onSuccess: () => {
      setSavedFilename(activeFilename);
      setSaveError(null);
      scheduleReset(() => setSavedFilename(null));
    },
    onError: (err) => {
      setSaveError(err instanceof Error ? err.message : "Save failed");
    },
  });

  if (docs.length === 0) {
    return (
      <div className="px-10 py-8">
        <EmptyState address={`${projectId} / docs`} message={t("projectDocs.emptyMessage")} />
      </div>
    );
  }

  const lang = activeDoc ? langFor(activeDoc.filename) : "markdown";

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left: file list */}
      <nav
        aria-label={t("projectDocs.fileListLabel")}
        className="flex w-56 shrink-0 flex-col overflow-y-auto border-r border-outline-variant/40 bg-surface-container-lowest py-2"
      >
        {docs.map((doc) => {
          const isActive = doc.filename === activeFilename;
          return (
            <button
              key={doc.filename}
              type="button"
              onClick={() => setActiveFilename(doc.filename)}
              aria-current={isActive ? "true" : undefined}
              className={cn(
                "flex items-center gap-2 px-4 py-2 text-left transition-colors",
                isActive
                  ? "bg-surface-container-highest text-on-surface"
                  : "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
              )}
            >
              {/* white tick for active */}
              <span
                aria-hidden
                className={cn(
                  "shrink-0 text-[13px]",
                  isActive ? "text-on-surface" : "text-transparent",
                )}
              >
                ✓
              </span>
              <span className="data truncate">{doc.filename}</span>
            </button>
          );
        })}
      </nav>

      {/* Right: editor */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* File info bar + Save */}
        <div className="flex items-center justify-between border-b border-outline-variant/40 bg-surface-container-lowest px-4 py-2">
          <div className="flex items-center gap-2">
            <span className="data text-outline">{activeDoc?.filename}</span>
            <span className="data rounded border border-outline-variant/30 px-1.5 py-0.5 uppercase text-outline">
              {lang}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {saveError && <span className="text-[11px] text-error">{saveError}</span>}
            <button
              type="button"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
              aria-label={t("projectDocs.saveDocLabel")}
              className="flex items-center gap-1.5 rounded bg-on-surface px-3 py-1.5 text-[12px] font-medium text-surface transition-colors hover:opacity-90 disabled:opacity-50"
              style={{ color: "var(--color-surface)", backgroundColor: "var(--color-on-surface)" }}
            >
              {saveMutation.isPending
                ? "Saving…"
                : savedFilename === activeFilename
                  ? "Saved"
                  : "Save"}
            </button>
          </div>
        </div>

        {/* CodeMirror editor */}
        <div className="flex-1 overflow-hidden bg-surface-container-lowest">
          {activeDoc && (
            <CodeMirrorEditor
              key={activeFilename}
              value={activeDoc.content}
              onChange={handleChange}
              language={lang}
            />
          )}
        </div>
      </div>
    </div>
  );
}
