import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

function resolveHome() {
  return process.env.HOME || os.homedir();
}

function lockPaths() {
  const configDir = path.join(resolveHome(), ".config", "gwiki");
  return {
    configDir,
    lockFile: path.join(configDir, "lock.yaml"),
    lockTmp: path.join(configDir, "lock.yaml.tmp"),
  };
}

/**
 * Read the persistent KB lock.
 * @returns {Promise<{defaultKbs: string[], exists: boolean, parseError?: string}>}
 */
export async function readLock() {
  const { lockFile } = lockPaths();
  let raw;
  try {
    raw = await readFile(lockFile, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") {
      return { defaultKbs: [], exists: false };
    }
    throw error;
  }
  try {
    const parsed = parseSimpleYaml(raw);
    const list = Array.isArray(parsed.default_kbs) ? parsed.default_kbs : [];
    return { defaultKbs: list.map(String), exists: true };
  } catch (error) {
    return {
      defaultKbs: [],
      exists: true,
      parseError: error?.message || String(error),
    };
  }
}

/**
 * Read the persistent KB lock. On parse error, back up the corrupt file
 * to lock.yaml.bak.<timestamp> and report the backup path.
 * @returns {Promise<{defaultKbs: string[], exists: boolean, parseError?: string, backupPath?: string}>}
 */
export async function readLockWithBackup() {
  const result = await readLock();
  if (!result.parseError) return result;
  const { lockFile } = lockPaths();
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const backupPath = `${lockFile}.bak.${ts}`;
  try {
    await rename(lockFile, backupPath);
  } catch (error) {
    // best-effort backup; if rename fails, still report parseError
    return result;
  }
  return { ...result, backupPath, exists: false };
}

/**
 * Write (append) the persistent KB lock atomically. Deduplicates against existing KBs.
 * @param {string[]} kbs - KB ids to add to the lock.
 */
export async function writeLock(kbs) {
  const ids = Array.isArray(kbs) ? kbs.map(String) : [];
  const current = await readLock();
  const merged = uniq([...current.defaultKbs, ...ids]);
  const { configDir, lockFile, lockTmp } = lockPaths();
  await mkdir(configDir, { recursive: true });
  const serialized = serializeLock({ default_kbs: merged });
  await writeFile(lockTmp, serialized, "utf8");
  await rename(lockTmp, lockFile);
}

/**
 * Replace (overwrite) the persistent KB lock atomically.
 * @param {string[]} kbs - KB ids to set as the new lock.
 */
export async function replaceLock(kbs) {
  const ids = Array.isArray(kbs) ? kbs.map(String) : [];
  const { configDir, lockFile, lockTmp } = lockPaths();
  await mkdir(configDir, { recursive: true });
  const serialized = serializeLock({ default_kbs: ids });
  await writeFile(lockTmp, serialized, "utf8");
  await rename(lockTmp, lockFile);
}

/**
 * Clear the persistent KB lock (remove file).
 */
export async function clearLock() {
  const { lockFile } = lockPaths();
  try {
    await unlink(lockFile);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
}

// Heuristic: kb_ids are ASCII identifiers (alphanumeric with optional
// dashes/underscores). Business names contain other characters (e.g. CJK).
// This is used to decide whether an arg is an exact kb_id (no cloud lookup
// needed) or a business name (needs listKbs to fuzzy-resolve).
const KB_ID_PATTERN = /^[a-zA-Z][a-zA-Z0-9_-]*$/;

export function looksLikeKbId(value) {
  return KB_ID_PATTERN.test(String(value || "").trim());
}

/**
 * Resolve business names / kb_ids to kb_ids via Cloud listKbs.
 * @param {string[]} names - each arg may be a kb_id (exact) or a business name.
 * @param {{listKbs: () => Promise<Array<{id: string, display_name?: string, description?: string, metadata?: any}>>}} deps
 * @returns {Promise<{ids: string[], ambiguous: Array<{name: string, candidates: any[]}>, unknown: string[]}>}
 */
export async function resolveKbIds(names, { listKbs }) {
  const kbs = await listKbs();
  const lookup = new Map();
  for (const kb of kbs) {
    if (kb?.id) lookup.set(String(kb.id), kb);
  }
  const ids = [];
  const ambiguous = [];
  const unknown = [];
  for (const raw of names) {
    const name = String(raw || "").trim();
    if (!name) continue;
    if (lookup.has(name)) {
      ids.push(name);
      continue;
    }
    const candidates = kbs.filter((kb) => matchesKb(kb, name));
    if (candidates.length === 1) {
      ids.push(candidates[0].id);
    } else if (candidates.length > 1) {
      // One ambiguous entry per input name, with all matching KBs in `candidates`.
      ambiguous.push({ name, candidates });
    } else {
      unknown.push(name);
    }
  }
  return { ids: uniq(ids), ambiguous, unknown };
}

function matchesKb(kb, name) {
  const lower = name.toLowerCase();
  const displayName = String(kb.display_name || "").toLowerCase();
  const description = String(kb.description || "").toLowerCase();
  if (displayName && displayName.includes(lower)) return true;
  if (description && description.includes(lower)) return true;
  const terms = kb.metadata?.routingIndex?.routing?.terms || [];
  return terms.some((t) => String(t || "").toLowerCase().includes(lower));
}

function uniq(arr) {
  return Array.from(new Set(arr));
}

function serializeLock(obj) {
  const list = obj.default_kbs || [];
  if (list.length === 0) {
    return "# gwiki KB lock\n# Empty list = no persistent default.\ndefault_kbs: []\n";
  }
  const lines = list.map((id) => `  - ${id}`);
  return `# gwiki KB lock — persistent default KBs (auto-loaded by gwiki.js main path).\n# Remove entries or delete this file to clear.\ndefault_kbs:\n${lines.join("\n")}\n`;
}

function parseSimpleYaml(text) {
  const result = {};
  const lines = String(text || "").split(/\r?\n/);
  let currentList = null;
  let anyContent = false;
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    anyContent = true;
    if (trimmed.startsWith("- ")) {
      if (currentList) {
        const value = trimmed.slice(2).trim();
        currentList.push(stripQuotes(value));
      }
      continue;
    }
    const sep = trimmed.indexOf(":");
    if (sep < 0) {
      throw new Error(`Unexpected YAML line: ${trimmed}`);
    }
    const key = trimmed.slice(0, sep).trim();
    if (!key) {
      throw new Error(`Empty key in YAML line: ${trimmed}`);
    }
    let value = trimmed.slice(sep + 1).trim();
    if (value === "[]" || value === "") {
      result[key] = [];
      currentList = result[key];
      continue;
    }
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    result[key] = value;
    currentList = null;
  }
  if (!anyContent) {
    throw new Error("Empty YAML document");
  }
  return result;
}

function stripQuotes(value) {
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    return value.slice(1, -1);
  }
  return value;
}