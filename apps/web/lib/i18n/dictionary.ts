import en from "@/locales/en";
import ja from "@/locales/ja";

export type Locale = "ja" | "en";

// Derive the key structure from ja.ts while relaxing values to string, so other languages such as English are not constrained.
type Loosen<T> = {
  [K in keyof T]: T[K] extends string ? string : Loosen<T[K]>;
};

export type Dict = Loosen<typeof ja>;

const dicts: Record<Locale, Dict> = { ja, en };

export function getDictionary(locale: Locale): Dict {
  return dicts[locale] ?? dicts.ja;
}
