"use client";

import { type QueryKey, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

export interface UseModalMutationOptions<TVariables = void> {
  mutationFn: (vars: TVariables) => Promise<unknown>;
  /** Query keys to invalidate on success */
  invalidateKeys?: readonly QueryKey[];
  onSuccess?: () => void;
  /** Fallback error message when the thrown error is not an Error instance */
  fallbackError?: string;
}

export interface UseModalMutationResult<TVariables = void> {
  isOpen: boolean;
  setOpen: (open: boolean) => void;
  error: string | null;
  setError: (err: string | null) => void;
  isPending: boolean;
  submit: (vars: TVariables) => void;
}

/**
 * Shared hook for Dialog + useMutation patterns.
 * Handles open state, error display, invalidation, and close-on-success.
 */
export function useModalMutation<TVariables = void>({
  mutationFn,
  invalidateKeys = [],
  onSuccess,
  fallbackError = "Operation failed",
}: UseModalMutationOptions<TVariables>): UseModalMutationResult<TVariables> {
  const [isOpen, setIsOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn,
    onSuccess: () => {
      for (const key of invalidateKeys) {
        qc.invalidateQueries({ queryKey: key });
      }
      setIsOpen(false);
      setError(null);
      onSuccess?.();
    },
    onError: (err) => {
      setError(err instanceof Error ? err.message : fallbackError);
    },
  });

  return {
    isOpen,
    setOpen: setIsOpen,
    error,
    setError,
    isPending: mutation.isPending,
    submit: mutation.mutate,
  };
}
