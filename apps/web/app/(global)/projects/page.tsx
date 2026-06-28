import { NewProjectModal } from "@/components/features/projects/new-project-modal";
import { ProjectsClient } from "@/components/features/projects/projects-client";
import { listProjects } from "@/lib/api/endpoints";
import { getDictionary } from "@/lib/i18n/dictionary";
import { getLocale } from "@/lib/i18n/locale";

export default async function ProjectsPage() {
  const [projects, locale] = await Promise.all([listProjects().catch(() => []), getLocale()]);
  const dict = getDictionary(locale);

  return (
    <div className="px-4 py-5 md:px-10 md:py-8" style={{ maxWidth: "var(--content-max, 1280px)" }}>
      {/* Page header — left-registered, PC width */}
      <div className="mb-6 flex items-start justify-between gap-4 md:mb-8">
        <div>
          <h1 className="text-[18px] font-semibold leading-tight tracking-[-0.02em] text-on-surface">
            {dict.projects.heading}
          </h1>
          <p className="data mt-1 text-on-surface-variant">{projects.length}</p>
        </div>
        {/* Primary CTA — one per page. Render the modal's own client-side trigger
            (do NOT pass a server-built element into Radix's DialogTrigger; the
            injected aria-controls useId would mismatch on hydration). */}
        <NewProjectModal />
      </div>

      <ProjectsClient initialProjects={projects} />
    </div>
  );
}
