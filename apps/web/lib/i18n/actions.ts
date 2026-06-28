"use server";

import { cookies } from "next/headers";
import type { Locale } from "./dictionary";
import { LOCALE_COOKIE } from "./locale";

export async function setLocale(locale: Locale) {
  (await cookies()).set(LOCALE_COOKIE, locale, {
    path: "/",
    maxAge: 31536000,
    sameSite: "lax",
  });
}
