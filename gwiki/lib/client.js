import { resolveSkillRoot, readGwikiVersion } from "./usageReport.js";

let _skillVersion = null;

export function resetGwikiClientCachesForTests() {
  _skillVersion = null;
}

async function getSkillVersion() {
  if (_skillVersion) return _skillVersion;
  _skillVersion = await readGwikiVersion(resolveSkillRoot());
  return _skillVersion;
}

export async function listKbs({ cloudUrl, serviceToken }) {
  return requestJson({ cloudUrl, serviceToken, path: "/admin/kbs" });
}

export async function submitJob({ cloudUrl, serviceToken, payload }) {
  return requestJson({
    cloudUrl,
    serviceToken,
    path: "/kb/jobs",
    method: "POST",
    body: payload,
    expectedStatuses: new Set([202]),
  });
}

export async function getJobResult({ cloudUrl, serviceToken, jobId }) {
  return requestJson({
    cloudUrl,
    serviceToken,
    path: `/kb/jobs/${encodeURIComponent(jobId)}/result`,
    expectedStatuses: new Set([200, 202]),
  });
}

export async function streamJobEvents({ cloudUrl, serviceToken, jobId, onEvent }) {
  const response = await fetch(resolveCloudUrl(cloudUrl, `/kb/jobs/${encodeURIComponent(jobId)}/events`), {
    headers: await buildHeaders(serviceToken),
  });
  if (!response.ok) {
    return false;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const dataLine = part.split("\n").find((line) => line.startsWith("data:"));
      if (!dataLine) {
        continue;
      }
      const event = JSON.parse(dataLine.slice("data:".length).trim());
      onEvent(event);
      if (["completed", "done", "failed", "stale", "cancelled"].includes(event.type)) {
        await reader.cancel();
        return true;
      }
    }
  }
  return true;
}

let tokenRefresher = null;

/**
 * Install the callback that renews the personal token via SSO exchange.
 * On a 401 SSO_REVERIFY_REQUIRED (weekly SSO freshness window elapsed) every
 * request transparently refreshes once and retries, instead of failing.
 * The refresher returns the fresh token (same token, re-verified).
 */
export function setTokenRefresher(refresher) {
  tokenRefresher = refresher;
}

async function requestJson({
  cloudUrl,
  serviceToken,
  path,
  method = "GET",
  body,
  expectedStatuses = new Set([200]),
}) {
  let token = serviceToken;
  for (let attempt = 0; ; attempt += 1) {
    const response = await fetch(resolveCloudUrl(cloudUrl, path), {
      method,
      headers: await buildHeaders(token),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    const parsed = text ? JSON.parse(text) : {};
    if (expectedStatuses.has(response.status)) {
      return parsed;
    }
    if (
      response.status === 401 &&
      attempt === 0 &&
      parsed?.code === "SSO_REVERIFY_REQUIRED" &&
      tokenRefresher
    ) {
      token = await tokenRefresher();
      continue;
    }
    if (response.status === 401) {
      const msg = parsed?.message || parsed?.error || "unauthorized";
      throw new Error(`401 ${msg}`);
    }
    throw new Error(`${response.status} ${JSON.stringify(parsed)}`);
  }
}

async function buildHeaders(serviceToken) {
  const version = await getSkillVersion();
  return {
    ...(serviceToken ? { authorization: `Bearer ${serviceToken}` } : {}),
    "content-type": "application/json",
    "x-gwiki-version": version,
  };
}

function resolveCloudUrl(cloudUrl, requestPath) {
  const base = new URL(String(cloudUrl || ""));
  const joinedPath = `${base.pathname.replace(/\/+$/, "")}/${String(requestPath || "").replace(/^\/+/, "")}`;
  return new URL(`${joinedPath}${base.search}`, `${base.origin}/`);
}
