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
