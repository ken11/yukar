"use client";

import * as RadixDialog from "@radix-ui/react-dialog";
import { Icon } from "@/components/icon";
import { cn } from "@/lib/cn";

export const Dialog = RadixDialog.Root;
export const DialogTrigger = RadixDialog.Trigger;
export const DialogClose = RadixDialog.Close;

export function DialogContent({
  children,
  className,
  title,
}: {
  children: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <RadixDialog.Portal>
      <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm" />
      {/*
       * aria-describedby={undefined}: opt out of the Radix a11y warning when no Description is present.
       * A Description is unnecessary because the visible content provides sufficient explanation.
       */}
      <RadixDialog.Content
        aria-describedby={undefined}
        className={cn(
          "fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border border-outline-variant bg-surface-container p-4 shadow-xl md:w-full md:p-6",
          "max-h-[calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)-2rem)] overflow-y-auto",
          className,
        )}
      >
        <div className="mb-4 flex items-center justify-between">
          {/* Title is always rendered. When not specified, hidden visually with sr-only (required for a11y) */}
          {title ? (
            <RadixDialog.Title className="text-headline-sm font-semibold text-on-surface">
              {title}
            </RadixDialog.Title>
          ) : (
            <RadixDialog.Title className="sr-only">Dialog</RadixDialog.Title>
          )}
          <RadixDialog.Close className="ml-auto rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-on-surface">
            <Icon name="close" className="text-[18px]" />
          </RadixDialog.Close>
        </div>
        {children}
      </RadixDialog.Content>
    </RadixDialog.Portal>
  );
}

export function DialogFooter({ children }: { children: React.ReactNode }) {
  return <div className="mt-6 flex justify-end gap-3">{children}</div>;
}
