import { describe, expect, it } from "vitest";
import type { DevServerConfig } from "@/lib/api/endpoints";
import { configFromDraft, type DevServerDraft, draftFromConfig, emptyServiceDraft } from "./draft";

const fullConfig: DevServerConfig = {
  services: [
    {
      name: "web",
      command: ["pnpm", "dev", "--port", "{port}"],
      cwd: "apps/web",
      base_port: 3000,
      readiness: { path: "/health", timeout_seconds: 90 },
      env: { NODE_ENV: "development", API_URL: "http://127.0.0.1:{port:api}" },
      env_file: ["~/secrets/dev.env", ".env.development"],
      env_passthrough: ["DATABASE_URL"],
    },
    {
      name: "api",
      command: ["uvicorn", "main:app", "--port", "{port}"],
      cwd: ".",
      base_port: 8000,
      readiness: { path: null, timeout_seconds: 60 },
      env: {},
      env_file: [],
      env_passthrough: [],
    },
  ],
  browser: {
    allowed_origins: ["https://fonts.googleapis.com"],
    allow_common_cdns: false,
  },
};

function validDraft(overrides: Partial<DevServerDraft> = {}): DevServerDraft {
  return {
    enabled: true,
    services: [
      {
        name: "web",
        commandLine: "pnpm dev --port {port}",
        cwd: "apps/web",
        basePort: "3000",
        readinessPath: "/health",
        readinessTimeout: "90",
        envText: "NODE_ENV=development",
        envFileText: "",
        envPassthroughText: "",
      },
    ],
    allowedOriginsText: "https://fonts.googleapis.com",
    allowCommonCdns: false,
    ...overrides,
  };
}

function patchService(
  draft: DevServerDraft,
  patch: Partial<(typeof draft.services)[number]>,
): DevServerDraft {
  return { ...draft, services: [{ ...draft.services[0], ...patch }] };
}

describe("draftFromConfig", () => {
  it("returns a disabled empty draft for null/undefined", () => {
    for (const config of [null, undefined]) {
      const draft = draftFromConfig(config);
      expect(draft.enabled).toBe(false);
      expect(draft.services).toEqual([]);
      expect(draft.allowedOriginsText).toBe("");
      expect(draft.allowCommonCdns).toBe(true);
    }
  });

  it("maps a full config into editable text fields", () => {
    const draft = draftFromConfig(fullConfig);
    expect(draft.enabled).toBe(true);
    expect(draft.services).toEqual([
      {
        name: "web",
        commandLine: "pnpm dev --port {port}",
        cwd: "apps/web",
        basePort: "3000",
        readinessPath: "/health",
        readinessTimeout: "90",
        envText: "NODE_ENV=development\nAPI_URL=http://127.0.0.1:{port:api}",
        envFileText: "~/secrets/dev.env\n.env.development",
        envPassthroughText: "DATABASE_URL",
      },
      {
        name: "api",
        commandLine: "uvicorn main:app --port {port}",
        cwd: ".",
        basePort: "8000",
        readinessPath: "",
        readinessTimeout: "60",
        envText: "",
        envFileText: "",
        envPassthroughText: "",
      },
    ]);
    expect(draft.allowedOriginsText).toBe("https://fonts.googleapis.com");
    expect(draft.allowCommonCdns).toBe(false);
  });

  it("defaults missing readiness to null path / 60s and missing browser to CDN on", () => {
    const draft = draftFromConfig({
      services: [{ name: "api", command: ["uvicorn"], cwd: ".", base_port: 8000 }],
    });
    expect(draft.services[0].readinessPath).toBe("");
    expect(draft.services[0].readinessTimeout).toBe("60");
    expect(draft.allowCommonCdns).toBe(true);
  });
});

describe("configFromDraft", () => {
  it("roundtrips draftFromConfig(config) back to the same config", () => {
    const result = configFromDraft(draftFromConfig(fullConfig));
    expect(result).toEqual({ ok: true, config: fullConfig });
  });

  it("converts a valid draft", () => {
    const result = configFromDraft(validDraft());
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.services[0]).toEqual({
      name: "web",
      command: ["pnpm", "dev", "--port", "{port}"],
      cwd: "apps/web",
      base_port: 3000,
      readiness: { path: "/health", timeout_seconds: 90 },
      env: { NODE_ENV: "development" },
      env_file: [],
      env_passthrough: [],
    });
    expect(result.config.browser).toEqual({
      allowed_origins: ["https://fonts.googleapis.com"],
      allow_common_cdns: false,
    });
  });

  it("maps empty readiness path to null and empty cwd to '.'", () => {
    const result = configFromDraft(patchService(validDraft(), { readinessPath: "  ", cwd: "  " }));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.services[0].readiness?.path).toBeNull();
    expect(result.config.services[0].cwd).toBe(".");
  });

  it("defaults a blank timeout to 60", () => {
    const result = configFromDraft(patchService(validDraft(), { readinessTimeout: "" }));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.services[0].readiness?.timeout_seconds).toBe(60);
  });

  it("ignores blank env lines and keeps '=' inside values", () => {
    const result = configFromDraft(patchService(validDraft(), { envText: "\nA=1\n\nB=x=y\n  \n" }));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.services[0].env).toEqual({ A: "1", B: "x=y" });
  });

  it("splits allowed origins one per line, dropping blanks", () => {
    const result = configFromDraft(
      validDraft({ allowedOriginsText: "https://a.example\n\n https://b.example \n" }),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.browser?.allowed_origins).toEqual([
      "https://a.example",
      "https://b.example",
    ]);
  });

  it("rejects an empty services list", () => {
    const result = configFromDraft(validDraft({ services: [] }));
    expect(result).toEqual({ ok: false, error: { code: "noServices" } });
  });

  it("rejects a service without a name", () => {
    const result = configFromDraft(patchService(validDraft(), { name: "  " }));
    expect(result).toEqual({
      ok: false,
      error: { code: "serviceNameRequired", serviceIndex: 0 },
    });
  });

  it.each([
    "web app",
    "-web",
    "_web",
    "web.app",
    "web/app",
    "wéb",
  ])("rejects an invalid service name %j", (name) => {
    const result = configFromDraft(patchService(validDraft(), { name }));
    expect(result).toEqual({
      ok: false,
      error: { code: "invalidServiceName", serviceIndex: 0 },
    });
  });

  it.each([
    "web",
    "web-2",
    "web_2",
    "Web",
    "0",
    "a1-B_2",
  ])("accepts a valid service name %j", (name) => {
    const result = configFromDraft(patchService(validDraft(), { name }));
    expect(result.ok).toBe(true);
  });

  it("rejects duplicate service names, reporting the second occurrence", () => {
    const draft = validDraft();
    draft.services = [
      draft.services[0],
      { ...emptyServiceDraft(), name: "web", commandLine: "pnpm start", basePort: "4000" },
    ];
    const result = configFromDraft(draft);
    expect(result).toEqual({ ok: false, error: { code: "duplicateServiceName", serviceIndex: 1 } });
  });

  it("rejects a service without a command", () => {
    const result = configFromDraft(patchService(validDraft(), { commandLine: "   " }));
    expect(result).toEqual({ ok: false, error: { code: "commandRequired", serviceIndex: 0 } });
  });

  it("accepts a {port:name} reference to a sibling service", () => {
    const draft = validDraft();
    draft.services = [
      { ...draft.services[0], envText: "API_URL=http://127.0.0.1:{port:api}" },
      { ...emptyServiceDraft(), name: "api", commandLine: "uvicorn app", basePort: "8000" },
    ];
    expect(configFromDraft(draft).ok).toBe(true);
  });

  it("rejects a {port:name} reference to a service not in this config (env value)", () => {
    const result = configFromDraft(
      patchService(validDraft(), { envText: "API_URL=http://127.0.0.1:{port:api}" }),
    );
    expect(result).toEqual({
      ok: false,
      error: { code: "unknownPortReference", serviceIndex: 0, line: "api" },
    });
  });

  it("rejects a {port:name} reference to a service not in this config (command)", () => {
    const result = configFromDraft(
      patchService(validDraft(), { commandLine: "serve --upstream {port:backend}" }),
    );
    expect(result).toEqual({
      ok: false,
      error: { code: "unknownPortReference", serviceIndex: 0, line: "backend" },
    });
  });

  it("leaves a bare {port} placeholder untouched by the reference check", () => {
    const result = configFromDraft(
      patchService(validDraft(), { envText: "SELF=http://127.0.0.1:{port}" }),
    );
    expect(result.ok).toBe(true);
  });

  it("accepts a {port:repo/service} reference matching another repo's saved config", () => {
    const result = configFromDraft(
      patchService(validDraft(), { envText: "API_URL=http://127.0.0.1:{port:backend/api}" }),
      { selfRepoName: "frontend", repoServices: { backend: ["api"] } },
    );
    expect(result.ok).toBe(true);
  });

  it("rejects a {port:repo/…} reference to an unknown repo", () => {
    const result = configFromDraft(
      patchService(validDraft(), { envText: "API_URL=http://127.0.0.1:{port:ghost/api}" }),
      { selfRepoName: "frontend", repoServices: { backend: ["api"] } },
    );
    expect(result).toEqual({
      ok: false,
      error: { code: "unknownRepoReference", serviceIndex: 0, line: "ghost" },
    });
  });

  it("rejects a {port:repo/service} whose service the repo does not declare", () => {
    const result = configFromDraft(
      patchService(validDraft(), { envText: "API_URL=http://127.0.0.1:{port:backend/ghost}" }),
      { selfRepoName: "frontend", repoServices: { backend: ["api"] } },
    );
    expect(result).toEqual({
      ok: false,
      error: { code: "unknownRemoteService", serviceIndex: 0, line: "backend/ghost" },
    });
  });

  it("validates a self-qualified {port:self/…} reference against the draft", () => {
    const ok = configFromDraft(
      patchService(validDraft(), { envText: "SELF=http://127.0.0.1:{port:frontend/web}" }),
      { selfRepoName: "frontend", repoServices: {} },
    );
    expect(ok.ok).toBe(true);
    const bad = configFromDraft(
      patchService(validDraft(), { envText: "SELF=http://127.0.0.1:{port:frontend/ghost}" }),
      { selfRepoName: "frontend", repoServices: {} },
    );
    expect(bad).toEqual({
      ok: false,
      error: { code: "unknownPortReference", serviceIndex: 0, line: "ghost" },
    });
  });

  it("skips qualified references when no cross-repo context is given", () => {
    const result = configFromDraft(
      patchService(validDraft(), { envText: "API_URL=http://127.0.0.1:{port:backend/api}" }),
    );
    expect(result.ok).toBe(true);
  });

  it("validates references to dotted repo names ({port:next.js/api})", () => {
    const ok = configFromDraft(
      patchService(validDraft(), { envText: "API_URL=http://127.0.0.1:{port:next.js/api}" }),
      { selfRepoName: "frontend", repoServices: { "next.js": ["api"] } },
    );
    expect(ok.ok).toBe(true);
    const bad = configFromDraft(
      patchService(validDraft(), { envText: "API_URL=http://127.0.0.1:{port:ghost.io/api}" }),
      { selfRepoName: "frontend", repoServices: { "next.js": ["api"] } },
    );
    expect(bad).toEqual({
      ok: false,
      error: { code: "unknownRepoReference", serviceIndex: 0, line: "ghost.io" },
    });
  });

  it("reports a repo named after an Object.prototype member instead of crashing", () => {
    const result = configFromDraft(
      patchService(validDraft(), { envText: "X=http://127.0.0.1:{port:toString/api}" }),
      { selfRepoName: "frontend", repoServices: { backend: ["api"] } },
    );
    expect(result).toEqual({
      ok: false,
      error: { code: "unknownRepoReference", serviceIndex: 0, line: "toString" },
    });
  });

  it("reports a loose unqualified reference ({port: api} with a space)", () => {
    const result = configFromDraft(
      patchService(validDraft(), { envText: "X=http://127.0.0.1:{port: api}" }),
    );
    expect(result).toEqual({
      ok: false,
      error: { code: "unknownPortReference", serviceIndex: 0, line: " api" },
    });
  });

  it.each([
    "0",
    "65536",
    "-1",
    "3000.5",
    "abc",
    "",
    "0x50",
    "3e4",
    "1_000",
  ])("rejects invalid port %j", (basePort) => {
    const result = configFromDraft(patchService(validDraft(), { basePort }));
    expect(result).toEqual({ ok: false, error: { code: "invalidPort", serviceIndex: 0 } });
  });

  it("trims surrounding whitespace on a port", () => {
    const result = configFromDraft(patchService(validDraft(), { basePort: " 12 " }));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.services[0].base_port).toBe(12);
  });

  it.each([
    "0",
    "-5",
    "abc",
    "601",
    "0x50",
    "3e4",
    "1_000",
  ])("rejects invalid timeout %j", (readinessTimeout) => {
    const result = configFromDraft(patchService(validDraft(), { readinessTimeout }));
    expect(result).toEqual({ ok: false, error: { code: "invalidTimeout", serviceIndex: 0 } });
  });

  it("accepts the timeout upper boundary of 600", () => {
    const result = configFromDraft(patchService(validDraft(), { readinessTimeout: "600" }));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.services[0].readiness?.timeout_seconds).toBe(600);
  });

  it("rejects env lines without '=' or with an empty key", () => {
    for (const line of ["JUST_A_KEY", "=value"]) {
      const result = configFromDraft(patchService(validDraft(), { envText: `A=1\n${line}` }));
      expect(result).toEqual({
        ok: false,
        error: { code: "invalidEnvLine", serviceIndex: 0, line },
      });
    }
  });

  it("reports the failing service's index", () => {
    const draft = validDraft();
    draft.services = [draft.services[0], { ...emptyServiceDraft(), name: "api", basePort: "no" }];
    const result = configFromDraft(draft);
    expect(result).toEqual({ ok: false, error: { code: "commandRequired", serviceIndex: 1 } });
  });
});

describe("env sources (env_file / env_passthrough)", () => {
  it("splits env files and pass-through names one per line, dropping blanks", () => {
    const result = configFromDraft(
      patchService(validDraft(), {
        envFileText: "~/secrets/dev.env\n\n .env.development \n",
        envPassthroughText: "DATABASE_URL\n\nSTRIPE_TEST_KEY\n",
      }),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.services[0].env_file).toEqual(["~/secrets/dev.env", ".env.development"]);
    expect(result.config.services[0].env_passthrough).toEqual(["DATABASE_URL", "STRIPE_TEST_KEY"]);
  });

  it.each(["1BAD", "A B", "DASH-ED"])("rejects invalid pass-through name %s", (name) => {
    const result = configFromDraft(patchService(validDraft(), { envPassthroughText: name }));
    expect(result).toEqual({
      ok: false,
      error: { code: "invalidEnvPassthroughName", serviceIndex: 0, line: name },
    });
  });

  it("roundtrips env sources through draftFromConfig", () => {
    const result = configFromDraft(draftFromConfig(fullConfig));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.services[0].env_file).toEqual(fullConfig.services[0].env_file);
    expect(result.config.services[0].env_passthrough).toEqual(
      fullConfig.services[0].env_passthrough,
    );
  });
});
