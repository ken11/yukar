/**
 * Draft ↔ DevServerConfig conversions for the per-repo dev-server editor.
 *
 * Pure functions so they can be unit-tested without rendering. Validation
 * returns error *codes* (plus context); the hook maps codes to i18n messages.
 */

import type { DevServerConfig, DevService } from "@/lib/api/endpoints";
import { arrayToLines, linesToArray } from "@/lib/text";
import { joinCommandLine, splitCommandLine } from "./command-line";

export interface ServiceDraft {
  name: string;
  commandLine: string;
  cwd: string;
  basePort: string;
  readinessPath: string;
  readinessTimeout: string;
  envText: string;
  envFileText: string;
  envPassthroughText: string;
}

export interface DevServerDraft {
  enabled: boolean;
  services: ServiceDraft[];
  allowedOriginsText: string;
  allowCommonCdns: boolean;
}

/** Validation failure: a code the hook translates, plus the offending service. */
export type DraftValidationError =
  | { code: "noServices" }
  | { code: "serviceNameRequired"; serviceIndex: number }
  | { code: "invalidServiceName"; serviceIndex: number }
  | { code: "duplicateServiceName"; serviceIndex: number }
  | { code: "commandRequired"; serviceIndex: number }
  | { code: "invalidPort"; serviceIndex: number }
  | { code: "invalidTimeout"; serviceIndex: number }
  | { code: "invalidEnvLine"; serviceIndex: number; line: string }
  | { code: "invalidEnvPassthroughName"; serviceIndex: number; line: string }
  | { code: "unknownPortReference"; serviceIndex: number; line: string }
  | { code: "unknownRepoReference"; serviceIndex: number; line: string }
  | { code: "unknownRemoteService"; serviceIndex: number; line: string };

/** Mirrors the backend DevService.name regex (models/project.py). */
export const SERVICE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

/** Mirrors the backend env-var name regex (models/project.py `_ENV_NAME_RE`). */
const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Named {port:service} / {port:repo/service} references (mirrors
 * preview/manager.py `_PORT_PLACEHOLDER_RE`). Matched LOOSELY on purpose:
 * repo names have no charset constraint ("next.js", "example.com"), so a
 * strict class would make such references silently unvalidatable. Anything
 * inside {port:...} must either validate or be reported.
 */
const PORT_REF_RE = /\{port:([^{}]+)\}/g;

/**
 * Cross-repo validation context for {port:repo/service} references: saved
 * service names per repo. The edited repo itself is validated against the
 * DRAFT being saved, not this map.
 */
export interface CrossRepoContext {
  selfRepoName: string;
  repoServices: Record<string, string[]>;
}

export type DraftConversionResult =
  | { ok: true; config: DevServerConfig }
  | { ok: false; error: DraftValidationError };

export function emptyServiceDraft(): ServiceDraft {
  return {
    name: "",
    commandLine: "",
    cwd: "",
    basePort: "3000",
    readinessPath: "",
    readinessTimeout: "60",
    envText: "",
    envFileText: "",
    envPassthroughText: "",
  };
}

export function emptyDevServerDraft(): DevServerDraft {
  return {
    enabled: false,
    services: [],
    allowedOriginsText: "",
    allowCommonCdns: true,
  };
}

function envToText(env: Record<string, string> | undefined): string {
  return Object.entries(env ?? {})
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function serviceToDraft(service: DevService): ServiceDraft {
  return {
    name: service.name,
    commandLine: joinCommandLine(service.command),
    cwd: service.cwd ?? ".",
    basePort: String(service.base_port),
    readinessPath: service.readiness?.path ?? "",
    readinessTimeout: String(service.readiness?.timeout_seconds ?? 60),
    envText: envToText(service.env),
    envFileText: arrayToLines(service.env_file),
    envPassthroughText: arrayToLines(service.env_passthrough),
  };
}

/** Build the editable draft from a saved config (null/undefined → disabled draft). */
export function draftFromConfig(config: DevServerConfig | null | undefined): DevServerDraft {
  if (!config) return emptyDevServerDraft();
  return {
    enabled: true,
    services: config.services.map(serviceToDraft),
    allowedOriginsText: arrayToLines(config.browser?.allowed_origins),
    allowCommonCdns: config.browser?.allow_common_cdns ?? true,
  };
}

/** Parse "KEY=VALUE" lines. Blank lines are ignored; a line without "=" (or with an empty key) is invalid. */
function parseEnvText(
  text: string,
  serviceIndex: number,
): { ok: true; env: Record<string, string> } | { ok: false; error: DraftValidationError } {
  const env: Record<string, string> = {};
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (line === "") continue;
    const idx = line.indexOf("=");
    if (idx < 1) {
      return { ok: false, error: { code: "invalidEnvLine", serviceIndex, line } };
    }
    env[line.slice(0, idx).trim()] = line.slice(idx + 1);
  }
  return { ok: true, env };
}

/**
 * Validate a draft and convert it to a DevServerConfig. Mirrors the backend
 * pydantic constraints (models/project.py) so the client rejects the same
 * inputs the server would 422 on.
 *
 * Rules: at least one service; each service name is required, matches
 * `^[A-Za-z0-9][A-Za-z0-9_-]*$`, and is unique; command is required; base port
 * is a plain decimal integer in 1–65535; readiness timeout is a plain decimal
 * number in (0, 600] (blank → default 60); empty readiness path → null (wait
 * for the port only); empty cwd → ".".
 */
export function configFromDraft(
  draft: DevServerDraft,
  crossRepo?: CrossRepoContext,
): DraftConversionResult {
  if (draft.services.length === 0) {
    return { ok: false, error: { code: "noServices" } };
  }

  const services: DevService[] = [];

  for (const [serviceIndex, svc] of draft.services.entries()) {
    const name = svc.name.trim();
    if (name === "") {
      return { ok: false, error: { code: "serviceNameRequired", serviceIndex } };
    }
    if (!SERVICE_NAME_RE.test(name)) {
      return { ok: false, error: { code: "invalidServiceName", serviceIndex } };
    }
    const command = splitCommandLine(svc.commandLine);
    if (command.length === 0) {
      return { ok: false, error: { code: "commandRequired", serviceIndex } };
    }
    // Number() alone accepts hex ("0x50") and exponent ("3e4") forms the
    // backend rejects, so gate on a plain-decimal shape first.
    const portText = svc.basePort.trim();
    const port = Number(portText);
    if (!/^\d+$/.test(portText) || port < 1 || port > 65535) {
      return { ok: false, error: { code: "invalidPort", serviceIndex } };
    }
    const timeoutText = svc.readinessTimeout.trim();
    let timeout: number;
    if (timeoutText === "") {
      timeout = 60;
    } else if (/^\d+(\.\d+)?$/.test(timeoutText)) {
      timeout = Number(timeoutText);
    } else {
      return { ok: false, error: { code: "invalidTimeout", serviceIndex } };
    }
    if (timeout <= 0 || timeout > 600) {
      return { ok: false, error: { code: "invalidTimeout", serviceIndex } };
    }
    const envResult = parseEnvText(svc.envText, serviceIndex);
    if (!envResult.ok) return envResult;

    const envPassthrough = linesToArray(svc.envPassthroughText);
    for (const varName of envPassthrough) {
      if (!ENV_NAME_RE.test(varName)) {
        return {
          ok: false,
          error: { code: "invalidEnvPassthroughName", serviceIndex, line: varName },
        };
      }
    }

    services.push({
      name,
      command,
      cwd: svc.cwd.trim() || ".",
      base_port: port,
      readiness: {
        path: svc.readinessPath.trim() || null,
        timeout_seconds: timeout,
      },
      env: envResult.env,
      env_file: linesToArray(svc.envFileText),
      env_passthrough: envPassthrough,
    });
  }

  const seenNames = new Map<string, number>();
  for (const [serviceIndex, service] of services.entries()) {
    if (seenNames.has(service.name)) {
      return { ok: false, error: { code: "duplicateServiceName", serviceIndex } };
    }
    seenNames.set(service.name, serviceIndex);
  }

  // Port references resolve against THIS config's services ({port:name}) or,
  // qualified as {port:repo/service}, another repo's saved config — an
  // undeclared reference would otherwise only explode at launch time,
  // mid-agent-turn. Mirrors the backend PUT-handler check.
  for (const [serviceIndex, service] of services.entries()) {
    for (const text of [...service.command, ...Object.values(service.env ?? {})]) {
      for (const match of text.matchAll(PORT_REF_RE)) {
        const ref = match[1];
        // Repo names cannot contain "/", so the first slash splits the
        // qualified form; everything after it is the service name.
        const slash = ref.indexOf("/");
        if (slash === -1) {
          if (!seenNames.has(ref)) {
            return {
              ok: false,
              error: { code: "unknownPortReference", serviceIndex, line: ref },
            };
          }
        } else if (crossRepo !== undefined) {
          // Qualified {port:repo/service}. The draft is the authority for the
          // edited repo itself; other repos validate against their saved config.
          const refRepo = ref.slice(0, slash);
          const refService = ref.slice(slash + 1);
          if (refRepo === crossRepo.selfRepoName) {
            if (!seenNames.has(refService)) {
              return {
                ok: false,
                error: { code: "unknownPortReference", serviceIndex, line: refService },
              };
            }
            // Object.hasOwn (not `in`): a repo literally named "toString" or
            // "constructor" must hit the not-found branch, not the prototype.
          } else if (!Object.hasOwn(crossRepo.repoServices, refRepo)) {
            return {
              ok: false,
              error: { code: "unknownRepoReference", serviceIndex, line: refRepo },
            };
          } else if (!crossRepo.repoServices[refRepo].includes(refService)) {
            return {
              ok: false,
              error: { code: "unknownRemoteService", serviceIndex, line: ref },
            };
          }
        }
      }
    }
  }

  return {
    ok: true,
    config: {
      services,
      browser: {
        allowed_origins: linesToArray(draft.allowedOriginsText),
        allow_common_cdns: draft.allowCommonCdns,
      },
    },
  };
}
