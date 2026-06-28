import { cookies } from "next/headers";
import type { Locale } from "./dictionary";

export const LOCALE_COOKIE = "yukar-locale";

export async function getLocale(): Promise<Locale> {
  const v = (await cookies()).get(LOCALE_COOKIE)?.value;
  return v === "en" ? "en" : "ja";
}
