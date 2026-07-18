"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Icon } from "@/components/icon";
import type { ScreenshotMeta } from "@/lib/api/endpoints";
import { deleteEpicScreenshot, epicScreenshotUrl } from "@/lib/api/endpoints";
import { useT } from "@/lib/i18n/provider";

/**
 * Grid of the epic's saved browser-verification screenshots. Each thumbnail
 * links to the raw image (opens full size in a new tab); a hover delete button
 * removes it — saved shots are opt-in, so the user stays in control of disk.
 *
 * The capture time is rendered straight from the ISO string (already JST) so
 * server and client agree — no `Date` locale/timezone hydration drift.
 */
export function ScreenshotGallery({
  projectId,
  epicId,
  screenshots,
  onDeleted,
}: {
  projectId: string;
  epicId: string;
  screenshots: ScreenshotMeta[];
  onDeleted: (filename: string) => void;
}) {
  const t = useT();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const deleteMutation = useMutation({
    mutationFn: (filename: string) => deleteEpicScreenshot(projectId, epicId, filename),
    onMutate: (filename) => {
      setPending(filename);
      setError(null);
    },
    onSuccess: (_data, filename) => onDeleted(filename),
    onError: (err) => setError(err instanceof Error ? err.message : t("docsEditor.deleteFailed")),
    onSettled: () => setPending(null),
  });

  if (screenshots.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <p className="max-w-sm text-center text-body-sm text-outline">
          {t("docsEditor.screenshotsEmpty")}
        </p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-4">
      {error && <p className="mb-3 text-body-sm text-error">{error}</p>}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-4">
        {screenshots.map((shot) => {
          const url = epicScreenshotUrl(projectId, epicId, shot.filename);
          const captured = shot.captured_at.slice(0, 16).replace("T", " ");
          const kb = Math.max(1, Math.round(shot.size_bytes / 1024));
          return (
            <figure
              key={shot.filename}
              className="group relative overflow-hidden rounded-md border border-outline-variant bg-surface-container-lowest"
            >
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                title={t("docsEditor.openFull")}
                className="block aspect-video overflow-hidden bg-surface-container"
              >
                {/* biome-ignore lint/performance/noImgElement: raw same-origin API bytes, not a static asset */}
                <img
                  src={url}
                  alt={shot.filename}
                  loading="lazy"
                  className="h-full w-full object-cover transition-transform group-hover:scale-[1.02]"
                />
              </a>
              <button
                type="button"
                aria-label={t("docsEditor.deleteLabel")}
                disabled={pending === shot.filename}
                onClick={() => {
                  if (window.confirm(t("docsEditor.deleteConfirm"))) {
                    deleteMutation.mutate(shot.filename);
                  }
                }}
                className="absolute right-1.5 top-1.5 flex h-7 w-7 items-center justify-center rounded bg-surface/80 text-on-surface-variant opacity-0 backdrop-blur-sm transition-opacity hover:bg-error hover:text-on-error focus-visible:opacity-100 group-hover:opacity-100 disabled:opacity-50"
              >
                <Icon name="delete" className="text-[16px]" />
              </button>
              <figcaption className="flex items-center justify-between gap-2 px-2.5 py-1.5 text-[11px] text-outline">
                <span className="truncate font-mono" title={shot.filename}>
                  {captured}
                </span>
                <span className="shrink-0 tabular-nums text-outline/70">{kb} KB</span>
              </figcaption>
            </figure>
          );
        })}
      </div>
    </div>
  );
}
