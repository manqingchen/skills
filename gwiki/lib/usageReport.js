import { readFile, mkdir, writeFile, open } from "node:fs/promises";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const GWIKI_PLATFORM_SLUG = "gwiki";
/** Local mock for tests — set SKILLS_MARKET_URL explicitly in test runs. */
export const TEST_SKILLS_MARKET_URL = "https://skills.guazi-apps.com";
/** Company GrapeHub production host (source + runtime default). */
export const PROD_SKILLS_MARKET_URL = "https://skills.guazi-apps.com";
export const DEFAULT_SKILLS_MARKET_URL = PROD_SKILLS_MARKET_URL;
/** Cross-process dedup window so list-kbs+lock in one turn counts once. */
export const USAGE_DEDUP_TTL_MS = 30 * 60 * 1000;

const skillRootFromModule = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/**
 * Resolve gwiki skill root (directory containing package.json / bin/gwiki.js).
 */
export function resolveSkillRoot(env = process.env) {
  if (env.GWIKI_SKILL_ROOT) {
    return path.resolve(env.GWIKI_SKILL_ROOT);
  }
  return skillRootFromModule;
}

/**
 * Read bundle version from package.json or VERSION files.
 */
export async function readGwikiVersion(skillRoot = resolveSkillRoot()) {
  const candidates = [
    path.join(skillRoot, "package.json"),
    path.join(skillRoot, "VERSION"),
    path.join(skillRoot, "..", "VERSION"),
    path.join(skillRoot, "..", "..", "VERSION"),
  ];

  for (const candidate of candidates) {
    try {
      const text = await readFile(candidate, "utf8");
      if (candidate.endsWith("package.json")) {
        const parsed = JSON.parse(text);
        if (parsed?.version) return String(parsed.version).trim();
        continue;
      }
      const match = text.match(/^version:\s*(.+)$/m);
      if (match?.[1]) return match[1].trim();
    } catch (error) {
      if (error?.code === "ENOENT") continue;
    }
  }
  return "0.0.0";
}

function marketBaseUrl(env = process.env) {
  const raw = env.SKILLS_MARKET_URL || DEFAULT_SKILLS_MARKET_URL;
  return String(raw).replace(/\/+$/, "");
}

function utcTimestamp(now = new Date()) {
  return now.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function usageDedupPath(env = process.env) {
  if (env.GWIKI_USAGE_DEDUP_PATH) {
    return path.resolve(env.GWIKI_USAGE_DEDUP_PATH);
  }
  const base = env.XDG_CACHE_HOME
    ? path.resolve(env.XDG_CACHE_HOME)
    : path.join(os.homedir(), ".cache");
  return path.join(base, "gwiki", "usage-started.dedup");
}

/**
 * Try to claim a short-lived dedup slot. Returns false if another process
 * already reported within USAGE_DEDUP_TTL_MS.
 */
export async function tryClaimUsageDedup({
  env = process.env,
  now = new Date(),
  ttlMs = USAGE_DEDUP_TTL_MS,
  dedupPath = usageDedupPath(env),
} = {}) {
  if (env.GWIKI_USAGE_DEDUP === "0") {
    return { claimed: true, skipped: false, reason: "dedup_disabled" };
  }
  await mkdir(path.dirname(dedupPath), { recursive: true });
  const nowMs = now.getTime();
  try {
    if (existsSync(dedupPath)) {
      const raw = await readFile(dedupPath, "utf8");
      const prev = Number.parseInt(String(raw).trim(), 10);
      if (Number.isFinite(prev) && nowMs - prev < ttlMs) {
        return { claimed: false, skipped: true, reason: "recently_reported", dedupPath, ageMs: nowMs - prev };
      }
    }
  } catch {
    // fall through and try to claim
  }

  try {
    const handle = await open(dedupPath, "w");
    try {
      await handle.writeFile(String(nowMs), "utf8");
    } finally {
      await handle.close();
    }
    return { claimed: true, skipped: false, reason: "claimed", dedupPath };
  } catch {
    // Best-effort: if we cannot write marker, still allow report once.
    return { claimed: true, skipped: false, reason: "claim_write_failed", dedupPath };
  }
}

/**
 * Fire-and-forget usage report matching GrapeHub skill-usage-events fields.
 * Never throws; never retries; never logs secrets.
 * Cross-process short TTL dedup prevents multi-CLI double counts.
 */
export async function reportSkillUsageStarted({
  env = process.env,
  fetchImpl = globalThis.fetch,
  skillRoot = resolveSkillRoot(env),
  now = new Date(),
  ttlMs = USAGE_DEDUP_TTL_MS,
  dedupPath = usageDedupPath(env),
} = {}) {
  if (env.GWIKI_DISABLE_USAGE_REPORT === "1") {
    return { ok: false, skipped: true, reason: "disabled" };
  }
  if (typeof fetchImpl !== "function") {
    return { ok: false, skipped: true, reason: "no_fetch" };
  }

  const claim = await tryClaimUsageDedup({ env, now, ttlMs, dedupPath });
  if (!claim.claimed) {
    return { ok: false, skipped: true, reason: "dedup", claim };
  }

  const version = await readGwikiVersion(skillRoot);
  const body = {
    skill: GWIKI_PLATFORM_SLUG,
    event: "started",
    version,
    timestamp: utcTimestamp(now),
  };

  const url = `${marketBaseUrl(env)}/api/v1/skill-usage-events`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2000);

  try {
    const response = await fetchImpl(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    return { ok: Boolean(response?.ok), status: response?.status, body, claim };
  } catch {
    return { ok: false, skipped: false, reason: "network_error", body, claim };
  } finally {
    clearTimeout(timer);
  }
}
