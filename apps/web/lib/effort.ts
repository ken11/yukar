/**
 * Shared manager effort type and option list.
 *
 * The type is derived from the API schema to stay in sync automatically.
 * EFFORT_OPTIONS drives both NewEpicModal and ManagerEffortControl.
 */
import type { Epic } from "@/lib/api/endpoints";

export type ManagerEffort = NonNullable<Epic["manager_effort"]>;

/** Value + i18n key pairs for effort pill rendering. */
export const EFFORT_OPTIONS: { value: ManagerEffort; labelKey: string }[] = [
  { value: "high", labelKey: "epics.effortHigh" },
  { value: "xhigh", labelKey: "epics.effortXhigh" },
  { value: "max", labelKey: "epics.effortMax" },
];
