import { writeFile, readFile, mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import {
  GWIKI_DEFAULT_SERVICE_TOKEN,
  GWIKI_REGISTER_PATH,
  GWIKI_SSO_EXCHANGE_PATH,
} from "./builtinAuth.js";
import {
  clearSharedSso,
  loadSharedSso,
  loginWithChromeCdp,
  saveSharedSso,
  verifyGuazisso,
} from "./browserAuth.js";

/**
 * Preferred registration path: reuse (or obtain via visible Chrome QR scan)
 * the shared GUAZISSO, exchange it for a personal cloud token. Works in both
 * terminals and agent contexts — the Chrome window opens on the user's
 * desktop either way.
 */
export async function registerViaSso({
  cloudUrl,
  env = process.env,
  onStatus = () => {},
} = {}) {
  let guazisso;
  const cached = await loadSharedSso(env);
  if (cached?.guazisso) {
    const verified = await verifyGuazisso(cached.guazisso, env);
    if (verified.ok) {
      guazisso = cached.guazisso;
      onStatus("使用共享 SSO 缓存，无需重新扫码。");
    } else if (verified.reason === "network") {
      throw new Error("SSO 校验接口无法访问，请检查网络后重试");
    } else {
      await clearSharedSso(env);
    }
  }
  if (!guazisso) {
    const login = await loginWithChromeCdp({ env, onStatus });
    guazisso = login.guazisso;
    await saveSharedSso(login.guazisso, login.domain, env);
  }

  onStatus("正在向云端换取访问 token…");
  const url = `${String(cloudUrl).replace(/\/+$/, "")}${GWIKI_SSO_EXCHANGE_PATH}`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ guazisso }),
    signal: AbortSignal.timeout(30_000),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`SSO 换取 token 失败（${response.status}）: ${body.error || "unknown"}`);
  }
  await saveToConfig({ serviceToken: body.token, userEmail: body.email }, env);
  return { token: body.token, email: body.email, name: body.name || "" };
}

/**
 * Register the current user by opening a browser for identity extraction.
 * Returns { token, email } on success.
 */
async function callRegisterApi(cloudUrl, { email, name, guazisso }) {
  const url = `${cloudUrl.replace(/\/+$/, "")}${GWIKI_REGISTER_PATH}`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, name, guazisso }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Registration failed (${response.status}): ${body}`);
  }

  return response.json();
}

function resolveConfigPath(env = process.env) {
  return (
    env.LLM_WIKI_CLOUD_CONFIG ||
    path.join(os.homedir(), ".config", "gwiki", "config.yaml")
  );
}

async function saveToConfig({ serviceToken, userEmail }, env = process.env) {
  const configPath = resolveConfigPath(env);
  const dir = path.dirname(configPath);
  await mkdir(dir, { recursive: true });

  let content = "";
  try {
    content = await readFile(configPath, "utf8");
  } catch {
    // file doesn't exist yet
  }

  const lines = content.split(/\r?\n/).filter((line) => {
    const key = line.split(":")[0].trim();
    return key !== "service_token" && key !== "user_email";
  });

  lines.push(`service_token: ${serviceToken}`);
  lines.push(`user_email: ${userEmail}`);

  await writeFile(configPath, lines.join("\n") + "\n", "utf8");
}

/**
 * Manual registration without the browser scan — requires the GUAZISSO to
 * come from somewhere (argument or env) because the register API now only
 * issues tokens for verified SSO identities.
 */
export async function registerManual({ cloudUrl, email, guazisso, env = process.env }) {
  const sso = String(guazisso || env.GUAZISSO || "").trim();
  if (!sso) {
    throw new Error(
      "手动注册需要 GUAZISSO：请设置环境变量 GUAZISSO 后重试，或直接执行 gwiki --register 扫码注册",
    );
  }
  const { token } = await callRegisterApi(cloudUrl, { email, guazisso: sso });
  await saveToConfig({ serviceToken: token, userEmail: email }, env);
  return { token, email };
}

export function needsUserRegistration(config) {
  return !config.userEmail && config.serviceToken === GWIKI_DEFAULT_SERVICE_TOKEN;
}

export function canPromptForRegistration() {
  return process.stdin.isTTY;
}

/** Agent-safe marker on stdout — agents must surface this in chat, not rely on terminal stdin. */
export function printRegistrationRequired() {
  console.log("首次使用 GWiki，需要扫码登录完成注册。");
  console.log("请在终端执行: gwiki --register（会打开 Chrome 扫码，一次即可）");
  console.log("无法打开浏览器时，设置环境变量 GUAZISSO 后执行: gwiki --register --email you@guazi.com");
  console.log("[gwiki-registration-required]");
}
