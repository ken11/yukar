import { cn } from "@/lib/cn";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "sm" | "md";
}

export function Button({
  variant = "secondary",
  size = "md",
  className,
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded font-medium transition-colors",
        size === "sm" && "px-3 py-1 text-body-sm",
        size === "md" && "px-4 py-2 text-body-md",
        variant === "primary" &&
          "bg-primary text-on-primary hover:bg-primary-container disabled:opacity-50",
        variant === "secondary" &&
          "border border-outline-variant text-on-surface hover:bg-surface-variant disabled:opacity-50",
        variant === "ghost" &&
          "text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface",
        variant === "danger" &&
          "border border-error/40 bg-error-container/20 text-error hover:bg-error-container/40",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}
