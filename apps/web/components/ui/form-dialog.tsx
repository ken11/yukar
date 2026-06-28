"use client";

import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogTrigger } from "@/components/ui/dialog";

interface FormDialogProps {
  /** Whether the dialog is open */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The element that triggers the dialog */
  trigger: ReactNode;
  /** Dialog title (shown in the header) */
  title: string;
  /** Optional description shown below the title */
  description?: ReactNode;
  /** Optional error string — renders an error banner when truthy */
  error: string | null;
  /** Form fields and content */
  children: ReactNode;
  /** Cancel button label */
  cancelLabel?: string;
  /** Submit button label (non-pending state) */
  submitLabel: ReactNode;
  /** Submit button label while pending */
  submitPendingLabel?: ReactNode;
  /** Whether the submit button should be disabled */
  submitDisabled?: boolean;
  isPending: boolean;
  onSubmit: () => void;
  /** Optional className applied to both Cancel and Submit buttons */
  buttonClassName?: string;
}

/**
 * Common dialog wrapper for create-entity forms.
 * Handles the Dialog/Trigger/error-banner/footer-buttons structure.
 * Each consumer provides its own form fields as children.
 */
export function FormDialog({
  open,
  onOpenChange,
  trigger,
  title,
  description,
  error,
  children,
  cancelLabel = "Cancel",
  submitLabel,
  submitPendingLabel,
  submitDisabled,
  isPending,
  onSubmit,
  buttonClassName,
}: FormDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent title={title}>
        {description && <p className="mb-4 text-body-sm text-on-surface-variant">{description}</p>}

        {error && (
          <div className="mb-4 rounded border border-error/30 bg-error/10 px-3 py-2 text-body-sm text-error">
            {error}
          </div>
        )}

        {children}

        <DialogFooter>
          <Button
            variant="secondary"
            className={buttonClassName}
            onClick={() => onOpenChange(false)}
          >
            {cancelLabel}
          </Button>
          <Button
            variant="primary"
            data-testid="form-dialog-submit"
            className={buttonClassName}
            disabled={submitDisabled || isPending}
            onClick={onSubmit}
          >
            {isPending && submitPendingLabel ? submitPendingLabel : submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
