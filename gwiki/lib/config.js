import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import {
  GWIKI_DEFAULT_CLOUD_URL,
  GWIKI_DEFAULT_SERVICE_TOKEN,
} from "./builtinAuth.js";

export async function loadConfig(env = process.env) {
  const fileConfig = await readFileConfig(env);
  return {
    cloudUrl: env.LLM_WIKI_CLOUD_URL || fileConfig.cloud_url || GWIKI_DEFAULT_CLOUD_URL,
    serviceToken:
      env.LLM_WIKI_CLOUD_TOKEN || fileConfig.service_token || GWIKI_DEFAULT_SERVICE_TOKEN,
    userEmail: env.LLM_WIKI_CLOUD_USER_EMAIL || fileConfig.user_email || "",
    defaultMode: fileConfig.default_mode || "async",
    pollIntervalMs: Number(env.LLM_WIKI_CLOUD_POLL_INTERVAL_MS || fileConfig.poll_interval_ms || 1000),
    timeoutMs: Number(env.LLM_WIKI_CLOUD_TIMEOUT_MS || fileConfig.timeout_ms || 10 * 60 * 1000),
  };
}

async function readFileConfig(env) {
  const configPath =
    env.LLM_WIKI_CLOUD_CONFIG ||
    path.join(os.homedir(), ".config", "gwiki", "config.yaml");
  try {
    return parseSimpleYaml(await readFile(configPath, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") {
      return {};
    }
    throw error;
  }
}

function parseSimpleYaml(text) {
  const config = {};
  for (const line of String(text || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const separator = trimmed.indexOf(":");
    if (separator < 0) continue;
    const key = trimmed.slice(0, separator).trim();
    let value = trimmed.slice(separator + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    config[key] = value;
  }
  return config;
}
