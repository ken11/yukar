import { SettingsFormClient } from "@/components/features/settings/settings-form-client";
import { getSettings } from "@/lib/api/endpoints";
import { getDictionary } from "@/lib/i18n/dictionary";
import { getLocale } from "@/lib/i18n/locale";

export default async function SettingsPage() {
  const locale = await getLocale();
  const t = getDictionary(locale);
  const settings = await getSettings().catch(() => null);

  return (
    <div className="px-4 py-5 md:px-10 md:py-8">
      {/* datum address */}
      <div className="mb-6">
        <p className="address">
          <span className="address-active">{t.settings.heading}</span>
        </p>
      </div>

      {/* horizontal datum */}
      <div className="edge-h mb-8" aria-hidden />

      <SettingsFormClient initialSettings={settings} />
    </div>
  );
}
