import { cookies } from "next/headers";
import { LOCALE_COOKIE } from "./constants";
import type { Locale } from "./dictionary";

export { LOCALE_COOKIE } from "./constants";

export async function getLocale(): Promise<Locale> {
  const v = (await cookies()).get(LOCALE_COOKIE)?.value;
  return v === "en" ? "en" : "ja";
}
