"use client";

// ---- shared primitives for SettingsFormClient ------------------------------------------------

export function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-5 text-[11px] font-medium uppercase tracking-[0.05em] text-on-surface-variant">
      {children}
    </p>
  );
}

export function FieldLabel({ htmlFor, children }: { htmlFor?: string; children: React.ReactNode }) {
  return (
    <label
      htmlFor={htmlFor}
      className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
    >
      {children}
    </label>
  );
}

export function FieldHint({ children }: { children: React.ReactNode }) {
  return <p className="mt-1 text-[11px] text-outline">{children}</p>;
}

export function Field({
  id,
  label,
  hint,
  children,
}: {
  id?: string;
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      {children}
      {hint && <FieldHint>{hint}</FieldHint>}
    </div>
  );
}

export const inputClass =
  "w-full rounded border border-outline-variant bg-surface-container-lowest px-3 py-2 text-[14px] text-on-surface placeholder:text-outline focus:border-outline focus:outline-none focus:ring-1 focus:ring-white/20";

// recess surface for code/data inputs
export const inputMonoClass =
  "w-full rounded border border-outline-variant bg-surface-container-lowest px-3 py-2 font-mono text-[13px] text-on-surface placeholder:text-outline focus:border-outline focus:outline-none focus:ring-1 focus:ring-white/20";

// multi-line code/data input (no resize by default; callers may add "resize-y" via cn())
export const textareaClass =
  "w-full rounded border border-outline-variant bg-surface-container-lowest px-3 py-2 font-mono text-[13px] text-on-surface placeholder:text-outline focus:border-outline focus:outline-none focus:ring-1 focus:ring-white/20 resize-none";

export function ToggleSwitch({
  id,
  value,
  onChange,
  labelOn,
  labelOff,
}: {
  id?: string;
  value: boolean;
  onChange: (v: boolean) => void;
  labelOn: string;
  labelOff: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={value}
        onClick={() => onChange(!value)}
        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-white/30 focus:ring-offset-1 focus:ring-offset-surface ${
          value ? "bg-[var(--color-light)]" : "bg-outline-variant"
        }`}
      >
        <span
          className={`inline-block h-3.5 w-3.5 rounded-full bg-surface transition-transform ${
            value ? "translate-x-4" : "translate-x-0.5"
          }`}
        />
      </button>
      <span className="text-[13px] text-on-surface-variant">{value ? labelOn : labelOff}</span>
    </div>
  );
}

export function ProviderPills<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex gap-2">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={`rounded border px-4 py-1.5 text-[13px] transition-colors focus:outline-none focus:ring-1 focus:ring-white/30 ${
            value === opt.value
              ? "border-[var(--color-light)]/40 bg-[var(--color-light)]/10 text-[var(--color-light)]"
              : "border-outline-variant text-on-surface-variant hover:border-outline hover:text-on-surface"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
