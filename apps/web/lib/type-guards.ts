/** Narrowing guard: true (and narrows out null/undefined) when the value is present. */
export function isDefined<T>(value: T | null | undefined): value is T {
  return value != null;
}
