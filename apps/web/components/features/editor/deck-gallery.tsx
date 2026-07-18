"use client";

import { Icon } from "@/components/icon";
import type { DeckMeta } from "@/lib/api/endpoints";
import { epicDeckPreviewUrl, epicDeckUrl } from "@/lib/api/endpoints";
import { useT } from "@/lib/i18n/provider";

/**
 * The epic's Manager-rendered slide decks: per deck a download link for the
 * .pptx plus the slide-preview gallery from its last previewed render.
 * Previews open full size in a new tab; there is no delete here — the deck
 * is the Manager's work product and is revised through conversation.
 *
 * Times render straight from the ISO string (already JST) so server and
 * client agree — no `Date` locale/timezone hydration drift.
 */
export function DeckGallery({
  projectId,
  epicId,
  decks,
}: {
  projectId: string;
  epicId: string;
  decks: DeckMeta[];
}) {
  const t = useT();

  if (decks.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <p className="max-w-sm text-center text-body-sm text-outline">
          {t("docsEditor.decksEmpty")}
        </p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="flex flex-col gap-6">
        {decks.map((deck) => {
          const updated = deck.updated_at.slice(0, 16).replace("T", " ");
          const kb = Math.max(1, Math.round(deck.size_bytes / 1024));
          return (
            <section key={deck.path}>
              <div className="mb-2 flex items-center gap-3">
                <Icon name="slideshow" className="text-[18px] text-on-surface-variant" />
                <span className="truncate font-mono text-body-sm text-on-surface" title={deck.path}>
                  {deck.path}
                </span>
                <span className="shrink-0 text-[11px] tabular-nums text-outline">
                  {updated} · {kb} KB
                </span>
                <a
                  href={epicDeckUrl(projectId, epicId, deck.path)}
                  className="ml-auto flex shrink-0 items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-body-sm font-medium text-on-primary transition-colors hover:bg-primary-container"
                >
                  <Icon name="download" className="text-[16px]" />
                  {t("docsEditor.deckDownload")}
                </a>
              </div>
              {deck.previews.length === 0 ? (
                <p className="text-[11px] text-outline">{t("docsEditor.deckNoPreviews")}</p>
              ) : (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
                  {deck.previews.map((name, i) => {
                    const url = epicDeckPreviewUrl(projectId, epicId, deck.path, name);
                    return (
                      <a
                        key={name}
                        href={url}
                        target="_blank"
                        rel="noreferrer"
                        title={t("docsEditor.openFull")}
                        className="group relative block overflow-hidden rounded-md border border-outline-variant bg-surface-container-lowest"
                      >
                        {/* biome-ignore lint/performance/noImgElement: raw same-origin API bytes, not a static asset */}
                        <img
                          src={url}
                          alt={`${deck.path} — ${name}`}
                          loading="lazy"
                          className="aspect-video w-full object-cover transition-transform group-hover:scale-[1.02]"
                        />
                        <span className="absolute bottom-1 left-1.5 rounded bg-surface/80 px-1.5 text-[11px] tabular-nums text-on-surface-variant backdrop-blur-sm">
                          {i + 1}
                        </span>
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
