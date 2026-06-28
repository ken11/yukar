import NextLink from "next/link";
import { cn } from "@/lib/cn";

export interface AddressSegment {
  label: string;
  active?: boolean;
  href?: string;
}

interface AddressLineProps {
  segments: AddressSegment[];
  className?: string;
}

/**
 * AddressLine — joins scope segments with a full-width slash.
 * Active segments are white 600.
 */
export function AddressLine({ segments, className }: AddressLineProps) {
  return (
    <span className={cn("address inline-flex flex-wrap items-center gap-0", className)}>
      {segments.map((seg, i) => {
        const isLast = i === segments.length - 1;
        const segEl = (
          <span className={cn("address", seg.active && "address-active")}>
            {seg.href ? (
              <NextLink href={seg.href} className="hover:underline">
                {seg.label}
              </NextLink>
            ) : (
              seg.label
            )}
          </span>
        );

        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional and stable
          <span key={i} className="inline-flex items-center">
            {segEl}
            {!isLast && <span className="address mx-1 select-none opacity-40">／</span>}
          </span>
        );
      })}
    </span>
  );
}
