/**
 * All endpoints from spec §8 are strictly typed with paths/components types from @yukar/api-types.
 * Manual type redefinition is prohibited. Import and use directly from components["schemas"].
 */

import type { components, operations } from "@yukar/api-types";
import { ApiError, apiFetch } from "./client";

export { ApiError } from "./client";

// ---- Type aliases (references, not redefinitions) ----

export type Project = components["schemas"]["Project"];
export type Epic = components["schemas"]["Epic"];
// Epic + run digest: the list endpoint embeds a per-epic state.yaml
// summary so boards can show "your turn" without N+1 run/state calls.
export type EpicWithRunSummary = components["schemas"]["EpicWithRunSummary"];
export type RunSummary = components["schemas"]["RunSummary"];
export type ThreadEntry = components["schemas"]["ThreadEntry"];
export type Message = components["schemas"]["Message"];
export type TasksFile = components["schemas"]["TasksFile"];
export type TasksResponse = components["schemas"]["TasksResponse"];
export type Task = components["schemas"]["Task"];
export type PlanApproval = components["schemas"]["PlanApproval"];
export type PlanApprovalRequest = components["schemas"]["PlanApprovalRequest"];
export type DocResponse = components["schemas"]["DocResponse"];
export type DiffResult = components["schemas"]["DiffResult"];
export type Settings = components["schemas"]["Settings"];
export type LLMSettings = components["schemas"]["LLMSettings"];
export type LLMRolesSettings = components["schemas"]["LLMRolesSettings"];
export type LLMRoleSettings = components["schemas"]["LLMRoleSettings"];
export type EmbeddingSettings = components["schemas"]["EmbeddingSettings"];
export type IndexerSettings = components["schemas"]["IndexerSettings"];
export type ConversationSummarySettings = components["schemas"]["ConversationSummarySettings"];
export type CreateProjectRequest = components["schemas"]["CreateProjectRequest"];
export type CreateEpicRequest = components["schemas"]["CreateEpicRequest"];
export type CreateThreadRequest = components["schemas"]["CreateThreadRequest"];
export type StartReviewRequest = components["schemas"]["StartReviewRequest"];
export type PostMessageRequest = components["schemas"]["PostMessageRequest"];
export type CommitRequest = components["schemas"]["CommitRequest"];
export type MergeRequest = components["schemas"]["MergeRequest"];
export type PutDocRequest = components["schemas"]["PutDocRequest"];
// RunEvent union type (discriminated union)
export type RunStartedEvent = components["schemas"]["RunStartedEvent"];
export type RunCompletedEvent = components["schemas"]["RunCompletedEvent"];
export type RunFailedEvent = components["schemas"]["RunFailedEvent"];
export type RunStoppedEvent = components["schemas"]["RunStoppedEvent"];
export type TaskUpdateEvent = components["schemas"]["TaskUpdateEvent"];
export type WorkerStartedEvent = components["schemas"]["WorkerStartedEvent"];
export type WorkerCompletedEvent = components["schemas"]["WorkerCompletedEvent"];
export type EvalResultEvent = components["schemas"]["EvalResultEvent"];
export type TokenEvent = components["schemas"]["TokenEvent"];
export type ToolCallEvent = components["schemas"]["ToolCallEvent"];
export type ToolResultEvent = components["schemas"]["ToolResultEvent"];
export type DiffUpdateEvent = components["schemas"]["DiffUpdateEvent"];

export type ResolveRequest = components["schemas"]["ResolveRequest"];
export type ResolveStarted = components["schemas"]["ResolveStarted"];
export type PruneRequest = components["schemas"]["PruneRequest"];
export type RepoPruneResult = components["schemas"]["RepoPruneResult"];
export type DiffSummary = components["schemas"]["DiffSummary"];
export type RunState = components["schemas"]["RunState"];
export type ActiveWorker = components["schemas"]["ActiveWorker"];

export type PatchEpicRequest = components["schemas"]["PatchEpicRequest"];
export type StartMergeRequest = components["schemas"]["StartMergeRequest"];
export type StartMergeResponse = components["schemas"]["StartMergeResponse"];
export type StopMergeResponse = components["schemas"]["StopMergeResponse"];
export type EpicMergeProgressEvent = components["schemas"]["EpicMergeProgressEvent"];
export type EpicMergeResult = components["schemas"]["EpicMergeResult"];
export type EpicStatusChangedEvent = components["schemas"]["EpicStatusChangedEvent"];
export type EpicMergedEvent = components["schemas"]["EpicMergedEvent"];

export type RunPausedEvent = components["schemas"]["RunPausedEvent"];
export type RunResumedEvent = components["schemas"]["RunResumedEvent"];
export type RunPreparingEvent = components["schemas"]["RunPreparingEvent"];

// New events added in backend A1/A2
export type ManagerTurnStartedEvent = components["schemas"]["ManagerTurnStartedEvent"];
export type ManagerMessageEvent = components["schemas"]["ManagerMessageEvent"];
export type DelegationEvent = components["schemas"]["DelegationEvent"];
export type EvaluatorStartedEvent = components["schemas"]["EvaluatorStartedEvent"];
export type PauseEffectiveEvent = components["schemas"]["PauseEffectiveEvent"];
export type YourTurnEvent = components["schemas"]["YourTurnEvent"];
export type YourTurnEndedEvent = components["schemas"]["YourTurnEndedEvent"];
export type UserMessageCommittedEvent = components["schemas"]["UserMessageCommittedEvent"];
export type WorkerFailedEvent = components["schemas"]["WorkerFailedEvent"];
export type SensitiveFileWrittenEvent = components["schemas"]["SensitiveFileWrittenEvent"];

export type RunEvent =
  | RunStartedEvent
  | RunCompletedEvent
  | RunFailedEvent
  | RunStoppedEvent
  | RunPausedEvent
  | RunResumedEvent
  | RunPreparingEvent
  | TaskUpdateEvent
  | WorkerStartedEvent
  | WorkerCompletedEvent
  | WorkerFailedEvent
  | EvalResultEvent
  | TokenEvent
  | ToolCallEvent
  | ToolResultEvent
  | DiffUpdateEvent
  | ManagerTurnStartedEvent
  | ManagerMessageEvent
  | DelegationEvent
  | EvaluatorStartedEvent
  | PauseEffectiveEvent
  | YourTurnEvent
  | YourTurnEndedEvent
  | UserMessageCommittedEvent
  | SensitiveFileWrittenEvent
  | EpicMergedEvent;

/** Lifecycle-only events emitted by the project-level SSE stream. */
export type ProjectLifecycleEvent =
  | RunStartedEvent
  | RunCompletedEvent
  | RunFailedEvent
  | RunStoppedEvent
  | RunPausedEvent
  | RunResumedEvent;

/**
 * Extract the backend's human-readable `detail` string from an ApiError, if the
 * body carries one (FastAPI HTTPException → `{ detail: "..." }`).  Returns null
 * when there is no string detail, so callers can fall back to a generic message.
 * Prefer this over a fixed message for 4xx so the real reason (e.g. "No active
 * manager trial to continue" vs "An active run is in progress") reaches the user
 * instead of a one-size-fits-all guess.
 */
export function extractDetail(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const body = err.body as { detail?: unknown } | null | undefined;
  const detail = body?.detail;
  return typeof detail === "string" && detail.trim() ? detail : null;
}

/** Extract conflicts array from a 409 ApiError body if present */
export function extractConflicts(err: ApiError): string[] {
  if (err.status !== 409) return [];
  const body = err.body as { detail?: { conflicts?: string[] } | string } | null | undefined;
  if (!body || typeof body !== "object") return [];
  const detail = body.detail;
  if (detail && typeof detail === "object" && Array.isArray(detail.conflicts)) {
    return detail.conflicts as string[];
  }
  return [];
}

// ---- Projects ----

export function listProjects(): Promise<
  operations["list_projects_api_projects_get"]["responses"][200]["content"]["application/json"]
> {
  return apiFetch("/api/projects");
}

export function createProject(body: CreateProjectRequest): Promise<Project> {
  return apiFetch("/api/projects", { method: "POST", body });
}

export function getProject(projectId: string): Promise<Project> {
  return apiFetch(`/api/projects/${projectId}`);
}

export function deleteProject(projectId: string): Promise<void> {
  return apiFetch(`/api/projects/${projectId}`, { method: "DELETE" });
}

// ---- Epics ----

export function listEpics(
  projectId: string,
  includeCompleted = false,
): Promise<EpicWithRunSummary[]> {
  const q = includeCompleted ? "?include_completed=true" : "";
  return apiFetch(`/api/projects/${projectId}/epics${q}`);
}

export function createEpic(projectId: string, body: CreateEpicRequest): Promise<Epic> {
  return apiFetch(`/api/projects/${projectId}/epics`, {
    method: "POST",
    body,
  });
}

export function getEpic(projectId: string, epicId: string): Promise<Epic> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}`);
}

export function patchEpic(
  projectId: string,
  epicId: string,
  body: PatchEpicRequest,
): Promise<Epic> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}`, {
    method: "PATCH",
    body,
  });
}

export function startMerge(projectId: string, epicIds: string[]): Promise<StartMergeResponse> {
  return apiFetch(`/api/projects/${projectId}/merge`, {
    method: "POST",
    body: { epic_ids: epicIds } satisfies StartMergeRequest,
  });
}

export function stopMerge(projectId: string): Promise<StopMergeResponse> {
  return apiFetch(`/api/projects/${projectId}/merge/stop`, { method: "POST" });
}

// ---- Run ----

export function getRunState(projectId: string, epicId: string): Promise<RunState> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/run/state`);
}

/**
 * Start a Run.
 * After calling, invalidate the following query keys:
 *   - queryKeys.runState.get(projectId, epicId)
 *   - queryKeys.epics.detail(projectId, epicId)
 *
 * The actual cache patch is performed by applyRunCachePatch on the SSE run_started event.
 *
 * @see applyRunCachePatch (lib/sse/run-activity/cache-patch.ts)
 */
export function startRun(projectId: string, epicId: string): Promise<Record<string, string>> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/run`, {
    method: "POST",
  });
}

/**
 * Send an action (pause / resume / stop) to a Run.
 * After calling, invalidate the following query keys:
 *   - queryKeys.runState.get(projectId, epicId)
 *   - queryKeys.epics.detail(projectId, epicId)
 *
 * The actual cache patch is applied via SSE run_paused / run_resumed / run_stopped events.
 *
 * @see applyRunCachePatch (lib/sse/run-activity/cache-patch.ts)
 */
export function runAction(
  projectId: string,
  epicId: string,
  action: "pause" | "resume" | "stop",
): Promise<Record<string, string>> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/run/${action}`, { method: "POST" });
}

// ---- Threads ----

export function listThreads(projectId: string, epicId: string): Promise<ThreadEntry[]> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/threads`);
}

export function createThread(
  projectId: string,
  epicId: string,
  body: CreateThreadRequest,
): Promise<ThreadEntry> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/threads`, {
    method: "POST",
    body,
  });
}

export function archiveThread(
  projectId: string,
  epicId: string,
  threadId: string,
): Promise<Record<string, string>> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/threads/${threadId}/archive`, {
    method: "POST",
  });
}

/**
 * Start a read-only Reviewer run: creates a fresh reviewer conversation, seeds it
 * from the active Manager↔user conversation, and starts a reviewer run bound to it.
 * Returns the new reviewer ThreadEntry. Navigate to it and invalidate:
 *   - queryKeys.threads.list(projectId, epicId)
 */
export function startReview(
  projectId: string,
  epicId: string,
  body: StartReviewRequest = { title: "" },
): Promise<ThreadEntry> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/review`, {
    method: "POST",
    body,
  });
}

export function getThreadMessages(
  projectId: string,
  epicId: string,
  threadId: string,
): Promise<Message[]> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/threads/${threadId}`);
}

export function postMessage(
  projectId: string,
  epicId: string,
  threadId: string,
  body: PostMessageRequest,
): Promise<Message> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/threads/${threadId}/messages`, {
    method: "POST",
    body,
  });
}

// ---- Tasks ----

export function getTasks(projectId: string, epicId: string): Promise<TasksResponse> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/tasks`);
}

// ---- Plan approval ----

/**
 * Approve the current task-plan snapshot.  `tasksHash` must be the `plan_hash`
 * the backend returned from GET /tasks — the client never computes hashes.
 * The backend answers 409 when the plan changed after it was displayed
 * (TOCTOU guard); on 409 refetch tasks and let the user re-review.
 */
export function approvePlan(
  projectId: string,
  epicId: string,
  tasksHash: string,
): Promise<PlanApproval> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/plan/approval`, {
    method: "POST",
    body: { tasks_hash: tasksHash } satisfies PlanApprovalRequest,
  });
}

/** Revoke the recorded plan approval (204, idempotent). */
export function revokePlanApproval(projectId: string, epicId: string): Promise<void> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/plan/approval`, {
    method: "DELETE",
  });
}

// ---- Docs ----

export function listProjectDocs(projectId: string): Promise<string[]> {
  return apiFetch(`/api/projects/${projectId}/docs`);
}

export function getProjectDoc(projectId: string, filename: string): Promise<DocResponse> {
  return apiFetch(`/api/projects/${projectId}/docs/${filename}`);
}

export function putProjectDoc(
  projectId: string,
  filename: string,
  body: PutDocRequest,
): Promise<DocResponse> {
  return apiFetch(`/api/projects/${projectId}/docs/${filename}`, {
    method: "PUT",
    body,
  });
}

export function listEpicDocs(projectId: string, epicId: string): Promise<string[]> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/docs`);
}

export function getEpicDoc(
  projectId: string,
  epicId: string,
  filename: string,
): Promise<DocResponse> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/docs/${filename}`);
}

export function putEpicDoc(
  projectId: string,
  epicId: string,
  filename: string,
  body: PutDocRequest,
): Promise<DocResponse> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/docs/${filename}`, {
    method: "PUT",
    body,
  });
}

// ---- Git ----

export function getGitDiff(
  projectId: string,
  epicId: string,
  repo: string,
  mode: "working" | "epic" = "working",
): Promise<DiffResult> {
  const q = new URLSearchParams({ repo, mode });
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/git/diff?${q}`);
}

export function gitCommit(
  projectId: string,
  epicId: string,
  body: CommitRequest,
): Promise<Record<string, string>> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/git/commit`, {
    method: "POST",
    body,
  });
}

export function gitMerge(
  projectId: string,
  epicId: string,
  body: MergeRequest,
): Promise<Record<string, string>> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/git/merge`, {
    method: "POST",
    body,
  });
}

export function gitResolve(
  projectId: string,
  epicId: string,
  body: ResolveRequest,
): Promise<ResolveStarted> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/git/resolve`, {
    method: "POST",
    body,
  });
}

export function gitPrune(
  projectId: string,
  epicId: string,
  body: PruneRequest,
): Promise<RepoPruneResult[]> {
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/git/prune`, {
    method: "POST",
    body,
  });
}

export function getGitDiffSummary(
  projectId: string,
  epicId: string,
  mode: "working" | "epic" = "working",
): Promise<DiffSummary> {
  const q = new URLSearchParams({ mode });
  return apiFetch(`/api/projects/${projectId}/epics/${epicId}/git/diff/summary?${q}`);
}

// ---- Search ----

export type SearchRequest = components["schemas"]["SearchRequest"];
export type SearchResponse = components["schemas"]["SearchResponse"];
export type SearchResultItem = components["schemas"]["SearchResultItem"];
export type IndexTriggerResponse = components["schemas"]["IndexTriggerResponse"];
export type IndexStatusResponse = components["schemas"]["IndexStatusResponse"];
export type RepoIndexStatus = components["schemas"]["RepoIndexStatus"];

export function searchCodebase(projectId: string, body: SearchRequest): Promise<SearchResponse> {
  return apiFetch(`/api/projects/${projectId}/search`, { method: "POST", body });
}

export function triggerIndex(projectId: string, repo?: string): Promise<IndexTriggerResponse> {
  const q = repo ? `?repo=${encodeURIComponent(repo)}` : "";
  return apiFetch(`/api/projects/${projectId}/index${q}`, { method: "POST" });
}

export function getIndexStatus(projectId: string): Promise<IndexStatusResponse> {
  return apiFetch(`/api/projects/${projectId}/index/status`);
}

// ---- Agent Configs (L1) ----

export type AgentConfig = components["schemas"]["AgentConfig"];
export type AgentConfigUpdateRequest = components["schemas"]["AgentConfigUpdateRequest"];

export function listAgentConfigs(projectId: string): Promise<Record<string, string>> {
  return apiFetch(`/api/projects/${projectId}/agent-configs`);
}

export function getAgentConfig(
  projectId: string,
  role: "manager" | "worker" | "evaluator" | "reviewer",
): Promise<AgentConfig> {
  return apiFetch(`/api/projects/${projectId}/agent-configs/${role}`);
}

export function putAgentConfig(
  projectId: string,
  role: "manager" | "worker" | "evaluator" | "reviewer",
  instructions: string,
): Promise<AgentConfig> {
  return apiFetch(`/api/projects/${projectId}/agent-configs/${role}`, {
    method: "PUT",
    body: { instructions } satisfies AgentConfigUpdateRequest,
  });
}

// ---- Skills (L2) ----

export type SkillMeta = components["schemas"]["SkillMeta"];
export type Skill = components["schemas"]["Skill"];
export type SkillUpdateRequest = components["schemas"]["SkillUpdateRequest"];

export function listSkills(projectId: string): Promise<SkillMeta[]> {
  return apiFetch(`/api/projects/${projectId}/skills`);
}

export function getSkill(projectId: string, name: string): Promise<Skill> {
  return apiFetch(`/api/projects/${projectId}/skills/${encodeURIComponent(name)}`);
}

export function putSkill(projectId: string, name: string, content: string): Promise<Skill> {
  return apiFetch(`/api/projects/${projectId}/skills/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: { content } satisfies SkillUpdateRequest,
  });
}

export function deleteSkill(projectId: string, name: string): Promise<void> {
  return apiFetch(`/api/projects/${projectId}/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// ---- Agent Profiles (named custom agents) ----

export type AgentProfile = components["schemas"]["AgentProfile"];
export type RepoCommands = components["schemas"]["RepoCommands"];

export function listAgentProfiles(projectId: string): Promise<AgentProfile[]> {
  return apiFetch(`/api/projects/${projectId}/agent-profiles`);
}

export function getAgentProfile(projectId: string, name: string): Promise<AgentProfile> {
  return apiFetch(`/api/projects/${projectId}/agent-profiles/${encodeURIComponent(name)}`);
}

export function putAgentProfile(
  projectId: string,
  name: string,
  profile: AgentProfile,
): Promise<AgentProfile> {
  return apiFetch(`/api/projects/${projectId}/agent-profiles/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: profile,
  });
}

export function deleteAgentProfile(projectId: string, name: string): Promise<void> {
  return apiFetch(`/api/projects/${projectId}/agent-profiles/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// ---- Repos / run_command ----

export type Repo = components["schemas"]["Repo"];
export type AddRepoRequest = components["schemas"]["AddRepoRequest"];
export type DevServerConfig = components["schemas"]["DevServerConfig"];
export type DevService = components["schemas"]["DevService"];
export type ServiceReadiness = components["schemas"]["ServiceReadiness"];
export type DevServerBrowser = components["schemas"]["DevServerBrowser"];

export function listRepos(projectId: string): Promise<Repo[]> {
  return apiFetch(`/api/projects/${projectId}/repos`);
}

export function addRepo(projectId: string, body: AddRepoRequest): Promise<Repo> {
  return apiFetch(`/api/projects/${projectId}/repos`, { method: "POST", body });
}

export function deleteRepo(projectId: string, repoName: string): Promise<void> {
  return apiFetch(`/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}`, {
    method: "DELETE",
  });
}

export function putRepoCommands(
  projectId: string,
  repoName: string,
  commands: RepoCommands,
): Promise<Repo> {
  return apiFetch(`/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}/commands`, {
    method: "PUT",
    body: commands,
  });
}

export function putRepoDevServer(
  projectId: string,
  repoName: string,
  config: DevServerConfig,
): Promise<Repo> {
  return apiFetch(`/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}/dev-server`, {
    method: "PUT",
    body: config,
  });
}

export function deleteRepoDevServer(projectId: string, repoName: string): Promise<Repo> {
  return apiFetch(`/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}/dev-server`, {
    method: "DELETE",
  });
}

export type BlockedOriginItem = components["schemas"]["BlockedOriginItem"];

/** Origins the browser egress gate rejected for this project (in-process aggregate). */
export function listBlockedOrigins(projectId: string): Promise<BlockedOriginItem[]> {
  return apiFetch(`/api/projects/${projectId}/browser/blocked`);
}

// ---- Browser login capture (design §12) ----

export type BrowserAuthStatus = components["schemas"]["BrowserAuthStatus"];
export type LoginStartResponse = components["schemas"]["LoginStartResponse"];

export function getBrowserAuth(projectId: string, repoName: string): Promise<BrowserAuthStatus> {
  return apiFetch(`/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}/browser-auth`);
}

export function startBrowserLogin(
  projectId: string,
  repoName: string,
): Promise<LoginStartResponse> {
  return apiFetch(
    `/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}/browser-login/start`,
    { method: "POST" },
  );
}

export function finishBrowserLogin(
  projectId: string,
  repoName: string,
): Promise<BrowserAuthStatus> {
  return apiFetch(
    `/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}/browser-login/finish`,
    { method: "POST" },
  );
}

export function cancelBrowserLogin(projectId: string, repoName: string): Promise<void> {
  return apiFetch(
    `/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}/browser-login/cancel`,
    { method: "POST" },
  );
}

export function deleteBrowserAuth(projectId: string, repoName: string): Promise<void> {
  return apiFetch(`/api/projects/${projectId}/repos/${encodeURIComponent(repoName)}/browser-auth`, {
    method: "DELETE",
  });
}

// ---- MCP (L3) ----

export type McpConfig = components["schemas"]["McpConfig"];
export type McpServerConfig = components["schemas"]["McpServerConfig"];

export function getMcpConfig(projectId: string): Promise<McpConfig> {
  return apiFetch(`/api/projects/${projectId}/mcp`);
}

export function putMcpConfig(projectId: string, config: McpConfig): Promise<McpConfig> {
  return apiFetch(`/api/projects/${projectId}/mcp`, {
    method: "PUT",
    body: config,
  });
}

// ---- Settings ----

export function getSettings(): Promise<Settings> {
  return apiFetch("/api/settings");
}

export function putSettings(body: Settings): Promise<Settings> {
  return apiFetch("/api/settings", { method: "PUT", body });
}

// ---- Usage ----

export type UsageSummaryResponse = components["schemas"]["UsageSummaryResponse"];
export type ExchangeRateInfo = components["schemas"]["ExchangeRateInfo"];
export type BudgetState = components["schemas"]["BudgetState"];
export type BudgetSetRequest = components["schemas"]["BudgetSetRequest"];
export type BudgetSetResponse = components["schemas"]["BudgetSetResponse"];
export type ProjectUsageBreakdown = components["schemas"]["ProjectUsageBreakdown"];
export type EpicUsageBreakdown = components["schemas"]["EpicUsageBreakdown"];
export type RunUsageBreakdown = components["schemas"]["RunUsageBreakdown"];
export type ModelUsageBreakdown = components["schemas"]["ModelUsageBreakdown"];
export type UsagePeriodTotals = components["schemas"]["UsagePeriodTotals"];
export type UsageDailyPoint = components["schemas"]["UsageDailyPoint"];

export function getUsageSummary(): Promise<UsageSummaryResponse> {
  return apiFetch("/api/usage");
}

export function setBudget(body: BudgetSetRequest): Promise<BudgetSetResponse> {
  return apiFetch("/api/usage/budget", { method: "PUT", body });
}

// ---- System ----

export type SystemStatusResponse = components["schemas"]["SystemStatusResponse"];
export type IndexerWatcherHealth = components["schemas"]["IndexerWatcherHealth"];

export function getSystemStatus(): Promise<SystemStatusResponse> {
  return apiFetch("/api/system/status");
}
