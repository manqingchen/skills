/**
 * Browser SSO login for gwiki: launch a visible Chrome via CDP, wait for the
 * user to scan the Guazi SSO QR code, capture the GUAZISSO cookie (including
 * HttpOnly), and share it with the luban-query / data-asset-query skills via
 * the common cache file.
 *
 * Ported from those skills' Python implementation (data_asset_auth.py), so
 * behavior matches: temp profile, random debug port, launchctl-asuser on
 * macOS so the window is visible from restricted agent contexts.
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";

// Same shared cache the other skills read/write: one QR scan serves all.
const DEFAULT_AUTH_FILE = path.join(os.homedir(), ".agents", "cache", "sso", "guazisso.json");
const DEFAULT_LOGIN_URL = "https://sso-web.guazi-apps.com/";
const DEFAULT_VERIFY_URL = "https://sso.guazi-apps.com/account/checkIsLogin";
const DEFAULT_LOGIN_TIMEOUT_MS = 300_000;
const DEBUG_ENDPOINT_TIMEOUT_MS = 20_000;
const POLL_INTERVAL_MS = 1000;
const HEARTBEAT_INTERVAL_MS = 15_000;

export function authFilePath(env = process.env) {
  return env.GWIKI_AUTH_FILE || DEFAULT_AUTH_FILE;
}

export async function loadSharedSso(env = process.env) {
  try {
    const payload = JSON.parse(await readFile(authFilePath(env), "utf8"));
    if (payload && typeof payload === "object" && payload.GUAZISSO) {
      return {
        guazisso: String(payload.GUAZISSO),
        domain: String(payload.domain || ""),
        updatedAt: String(payload.updated_at || ""),
      };
    }
  } catch (error) {
    if (error?.code !== "ENOENT") {
      // Unreadable cache is not fatal: fall through to a fresh login.
    }
  }
  return null;
}

export async function saveSharedSso(guazisso, domain = "", env = process.env) {
  const filePath = authFilePath(env);
  await mkdir(path.dirname(filePath), { recursive: true });
  const payload = {
    GUAZISSO: guazisso,
    domain,
    updated_at: new Date().toISOString().replace("T", " ").slice(0, 19),
  };
  await writeFile(filePath, JSON.stringify(payload, null, 2) + "\n", { mode: 0o600 });
}

export async function clearSharedSso(env = process.env) {
  const filePath = authFilePath(env);
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(filePath, "{}\n", { mode: 0o600 });
}

/**
 * Verify a GUAZISSO cookie against the SSO check endpoint.
 * Returns { ok: true, email, name } or { ok: false, reason } where reason is
 * "invalid" (cookie expired) or "network" (endpoint unreachable) — callers
 * must not treat a network failure as an expired cookie.
 */
export async function verifyGuazisso(guazisso, env = process.env) {
  const token = String(guazisso || "").trim();
  if (!token) return { ok: false, reason: "invalid" };
  const verifyUrl = env.GWIKI_SSO_VERIFY_URL || DEFAULT_VERIFY_URL;
  let response;
  try {
    response = await fetch(verifyUrl, {
      headers: {
        accept: "application/json",
        cookie: `GUAZISSO=${token}`,
        "user-agent": "gwiki-skill/2.3.0",
      },
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    return { ok: false, reason: "network" };
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    return { ok: false, reason: "network" };
  }
  if (!payload || typeof payload !== "object" || String(payload.code) !== "0") {
    return { ok: false, reason: "invalid" };
  }
  const data = payload.data && typeof payload.data === "object" ? payload.data : {};
  const email = String(data.email || "").trim();
  if (!email) return { ok: false, reason: "invalid" };
  return { ok: true, email, name: String(data.fullname || "").trim() };
}

function chromeCandidates(env) {
  const configured = (env.GWIKI_CHROME_PATH || "").trim();
  const candidates = configured ? [configured] : [];
  if (process.platform === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      path.join(os.homedir(), "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    );
  } else if (process.platform === "win32") {
    for (const base of [env.ProgramFiles, env["ProgramFiles(x86)"], env.LOCALAPPDATA]) {
      if (base) {
        candidates.push(
          path.join(base, "Google/Chrome/Application/chrome.exe"),
          path.join(base, "Microsoft/Edge/Application/msedge.exe"),
          path.join(base, "Chromium/Application/chrome.exe"),
        );
      }
    }
  } else {
    for (const command of ["google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome", "msedge"]) {
      const result = spawnSync("which", [command], { encoding: "utf8" });
      const resolved = (result.stdout || "").trim();
      if (result.status === 0 && resolved) candidates.push(resolved);
    }
  }
  return [...new Set(candidates.filter(Boolean))];
}

export function findChrome(env = process.env) {
  return chromeCandidates(env).find((candidate) => existsSync(candidate)) || null;
}

function allocateDebugPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url, timeoutMs = 5000) {
  const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  return response.json();
}

function launchChrome({ chromePath, port, profileDir, loginUrl }) {
  const chromeArguments = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-default-browser-check",
    "--new-window",
    loginUrl,
  ];
  if (process.platform === "darwin") {
    // Agent processes (WorkBuddy etc.) live outside the user's GUI session,
    // so a plain spawn opens an invisible window; `open -na` drops the
    // debugging port when Chrome is already running. launchctl-asuser puts
    // the binary in the Aqua session with CDP intact, and --no-sandbox keeps
    // Chrome's own sandbox from failing to init in that context.
    return spawn(
      "launchctl",
      [
        "asuser",
        String(process.getuid()),
        chromePath,
        "--no-sandbox",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--no-zygote",
        ...chromeArguments,
      ],
      { stdio: "ignore" },
    );
  }
  return spawn(chromePath, chromeArguments, { stdio: "ignore" });
}

async function waitForDebugEndpoint(port, child) {
  const deadline = Date.now() + DEBUG_ENDPOINT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const payload = await fetchJson(`http://127.0.0.1:${port}/json/version`);
      if (payload && payload.webSocketDebuggerUrl) return;
    } catch {
      // not up yet
    }
    if (child.exitCode !== null) {
      throw new Error(`Chrome 启动器提前退出（退出码 ${child.exitCode}）`);
    }
    await sleep(200);
  }
  throw new Error("等待 Chrome CDP 服务超时");
}

async function pageWebSocketUrl(port) {
  const targets = await fetchJson(`http://127.0.0.1:${port}/json/list`);
  if (!Array.isArray(targets)) return null;
  const pages = targets.filter((target) => target && target.type === "page");
  const preferred = pages.find((page) => String(page.url || "").includes("guazi-apps.com"));
  const target = preferred || pages[0];
  return target?.webSocketDebuggerUrl || null;
}

async function cdpCall(webSocketUrl, method, params = {}, timeoutMs = 5000) {
  if (typeof WebSocket === "undefined") {
    throw new Error("当前 Node 版本无全局 WebSocket（需要 Node >= 21），无法进行 CDP 登录");
  }
  return await new Promise((resolve, reject) => {
    const socket = new WebSocket(webSocketUrl);
    const timer = setTimeout(() => {
      try {
        socket.close();
      } catch {}
      reject(new Error("CDP 调用超时"));
    }, timeoutMs);
    const fail = (error) => {
      clearTimeout(timer);
      reject(error);
    };
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ id: 1, method, params }));
    });
    socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (message.id !== 1) return;
      clearTimeout(timer);
      try {
        socket.close();
      } catch {}
      if (message.error) {
        reject(new Error(`CDP 调用失败: ${message.error.message || "unknown"}`));
      } else {
        resolve(message.result || {});
      }
    });
    socket.addEventListener("error", () => fail(new Error("CDP WebSocket 连接失败")));
  });
}

async function readGuazissoCookie(port) {
  const webSocketUrl = await pageWebSocketUrl(port);
  if (!webSocketUrl) return null;
  const result = await cdpCall(webSocketUrl, "Storage.getCookies");
  const cookies = Array.isArray(result.cookies) ? result.cookies : [];
  const cookie = cookies.find((item) => String(item?.name || "").toUpperCase() === "GUAZISSO");
  if (!cookie || !cookie.value) return null;
  return { value: String(cookie.value), domain: String(cookie.domain || "") };
}

async function closeChromeViaCdp(port) {
  try {
    const payload = await fetchJson(`http://127.0.0.1:${port}/json/version`);
    if (payload?.webSocketDebuggerUrl) {
      await cdpCall(payload.webSocketDebuggerUrl, "Browser.close");
    }
  } catch {
    // Chrome may already be gone; nothing to clean up.
  }
}

/**
 * Full visible-Chrome QR login. Resolves with
 * { guazisso, domain, email, name } once a verified cookie exists.
 */
export async function loginWithChromeCdp({
  timeoutMs,
  env = process.env,
  onStatus = () => {},
} = {}) {
  const loginTimeout = timeoutMs || Number(env.GWIKI_SSO_LOGIN_TIMEOUT_MS) || DEFAULT_LOGIN_TIMEOUT_MS;
  const chromePath = findChrome(env);
  if (!chromePath) {
    throw new Error("未找到 Chrome/Edge/Chromium，请安装或设置 GWIKI_CHROME_PATH");
  }
  if (typeof WebSocket === "undefined") {
    throw new Error("当前 Node 版本无全局 WebSocket（需要 Node >= 21），无法自动扫码登录");
  }
  const loginUrl = env.GWIKI_SSO_LOGIN_URL || DEFAULT_LOGIN_URL;
  const sessionDir = await mkdtemp(path.join(os.tmpdir(), "gwiki-sso-chrome-"));
  const profileDir = path.join(sessionDir, "profile");
  await mkdir(profileDir, { recursive: true });
  const port = await allocateDebugPort();
  let child = null;
  try {
    child = launchChrome({ chromePath, port, profileDir, loginUrl });
    await waitForDebugEndpoint(port, child);
    onStatus(`已打开 SSO 登录页: ${loginUrl}`);
    onStatus("请在 Chrome 中完成扫码登录，脚本会自动检测 GUAZISSO。");
    const deadline = Date.now() + loginTimeout;
    let lastHeartbeat = 0;
    let lastRejected = "";
    while (Date.now() < deadline) {
      let cookie = null;
      try {
        cookie = await readGuazissoCookie(port);
      } catch {
        // target may disappear mid-navigation; keep polling
      }
      if (child && child.exitCode !== null) {
        throw new Error("浏览器已关闭，登录中止");
      }
      if (cookie && cookie.value !== lastRejected) {
        onStatus("检测到 GUAZISSO，正在验证…");
        const verified = await verifyGuazisso(cookie.value, env);
        if (verified.ok) {
          return {
            guazisso: cookie.value,
            domain: cookie.domain,
            email: verified.email,
            name: verified.name,
          };
        }
        if (verified.reason === "network") {
          throw new Error("SSO 校验接口无法访问，请检查网络后重试");
        }
        lastRejected = cookie.value;
        onStatus("当前 Cookie 尚未生效，继续等待登录完成。");
      }
      const now = Date.now();
      if (now - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
        onStatus(`[等待登录] 剩余 ${Math.max(0, Math.round((deadline - now) / 1000))} 秒`);
        lastHeartbeat = now;
      }
      await sleep(POLL_INTERVAL_MS);
    }
    throw new Error(`登录超时（${Math.round(loginTimeout / 1000)} 秒），未获取到有效 GUAZISSO`);
  } finally {
    await closeChromeViaCdp(port);
    if (child && child.exitCode === null) {
      child.kill();
    }
    await rm(sessionDir, { recursive: true, force: true, maxRetries: 5 }).catch(() => {});
  }
}
