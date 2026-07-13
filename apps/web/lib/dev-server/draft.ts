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
  | { code: "invalidEnvLine"; serviceIndex: number; line: string };

/** Mirrors the backend DevService.name regex (models/project.py). */
const SERVICE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

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
export function configFromDraft(draft: DevServerDraft): DraftConversionResult {
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
    });
  }

  const seenNames = new Map<string, number>();
  for (const [serviceIndex, service] of services.entries()) {
    if (seenNames.has(service.name)) {
      return { ok: false, error: { code: "duplicateServiceName", serviceIndex } };
    }
    seenNames.set(service.name, serviceIndex);
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
