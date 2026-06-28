/**
 * TanStack Query key constants. Hierarchical tuples control the invalidation scope of the cache.
 *
 * #18: Added search / git umbrella keys. Replaced direct arrays in command-palette / diff pages.
 */

export const queryKeys = {
  projects: {
    all: () => ["projects"] as const,
    list: () => ["projects", "list"] as const,
    detail: (projectId: string) => ["projects", projectId] as const,
  },
  epics: {
    list: (projectId: string) => ["epics", projectId] as const,
    detail: (projectId: string, epicId: string) => ["epics", projectId, epicId] as const,
  },
  threads: {
    list: (projectId: string, epicId: string) => ["threads", projectId, epicId] as const,
    messages: (projectId: string, epicId: string, threadId: string) =>
      ["threads", projectId, epicId, threadId, "messages"] as const,
  },
  tasks: {
    get: (projectId: string, epicId: string) => ["tasks", projectId, epicId] as const,
  },
  docs: {
    projectList: (projectId: string) => ["docs", "project", projectId] as const,
    projectDoc: (projectId: string, filename: string) =>
      ["docs", "project", projectId, filename] as const,
    epicList: (projectId: string, epicId: string) => ["docs", "epic", projectId, epicId] as const,
    epicDoc: (projectId: string, epicId: string, filename: string) =>
      ["docs", "epic", projectId, epicId, filename] as const,
  },
  git: {
    /** Umbrella key: invalidateQueries({ queryKey: queryKeys.git.all() }) invalidates all git-related queries */
    all: () => ["git"] as const,
    diff: (projectId: string, epicId: string, repo: string, mode: "working" | "epic") =>
      ["git", "diff", projectId, epicId, repo, mode] as const,
    diffSummary: (projectId: string, epicId: string, mode: "working" | "epic") =>
      ["git", "diff-summary", projectId, epicId, mode] as const,
  },
  settings: {
    get: () => ["settings"] as const,
  },
  index: {
    status: (projectId: string) => ["index", "status", projectId] as const,
  },
  runState: {
    get: (projectId: string, epicId: string) => ["runState", projectId, epicId] as const,
  },
  usage: {
    summary: () => ["usage", "summary"] as const,
  },
  /** Full-text codebase search. Cached by projectId / query combination. */
  search: {
    /** Umbrella key: invalidateQueries({ queryKey: queryKeys.search.all() }) invalidates all search caches */
    all: () => ["search"] as const,
    results: (projectId: string, query: string) => ["search", projectId, query] as const,
  },
  agentConfigs: {
    list: (projectId: string) => ["agentConfigs", projectId] as const,
    detail: (projectId: string, role: string) => ["agentConfigs", projectId, role] as const,
  },
  skills: {
    list: (projectId: string) => ["skills", projectId] as const,
    detail: (projectId: string, name: string) => ["skills", projectId, name] as const,
  },
  mcp: {
    get: (projectId: string) => ["mcp", projectId] as const,
  },
  agentProfiles: {
    list: (projectId: string) => ["agentProfiles", projectId] as const,
    detail: (projectId: string, name: string) => ["agentProfiles", projectId, name] as const,
  },
  repos: {
    list: (projectId: string) => ["repos", projectId] as const,
  },
} as const;
