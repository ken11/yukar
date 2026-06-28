interface EmptyStateProps {
  /** mono single line — machine ID such as scope address */
  address?: string;
  /** Geist body message */
  message: string;
  /** optional CTA */
  action?: React.ReactNode;
}

/**
 * EmptyState — left-aligned, large void (design-language §information-design, §layout).
 * address: mono single line / message: Geist / action: secondary button.
 * items-start / left align: prevents content from feeling too spread out at desktop widths (the floating sensation of center-alignment).
 */
export function EmptyState({ address, message, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-start gap-6 py-[var(--spacing-bay)]">
      {address && <span className="data">{address}</span>}
      <p className="max-w-sm text-body-md text-on-surface-variant">{message}</p>
      {action && <div>{action}</div>}
    </div>
  );
}
