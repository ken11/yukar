"use client";

import { useMutation } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useState } from "react";
import { Icon } from "@/components/icon";
import type { DocResponse } from "@/lib/api/endpoints";
import { putEpicDoc, putProjectDoc } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";
import { useResetTimer } from "@/lib/hooks/use-reset-timer";
import { langFor } from "@/lib/lang-for";

const CodeMirrorEditor = dynamic(
  () => import("./code-mirror-editor").then((m) => m.CodeMirrorEditor),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-full items-center justify-center text-body-sm text-outline">
        Loading editor…
      </div>
    ),
  },
);

type DocWithScope = DocResponse & { scope: "project" | "epic" };

interface DocsPageClientProps {
  projectId: string;
  epicId: string;
  initialDocs: DocWithScope[];
}

export function DocsPageClient({ projectId, epicId, initialDocs }: DocsPageClientProps) {
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
      if (activeDoc.scope === "project") {
        return putProjectDoc(projectId, activeDoc.filename, { content: activeDoc.content });
      }
      return putEpicDoc(projectId, epicId, activeDoc.filename, { content: activeDoc.content });
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
      <div className="flex h-full items-center justify-center">
        <p className="text-body-sm text-outline">No documents found for this epic or project.</p>
      </div>
    );
  }

  const lang = activeDoc ? langFor(activeDoc.filename) : "markdown";

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Tabs */}
      <div className="flex items-center justify-between border-b border-outline-variant bg-surface-container-lowest">
        <div className="flex items-end overflow-x-auto">
          {docs.map((doc) => (
            <button
              key={doc.filename}
              type="button"
              onClick={() => setActiveFilename(doc.filename)}
              className={cn(
                "flex items-center gap-2 border-b-2 px-5 py-3 text-body-sm transition-colors whitespace-nowrap",
                doc.filename === activeFilename
                  ? "border-primary text-on-surface"
                  : "border-transparent text-on-surface-variant hover:border-outline-variant hover:text-on-surface",
              )}
            >
              <Icon
                name={langFor(doc.filename) === "yaml" ? "data_object" : "description"}
                className="text-[15px]"
              />
              <span className="text-[11px] text-outline mr-1">[{doc.scope}]</span>
              {doc.filename}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2 px-4">
          {saveError && <span className="text-[11px] text-error">{saveError}</span>}
          <button
            type="button"
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending}
            className="flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-body-sm font-medium text-on-primary transition-colors hover:bg-primary-container disabled:opacity-50"
          >
            <Icon
              name={savedFilename === activeFilename ? "check" : "save"}
              className="text-[16px]"
            />
            {saveMutation.isPending
              ? "Saving…"
              : savedFilename === activeFilename
                ? "Saved"
                : "Save Changes"}
          </button>
        </div>
      </div>

      {/* File info bar */}
      {activeDoc && (
        <div className="flex items-center gap-2 border-b border-outline-variant/30 bg-surface-container-lowest px-4 py-1.5 text-[11px] text-outline">
          <Icon name="folder" className="text-[13px]" />
          <code className="font-mono">{activeDoc.filename}</code>
          <span className="ml-1 text-outline/50">[{activeDoc.scope}]</span>
          <span className="ml-auto rounded bg-surface-container px-1.5 py-0.5 font-mono uppercase">
            {lang}
          </span>
        </div>
      )}

      {/* Editor */}
      <div className="flex-1 overflow-hidden">
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
  );
}
