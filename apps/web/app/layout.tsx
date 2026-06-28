import type { Metadata, Viewport } from "next";
import { Geist, JetBrains_Mono, Noto_Sans_JP } from "next/font/google";
import { Toaster } from "sonner";
import { CommandPalette } from "@/components/features/command-palette/command-palette";
import { getDictionary } from "@/lib/i18n/dictionary";
import { getLocale } from "@/lib/i18n/locale";
import { I18nProvider } from "@/lib/i18n/provider";
import { Providers } from "./providers";
import "./globals.css";

const geist = Geist({ subsets: ["latin"], variable: "--font-geist", display: "swap" });
const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});
const notoJp = Noto_Sans_JP({
  weight: ["400", "500", "600"],
  subsets: ["latin"],
  variable: "--font-noto-jp",
  display: "swap",
  preload: false,
});

export const metadata: Metadata = {
  title: "yukar",
  description: "A local-first autonomous coding agent",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

/**
 * FOUC prevention: reads localStorage or prefers-color-scheme before page paint
 * and adds the .dark class to the html element.
 * SSR defaults to "dark" (the client corrects it afterward).
 */
const themeScript = `(function(){try{var s=localStorage.getItem('yukar-theme');if(s==='dark'){document.documentElement.classList.add('dark');return;}if(s==='light'){document.documentElement.classList.remove('dark');return;}}catch(e){}if(window.matchMedia('(prefers-color-scheme: dark)').matches){document.documentElement.classList.add('dark');}else{document.documentElement.classList.remove('dark');}})();`;

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const locale = await getLocale();
  const dict = getDictionary(locale);

  return (
    <html
      lang={locale}
      className={`dark ${geist.variable} ${jetbrainsMono.variable}${locale === "ja" ? ` ${notoJp.variable}` : ""}`}
      suppressHydrationWarning
    >
      <head>
        {/* biome-ignore lint/security/noDangerouslySetInnerHtml: FOUC prevention script must run synchronously before paint */}
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <Providers>
          <I18nProvider dict={dict} locale={locale}>
            {children}
            <CommandPalette />
          </I18nProvider>
        </Providers>
        <Toaster
          theme="dark"
          position="bottom-right"
          toastOptions={{
            style: {
              background: "var(--color-surface-container-high)",
              border: "1px solid var(--color-outline-variant)",
              color: "var(--color-on-surface)",
            },
          }}
        />
      </body>
    </html>
  );
}
