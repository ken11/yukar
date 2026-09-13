/**
 * Locale plumbing constants shared by server and client code.
 *
 * `LOCALE_COOKIE` is where the UI's language toggle is persisted (set by the
 * `setLocale` server action, read by `getLocale` in RSC).  `LOCALE_HEADER` is
 * how the browser tells the backend which language the user is reading in:
 * `apiFetch` attaches it to every request, and run-starting endpoints hand it
 * to the Manager / Reviewer so they answer in that language.
 */
export const LOCALE_COOKIE = "yukar-locale";
export const LOCALE_HEADER = "X-Yukar-Locale";
