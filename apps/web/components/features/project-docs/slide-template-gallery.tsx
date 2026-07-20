"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Icon } from "@/components/icon";
import type { SlideTemplateMeta } from "@/lib/api/endpoints";
import { deleteSlideTemplate, slideTemplatePreviewUrl } from "@/lib/api/endpoints";
import { useT } from "@/lib/i18n/provider";

/**
 * Project-level slide templates: reusable deck designs saved by the Manager
 * (pptx_save_template) for future epics to start from.  Each template shows
 * its two thumbnails (cover + a body slide — the cover alone often
 * misrepresents the design), metadata, and a delete button; templates are
 * created only through the Manager tools, so there is no upload here.
 *
 * Times render straight from the ISO string (already JST) so server and
 * client agree — no `Date` locale/timezone hydration drift.
 */
export function SlideTemplateGallery({
  projectId,
  templates,
  onDeleted,
}: {
  projectId: string;
  templates: SlideTemplateMeta[];
  onDeleted: (name: string) => void;
}) {
  const t = useT();
  const [error, setError] = useState<string | null>(null);

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteSlideTemplate(projectId, name),
    onMutate: () => setError(null),
    onSuccess: (_data, name) => onDeleted(name),
    onError: (err) =>
      setError(err instanceof Error ? err.message : t("projectDocs.templateDeleteFailed")),
  });

  if (templates.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <p className="max-w-sm text-center text-body-sm text-outline">
          {t("projectDocs.templatesEmpty")}
        </p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-4">
      {error && <p className="mb-3 text-body-sm text-error">{error}</p>}
      <div className="flex flex-col gap-6">
        {templates.map((tpl) => {
          const created = tpl.created_at.slice(0, 16).replace("T", " ");
          return (
            <section key={tpl.name}>
              <div className="mb-2 flex items-center gap-3">
                <Icon name="dashboard_customize" className="text-[18px] text-on-surface-variant" />
                <span className="truncate font-mono text-body-sm text-on-surface" title={tpl.name}>
                  {tpl.name}
                </span>
                <span className="shrink-0 text-[11px] tabular-nums text-outline">
                  {tpl.slide_count} {t("projectDocs.templateSlides")} · {tpl.size} · {created}
                </span>
                <button
                  type="button"
                  aria-label={t("projectDocs.templateDeleteLabel")}
                  disabled={deleteMutation.isPending && deleteMutation.variables === tpl.name}
                  onClick={() => {
                    if (window.confirm(t("projectDocs.templateDeleteConfirm"))) {
                      deleteMutation.mutate(tpl.name);
                    }
                  }}
                  className="ml-auto flex h-7 w-7 shrink-0 items-center justify-center rounded text-on-surface-variant transition-colors hover:bg-error hover:text-on-error disabled:opacity-50"
                >
                  <Icon name="delete" className="text-[16px]" />
                </button>
              </div>
              {tpl.description && (
                <p className="mb-2 text-body-sm text-on-surface-variant">{tpl.description}</p>
              )}
              {tpl.previews.length === 0 ? (
                <p className="text-[11px] text-outline">{t("projectDocs.templateNoPreviews")}</p>
              ) : (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
                  {tpl.previews.map((name) => {
                    const url = slideTemplatePreviewUrl(projectId, tpl.name, name);
                    return (
                      <a
                        key={name}
                        href={url}
                        target="_blank"
                        rel="noreferrer"
                        title={t("projectDocs.templateOpenFull")}
                        className="group relative block overflow-hidden rounded-md border border-outline-variant bg-surface-container-lowest"
                      >
                        {/* biome-ignore lint/performance/noImgElement: raw same-origin API bytes, not a static asset */}
                        <img
                          src={url}
                          alt={`${tpl.name} — ${name}`}
                          loading="lazy"
                          className="aspect-video w-full object-cover transition-transform group-hover:scale-[1.02]"
                        />
                      </a>
                    );
                  })}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
