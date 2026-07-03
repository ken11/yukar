import Link from "next/link";
import { AgentConfigsSection } from "@/components/features/settings/agent-configs-section";
import { AgentProfilesSection } from "@/components/features/settings/agent-profiles-section";
import { McpSection } from "@/components/features/settings/mcp-section";
import { SkillsSection } from "@/components/features/settings/skills-section";
import {
  getAgentConfig,
  getMcpConfig,
  listAgentProfiles,
  listRepos,
  listSkills,
} from "@/lib/api/endpoints";
import { getDictionary } from "@/lib/i18n/dictionary";
import { getLocale } from "@/lib/i18n/locale";

export default async function ProjectSettingsPage({ params }: { params: Promise<{ p: string }> }) {
  const { p } = await params;
  const locale = await getLocale();
  const t = getDictionary(locale);
  const ps = t.projectSettings;

  // Fetch all sections in parallel; fall back gracefully on error
  const [agentConfigs, skills, mcpConfig, agentProfiles, repos] = await Promise.all([
    Promise.all([
      getAgentConfig(p, "manager").catch(() => ({ role: "manager" as const, instructions: "" })),
      getAgentConfig(p, "worker").catch(() => ({ role: "worker" as const, instructions: "" })),
      getAgentConfig(p, "evaluator").catch(() => ({
        role: "evaluator" as const,
        instructions: "",
      })),
    ]),
    listSkills(p).catch(() => []),
    getMcpConfig(p).catch(() => ({ servers: [] })),
    listAgentProfiles(p).catch(() => []),
    listRepos(p).catch(() => []),
  ]);

  return (
    <div className="px-10 py-8">
      {/* datum address */}
      <div className="mb-6">
        <p className="address">
          <span className="address-active">{ps.heading}</span>
        </p>
      </div>

      {/* horizontal datum */}
      <div className="edge-h mb-8" aria-hidden />

      {/* model cross-links to global settings */}
      <p className="mb-8 text-[12px] text-outline">{ps.modelCrosslink}</p>

      <div className="w-full max-w-[960px] space-y-0">
        {/* L1 — agent instructions (per role) */}
        <AgentConfigsSection projectId={p} initialConfigs={agentConfigs} />

        <div className="edge-h my-8" aria-hidden />

        {/* L2 — skills */}
        <SkillsSection projectId={p} initialSkills={skills} />

        <div className="edge-h my-8" aria-hidden />

        {/* L3 — MCP */}
        <McpSection projectId={p} initialConfig={mcpConfig} />

        <div className="edge-h my-8" aria-hidden />

        {/* L4 — agent profiles */}
        <AgentProfilesSection
          projectId={p}
          initialProfiles={agentProfiles}
          initialSkills={skills}
          initialMcpConfig={mcpConfig}
          initialRepos={repos}
        />

        <div className="edge-h my-8" aria-hidden />

        {/* repositories link to their dedicated tab */}
        <p className="text-[12px] text-outline">
          {ps.reposCrosslink}{" "}
          <Link
            href={`/projects/${p}/repos`}
            className="underline underline-offset-2 hover:text-on-surface"
          >
            →
          </Link>
        </p>

        <div className="pb-16" />
      </div>
    </div>
  );
}
