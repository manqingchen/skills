#!/usr/bin/env node
import { access } from "node:fs/promises";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import readline from "node:readline/promises";

import {
  getJobResult,
  listKbs,
  setTokenRefresher,
  streamJobEvents,
  submitJob,
} from "../lib/client.js";
import { loadConfig } from "../lib/config.js";
import {
  buildReviewRequirementJobInput,
} from "../lib/requirementInput.js";
import {
  cloudJobTypeForIntent,
  commandForIntent,
  detectGwikiIntent,
} from "../lib/intent.js";
import {
  formatQueryAnswer,
  summarizeMarkdownForDisplay,
} from "../lib/formatAnswer.js";
import {
  needsUserRegistration,
  printRegistrationRequired,
  registerManual,
  registerViaSso,
} from "../lib/userAuth.js";
import { GWIKI_DEFAULT_SERVICE_TOKEN } from "../lib/builtinAuth.js";
import {
  clearLock,
  looksLikeKbId,
  readLockWithBackup,
  replaceLock,
  resolveKbIds,
  writeLock,
} from "../lib/lock.js";
import { reportSkillUsageStarted } from "../lib/usageReport.js";

const COMMAND_ALIASES = {
  plus: "query-plus",
  "query-plus": "query-plus",
  "gwiki-plus": "query-plus",
  "requirement-review": "review-requirement",
  "gwiki-requirement-review": "review-requirement",
  "requirement-write": "requirement-write",
  "gwiki-write-requirement": "requirement-write",
  "write-requirement": "requirement-write",
  ask: "ask",
  lock: "lock",
  relock: "relock",
  unlock: "unlock",
  "lock-status": "lock-status",
  "auth-status": "auth-status",
};

main().catch((error) => {
  console.error(`Cloud access failed: ${error.message}`);
  process.exitCode = 1;
});

async function main() {
  const config = await loadConfig();

  // Weekly SSO freshness: when the cloud reports the token needs
  // re-verification (SSO_REVERIFY_REQUIRED), transparently re-run the SSO
  // exchange (silent when the shared cache is valid, QR scan otherwise) and
  // retry the original request once.
  setTokenRefresher(async () => {
    const result = await registerViaSso({
      cloudUrl: config.cloudUrl,
      onStatus: (message) => console.error(message),
    });
    config.serviceToken = result.token;
    config.userEmail = result.email;
    return result.token;
  });

  // GrapeHub usage report (non-blocking; never fails the CLI).
  await reportSkillUsageStarted().catch(() => {});

  // Handle manual registration: gwiki --register --email user@guazi.com
  const registerFlag = process.argv.includes("--register");
  const emailIdx = process.argv.indexOf("--email");
  const emailFlag = emailIdx >= 0 ? process.argv[emailIdx + 1] : null;
  const emailEquals = process.argv.find((a) => a.startsWith("--email="));
  const emailArg = emailEquals ? emailEquals.split("=")[1] : emailFlag;

  if (registerFlag && emailArg) {
    const result = await registerManual({ cloudUrl: config.cloudUrl, email: emailArg });
    console.error(`Registered: ${result.email} — token saved to config.`);
    return;
  }

  // Scan-code registration: gwiki --register (visible Chrome + CDP).
  if (registerFlag) {
    try {
      const result = await registerViaSso({
        cloudUrl: config.cloudUrl,
        onStatus: (message) => console.error(message),
      });
      console.error(`已注册: ${result.email}${result.name ? `（${result.name}）` : ""} — token 已保存`);
    } catch (error) {
      console.error(`扫码注册失败: ${error.message}`);
      console.error("可改用（需有效 GUAZISSO 环境变量）: gwiki --register --email you@guazi.com");
      process.exitCode = 1;
    }
    return;
  }

  // Auto SSO registration when no personal token is configured: silently
  // reuses the shared SSO cache, or opens a visible Chrome for a QR scan —
  // works the same in terminals and agent contexts.
  if (needsUserRegistration(config)) {
    try {
      const result = await registerViaSso({
        cloudUrl: config.cloudUrl,
        onStatus: (message) => console.error(message),
      });
      console.error(`已通过 SSO 注册: ${result.email}`);
      Object.assign(config, await loadConfig());
    } catch (error) {
      console.error(`自动注册未完成: ${error.message}`);
      printRegistrationRequired();
      process.exitCode = 2;
      return;
    }
  }

  const [rawCommand, ...args] = process.argv.slice(2);
  const command = COMMAND_ALIASES[rawCommand] || rawCommand;

  if (command === "list-kbs") {
    const body = await listKbs(config);
    printKnowledgeBaseList(body.knowledgeBases || []);
    return;
  }

  if (command === "query" || command === "query-plus") {
    await runQueryCommand({ config, command, args });
    return;
  }

  if (command === "review-requirement") {
    await runReviewCommand({ config, args });
    return;
  }

  if (command === "requirement-write") {
    await runWriteCommand({ config, args });
    return;
  }

  if (command === "ask") {
    await runAskCommand({ config, args });
    return;
  }

  if (command === "job") {
    const { outputMode, rest } = parseJobOptions(args);
    const jobId = rest[0];
    if (!jobId) {
      throw new Error("jobId is required");
    }
    await printJob(config, jobId, { outputMode });
    return;
  }

  if (command === "lock") {
    await runLockCommand({ config, args, mode: "append" });
    return;
  }

  if (command === "relock") {
    await runLockCommand({ config, args, mode: "replace" });
    return;
  }

  if (command === "unlock") {
    await clearLock();
    console.log("unlocked (disk default cleared)");
    return;
  }

  if (command === "lock-status") {
    await runLockStatusCommand({ config });
    return;
  }

  if (command === "auth-status") {
    if (needsUserRegistration(config)) {
      printRegistrationRequired();
      process.exitCode = 2;
    } else {
      console.log(`registered: ${config.userEmail || "(token only)"}`);
    }
    return;
  }

  throw new Error(`unknown command: ${rawCommand || "(empty)"}`);
}

async function runQueryCommand({ config, command, args }) {
  const { kbId, selectedKbs, selectAllKbs, outputMode, routeJobId, sessionId, rest } = parseJobOptions(args);
  const question = rest.join(" ").trim();
  if (!question) {
    throw new Error("question is required");
  }
  await submitAndPrintJob({
    config,
    payload: buildJobPayload({
      type: command === "query-plus" ? "query-plus" : "query",
      kbId,
      mode: config.defaultMode,
      sessionId,
      input: {
        question,
        ...(routeJobId ? { routeJobId } : {}),
      },
    }),
    selectedKbs,
    selectAllKbs,
    outputMode,
  });
}

async function runReviewCommand({ config, args }) {
  const { kbId, selectedKbs, selectAllKbs, outputMode, requirementUrl, routeJobId, sessionId, rest } = parseJobOptions(args);
  const reviewInput = await resolveReviewRequirementInput({
    requirementUrl,
    rest,
  });
  await submitAndPrintJob({
    config,
    payload: buildJobPayload({
      type: "review-requirement",
      kbId,
      mode: config.defaultMode,
      sessionId,
      input: {
        ...reviewInput,
        ...(routeJobId ? { routeJobId } : {}),
      },
    }),
    selectedKbs,
    selectAllKbs,
    outputMode,
  });
}

async function runWriteCommand({ config, args }) {
  const { kbId, selectedKbs, selectAllKbs, outputMode, routeJobId, sessionId, rest } = parseJobOptions(args);
  const question = rest.join(" ").trim();
  if (!question) {
    throw new Error("requirement topic or description is required");
  }
  await submitAndPrintJob({
    config,
    payload: buildJobPayload({
      // 复用 query job，Cloud 侧走知识库检索提供撰写上下文
      type: "query",
      kbId,
      mode: config.defaultMode,
      sessionId,
      input: {
        question,
        context: "requirement-write",
        ...(routeJobId ? { routeJobId } : {}),
      },
    }),
    selectedKbs,
    selectAllKbs,
    outputMode,
  });
}

async function runAskCommand({ config, args }) {
  const { requirementUrl, rest } = parseJobOptions(args);
  const source = rest.join(" ").trim();
  const isCwikiUrl = Boolean(requirementUrl) || /^https?:\/\/cwiki\./iu.test(source);
  const intent = detectGwikiIntent(source || requirementUrl, { isCwikiUrl });
  const routedCommand = commandForIntent(intent);
  console.log(`intent=${intent} command=${routedCommand}`);

  if (routedCommand === "review-requirement") {
    await runReviewCommand({ config, args });
    return;
  }

  if (routedCommand === "requirement-write") {
    await runWriteCommand({ config, args });
    return;
  }

  await runQueryCommand({
    config,
    command: cloudJobTypeForIntent(intent),
    args,
  });
}

async function runLockCommand({ config, args, mode }) {
  const positional = args.filter((a) => !a.startsWith("--"));
  if (positional.length === 0) {
    throw new Error("至少指定一个 KB 业务名或 kb_id");
  }

  // Split positional args into exact kb_ids (no cloud lookup) and business
  // names (need listKbs to fuzzy-resolve). This lets `gwiki lock kb_detect`
  // work offline when no fuzzy resolution is needed.
  const exactIds = [];
  const businessNames = [];
  for (const raw of positional) {
    const name = String(raw || "").trim();
    if (!name) continue;
    if (looksLikeKbId(name)) {
      exactIds.push(name);
    } else {
      businessNames.push(name);
    }
  }

  let ids = uniq(exactIds);
  let ambiguous = [];
  let unknown = [];

  if (businessNames.length > 0) {
    const { listKbs: listKbsFromClient } = await import("../lib/client.js");
    const resolved = await resolveKbIds(businessNames, {
      listKbs: async () => {
        const body = await listKbsFromClient(config);
        return body.knowledgeBases || [];
      },
    });
    ids = uniq([...ids, ...resolved.ids]);
    ambiguous = resolved.ambiguous;
    unknown = resolved.unknown;
  }

  if (ambiguous.length > 0) {
    const lines = ambiguous.map(
      ({ name, candidates }) =>
        `  "${name}" matches: ${candidates.map((c) => `${c.id}(${c.display_name || c.id})`).join(", ")}`,
    );
    throw new Error(`业务名不明确:\n${lines.join("\n")}`);
  }

  if (unknown.length > 0) {
    const { listKbs: listKbsFromClient } = await import("../lib/client.js");
    const body = await listKbsFromClient(config);
    const enabled = (body.knowledgeBases || []).filter((kb) => kb.enabled !== false);
    const sample = enabled
      .slice(0, 20)
      .map((kb) => `  - ${kb.id} (${kb.display_name || kb.id})`)
      .join("\n");
    throw new Error(
      `未找到 KB: ${unknown.join(", ")}\n可用 KB（前 20 个）:\n${sample}`,
    );
  }

  if (ids.length === 0) {
    throw new Error("至少指定一个 KB 业务名或 kb_id");
  }

  if (mode === "replace") {
    await replaceLock(ids);
    console.log(`relocked: ${ids.join(",")}`);
  } else {
    await writeLock(ids);
    console.log(`locked: ${ids.join(",")}`);
  }
}

function uniq(arr) {
  return Array.from(new Set(arr));
}

async function runLockStatusCommand({ config: _config }) {
  const { defaultKbs, exists, parseError, backupPath } = await readLockWithBackup();
  if (parseError) {
    console.log(`lock.yaml 解析失败: ${parseError}. 已备份到 ${backupPath}`);
    return;
  }
  if (!exists || defaultKbs.length === 0) {
    console.log("(no disk default)");
    return;
  }
  console.log(`disk default_kbs: ${defaultKbs.join(",")}`);
  console.log(`auto-load: enabled (will be injected into main path when no --select-kbs is passed)`);
}

function printKnowledgeBaseList(knowledgeBases) {
  const enabled = knowledgeBases.filter((kb) => kb.enabled !== false);
  console.log(`当前 Cloud 注册的知识库：共 ${enabled.length} 个 enabled 知识库`);
  console.log("");
  console.log(["kb_id", "display_name", "描述", "来源数", "主要域"].join("\t"));
  for (const kb of enabled) {
    const profile = knowledgeBaseProfile(kb);
    console.log([
      kb.id,
      kb.display_name || kb.displayName || kb.id,
      profile.description,
      sourceCount(kb),
      profile.domains.join("、"),
    ].join("\t"));
  }
}

function knowledgeBaseProfile(kb) {
  const terms = (kb.metadata?.routingIndex?.routing?.terms || [])
    .filter((term) => isUsefulDomainTerm(term, kb))
    .slice(0, 5);
  return {
    description: cleanSummary(kb.metadata?.routingIndex?.summary || kb.description || kb.id),
    domains: terms.length > 0 ? terms : ["待补充"],
  };
}

function sourceCount(kb) {
  return Number(kb.metadata?.routingIndex?.stats?.sourceCount || 0);
}

function isUsefulDomainTerm(term, kb) {
  const text = String(term || "").trim();
  if (!text) return false;
  const lower = text.toLowerCase();
  if (lower === String(kb.id).toLowerCase()) return false;
  if (lower.includes("knowledge base")) return false;
  if (lower.includes("llm-wiki")) return false;
  return true;
}

function cleanSummary(value) {
  return String(value || "")
    .replace(/^>\s*/, "")
    .replace(/\[\[([^|\]]+\|)?([^\]]+)\]\]/g, "$2")
    .replace(/`/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function parseJobOptions(args) {
  const rest = [...args];
  const positional = [];
  let kbId = "auto";
  let selectedKbs = [];
  let selectAllKbs = false;
  let outputMode = "summary";
  let requirementUrl = "";
  let routeJobId = "";
  let sessionId = resolveSessionId();
  while (rest.length > 0) {
    if (rest[0] === "--kb") {
      rest.shift();
      kbId = rest.shift() || "auto";
      continue;
    }
    if (rest[0] === "--url") {
      rest.shift();
      requirementUrl = rest.shift() || "";
      continue;
    }
    if (rest[0] === "--select-kbs") {
      rest.shift();
      selectedKbs = parseSelectedKbIds(rest.shift() || "");
      continue;
    }
    if (rest[0] === "--route-job-id") {
      rest.shift();
      routeJobId = String(rest.shift() || "").trim();
      continue;
    }
    if (rest[0] === "--session-id") {
      rest.shift();
      sessionId = String(rest.shift() || "").trim() || resolveSessionId();
      continue;
    }
    if (rest[0] === "--all-kbs") {
      rest.shift();
      selectAllKbs = true;
      continue;
    }
    if (rest[0] === "--output") {
      rest.shift();
      outputMode = normalizeOutputMode(rest.shift() || "summary");
      continue;
    }
    positional.push(rest.shift());
  }
  return {
    kbId,
    selectedKbs,
    selectAllKbs,
    outputMode,
    requirementUrl,
    routeJobId,
    sessionId,
    rest: positional,
  };
}

function buildJobPayload({ type, kbId, mode, sessionId, input }) {
  return {
    type,
    kbId,
    mode,
    ...(sessionId ? { sessionId } : {}),
    input,
  };
}

function resolveSessionId() {
  const explicit = String(process.env.GWIKI_SESSION_ID || process.env.LLM_WIKI_CLOUD_SESSION_ID || "").trim();
  if (explicit) {
    return explicit;
  }
  return "";
}

async function resolveReviewRequirementInput({ requirementUrl, rest }) {
  if (requirementUrl) {
    return buildReviewRequirementJobInput({ requirementUrl });
  }

  const source = rest.join(" ").trim();
  if (!source) {
    throw new Error("requirement text, Cwiki URL, file path, or --url is required");
  }

  if (await pathExists(source)) {
    const content = (await readFile(source, "utf8")).trim();
    return buildReviewRequirementJobInput({ requirement: content });
  }

  return buildReviewRequirementJobInput({ requirement: source });
}

async function pathExists(filePath) {
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
}

// Version check helpers
const LAST_CHECK_FILE = path.join(os.homedir(), ".gwiki-last-check");

function getTodayString() {
  return new Date().toISOString().split("T")[0]; // YYYY-MM-DD
}

async function getLastCheckDate() {
  try {
    const content = await readFile(LAST_CHECK_FILE, "utf-8");
    return content.trim();
  } catch {
    return null;
  }
}

async function setLastCheckDate(dateString) {
  await writeFile(LAST_CHECK_FILE, dateString, "utf-8");
}

async function checkUpgradeHint(upgradeHint) {
  if (!upgradeHint) {
    return; // No hint from server
  }

  const today = getTodayString();
  const lastCheck = await getLastCheckDate();
  
  if (lastCheck === today) {
    return; // Already checked today
  }

  // Record today's check
  await setLastCheckDate(today);

  // Print upgrade hint to stderr
  const { latestVersion, clientVersion, reason } = upgradeHint;
  if (reason === "no-version-header") {
    console.error(
      `\n⚠ gwiki 有新版本 ${latestVersion}（当前版本未知），建议运行 gwiki-update-skill 升级`
    );
  } else if (reason === "outdated" && clientVersion) {
    console.error(
      `\n⚠ gwiki 有新版本 ${latestVersion}（当前 ${clientVersion}），建议运行 gwiki-update-skill 升级`
    );
  }
}

async function printJob(config, jobId, { outputMode = "summary" } = {}) {
  const streamed = await streamJobEvents({
    ...config,
    jobId,
    onEvent: (event) => {
      if (event.selectedKbs) {
        console.log(`routing selected=${event.selectedKbs.join(",")} confidence=${event.confidence ?? ""}`);
      } else if (event.type === "child_failed") {
        console.error(`[partial-failure] KB ${event.kbId || "unknown"}: ${event.error || "unknown error"}`);
      } else {
        console.log(event.type);
      }
    },
  });

  const body = streamed
    ? await getJobResult({ ...config, jobId }).catch(() => null)
    : await pollJobResult(config, jobId);
  if (!body) {
    console.log(`completed ${jobId}`);
    return;
  }
  if (body.status === "completed") {
    console.log(`completed ${jobId}`);
    if (body.result?.outputText) {
      await printResultOutput({ jobId, body, outputMode });
    }
    const failures = body.result?.partialFailures;
    if (Array.isArray(failures) && failures.length > 0) {
      console.error("");
      console.error(`⚠ ${failures.length} 个知识库查询失败（结果仅包含成功的知识库）：`);
      for (const f of failures) {
        console.error(`  - ${f.kbId}: ${f.error}`);
      }
    }
    
    // Check for upgrade hint (daily first use)
    await checkUpgradeHint(body.upgradeHint);
    
    return body;
  }
  console.log(`${body.status} ${jobId}`);
  return body;
}

async function submitAndPrintJob({ config, payload, selectedKbs = [], selectAllKbs = false, outputMode = "summary" }) {
  const isReview = payload.type === "review-requirement";
  const hasPreconfirmed = selectedKbs.length > 0;

  // Auto-load disk default when caller didn't pass selectedKbs
  let effectiveSelectedKbs = selectedKbs;
  if (!hasPreconfirmed) {
    const { defaultKbs, parseError, backupPath } = await readLockWithBackup();
    if (parseError) {
      console.error(`lock.yaml 解析失败: ${parseError}. 已备份到 ${backupPath}`);
    }
    if (defaultKbs.length > 0) {
      effectiveSelectedKbs = defaultKbs;
      console.log(`[active-default-kb] ${defaultKbs.join(",")}`);
    }
  }

  const initialPayload = effectiveSelectedKbs.length > 0
    ? {
        ...payload,
        input: {
          ...(payload.input || {}),
          selectedKbs: effectiveSelectedKbs,
          ...(isReview ? { phase: "execute" } : {}),
        },
      }
    : payload;

  const created = await submitJob({ ...config, payload: initialPayload });
  console.log(`submitted ${created.jobId} status=${created.status}`);
  const body = await printJob(config, created.jobId, { outputMode });
  const clarification = body?.status === "completed" && body.result?.needClarification
    ? body.result
    : null;
  if (!clarification) {
    if (!hasPreconfirmed && body?.result?.routing?.selectedKbs?.length > 0) {
      const kbNames = body.result.routing.selectedKbs.join(", ");
      console.log(`[auto-routed-kb] ${kbNames}`);
    }
    return body;
  }

  if (isReview) {
    console.log("等待 KB 确认，未启动评审");
  }

  const confirmedKbs = resolveConfirmedKnowledgeBases({
    clarification,
    selectedKbs: effectiveSelectedKbs,
    selectAllKbs,
  }) || await askForKnowledgeBases(clarification, created.jobId);
  if (confirmedKbs.length === 0) {
    throw new Error("no knowledge bases selected");
  }

  const confirmed = await submitJob({
    ...config,
    payload: {
      ...payload,
      input: {
        ...(payload.input || {}),
        selectedKbs: confirmedKbs,
        routeJobId: created.jobId,
        ...(isReview ? { phase: "execute" } : {}),
        ...(isReview && clarification.recommendation?.primaryKbId
          ? { primaryKbId: clarification.recommendation.primaryKbId }
          : {}),
      },
    },
  });
  console.log(`submitted ${confirmed.jobId} status=${confirmed.status}`);
  return printJob(config, confirmed.jobId, { outputMode });
}

async function printResultOutput({ jobId, body, outputMode }) {
  const routing = body.result?.routing;
  const kbContext = buildKbContextFromRouting(routing);
  const formatted = formatQueryAnswer({
    markdown: body.result?.outputText || "",
    ...kbContext,
  });

  if (outputMode === "full") {
    console.log(formatted);
    return;
  }
  if (outputMode === "raw-json") {
    console.log(JSON.stringify({ ...body, result: { ...body.result, outputText: formatted } }, null, 2));
    return;
  }

  const savedPath = await saveFullResult({ jobId, outputText: formatted });
  if (routing?.selectedKbs?.length > 0) {
    console.log(`selected KBs: ${routing.selectedKbs.join(",")}`);
  }
  const summary = summarizeMarkdownForDisplay(formatted);
  if (summary) {
    console.log(summary);
  }
  console.log(`full result saved: ${savedPath}`);
}

async function saveFullResult({ jobId, outputText }) {
  const directory = path.join(os.tmpdir(), "gwiki");
  await mkdir(directory, { recursive: true });
  const filePath = path.join(directory, `${jobId}.md`);
  await writeFile(filePath, outputText, "utf8");
  return filePath;
}

function buildKbContextFromRouting(routing) {
  const selected = routing?.selectedKbs;
  if (!Array.isArray(selected) || selected.length === 0) {
    return {};
  }
  const enriched = selected.map((kbId) => ({
    id: kbId,
    displayName: kbId,
    description: "",
  }));
  return enriched.length === 1 ? { kb: enriched[0] } : { kbs: enriched };
}

function resolveConfirmedKnowledgeBases({ clarification, selectedKbs, selectAllKbs }) {
  if (selectedKbs.length > 0) {
    return selectedKbs;
  }
  if (!selectAllKbs) {
    return null;
  }
  return (clarification.options || [])
    .map((option) => option.kbId)
    .filter(Boolean);
}

async function askForKnowledgeBases(clarification, routeJobId = "") {
  const options = clarification.options || [];
  if (options.length === 0) {
    throw new Error(clarification.reason || "Cloud requested clarification but did not return KB options");
  }
  printKnowledgeBaseClarification(clarification, routeJobId);
  if (!canPromptForKnowledgeBases()) {
    throw new Error("KB clarification required: ask the user for a Chinese knowledge base name or abbreviation, then retry with --select-kbs or --route-job-id");
  }

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  try {
    const answer = await rl.question("终端可直接输入编号，可用逗号多选：");
    return parseSelectedKnowledgeBases(answer, options);
  } finally {
    rl.close();
  }
}

function canPromptForKnowledgeBases() {
  if (process.stdin.isTTY) {
    return true;
  }
  return process.env.GWIKI_ALLOW_STDIN_KB_SELECT === "1";
}

function printKnowledgeBaseClarification(clarification, routeJobId = "") {
  const options = clarification.options || [];
  console.log("需要选择知识库：");
  const reasonLine = formatClarificationReason(clarification.reason);
  if (reasonLine) {
    console.log(reasonLine);
  }
  console.log("请回复中文知识库名称或常见缩写（如「检测」「采销」「网销」），可多选；不要回复数字编号。");
  const recommendation = resolveClarificationRecommendation(clarification, options);
  options.forEach((option, index) => {
    const displayName = option.displayName || option.label || option.kbId;
    const reasonHint = formatOptionReason(option.reason);
    const recommended = recommendation?.kbId === option.kbId ? "（推荐）" : "";
    console.log(`[${index + 1}] ${displayName}${recommended} ${reasonHint}`.trim());
  });
  if (recommendation) {
    console.log(`[recommended-kb] ${recommendation.displayName || recommendation.kbId}`);
    if (clarification.recommendation?.primaryKbId) {
      const detail = recommendation.reason ? ` (${recommendation.reason})` : "";
      console.log(`建议主 KB: ${recommendation.displayName || recommendation.kbId}${detail}`.trim());
    }
  }
  if (routeJobId) {
    console.log(`[route-job-id] ${routeJobId}`);
  }
  console.log("[kb-clarification-required]");
}

function resolveClarificationRecommendation(clarification, options = []) {
  const primaryKbId = clarification.recommendation?.primaryKbId;
  if (primaryKbId) {
    const matched = options.find((option) => option.kbId === primaryKbId);
    return {
      kbId: primaryKbId,
      displayName: matched?.displayName || matched?.label || primaryKbId,
      reason: clarification.recommendation?.reason || "",
    };
  }
  const top = options[0];
  if (!top?.kbId || Number(top.score) <= 0) {
    return null;
  }
  return {
    kbId: top.kbId,
    displayName: top.displayName || top.label || top.kbId,
    reason: top.reason || "",
  };
}

function formatClarificationReason(reason = "") {
  const normalized = String(reason || "").trim();
  if (!normalized) {
    return "";
  }
  const translations = [
    ["Multiple explicit KB aliases matched", "问题里提到了多个知识库名称，需确认使用哪一个"],
    ["Question mentions one KB explicitly but also strongly matches other KBs", "问题明确提到了一个知识库，但也强烈匹配其他知识库"],
    ["Requirement review did not explicitly name a KB", "需求评审未明确指定知识库"],
    ["Broad question did not explicitly request all KBs or name a KB", "问题范围较广，未指定具体知识库"],
    ["Question is ambiguous across available KBs", "问题在多个知识库之间不够明确"],
    ["Explicit KB constraint was not recognized", "指定的知识库名称未能识别"],
    ["No enabled knowledge bases support this capability", "当前没有支持该能力的知识库"],
  ];
  for (const [prefix, translated] of translations) {
    if (normalized.startsWith(prefix)) {
      return translated;
    }
  }
  return normalized;
}

function formatOptionReason(reason = "") {
  const normalized = String(reason || "").trim();
  if (!normalized) {
    return "";
  }
  const replacements = [
    [/Matched alias/gi, "名称匹配"],
    [/Explicit alias match/gi, "明确名称匹配"],
    [/Matched concept/gi, "概念匹配"],
    [/Matched domain phrase/gi, "业务域词匹配"],
    [/routing hint/gi, "文档标题/摘要匹配"],
    [/UI analysis/gi, "需求内容分析"],
    [/comparison-case/gi, "对比案例提及"],
    [/domain fragment/gi, "部分词面重叠"],
    [/term overlap/gi, "术语重叠"],
    [/Highest routing score for/gi, "路由得分最高"],
  ];
  let formatted = normalized;
  for (const [pattern, replacement] of replacements) {
    formatted = formatted.replace(pattern, replacement);
  }
  return formatted;
}

function parseSelectedKnowledgeBases(answer, options) {
  const selected = [];
  const seen = new Set();
  for (const part of String(answer || "").split(/[,\s，、]+/u)) {
    if (!part) continue;
    const index = Number.parseInt(part, 10) - 1;
    const option = options[index];
    if (!option?.kbId || seen.has(option.kbId)) continue;
    seen.add(option.kbId);
    selected.push(option.kbId);
  }
  return selected;
}

function parseSelectedKbIds(value) {
  return String(value || "")
    .split(/[,\s，、]+/u)
    .map((part) => part.trim())
    .filter(Boolean);
}

function normalizeOutputMode(value) {
  const mode = String(value || "").trim();
  if (["summary", "full", "raw-json"].includes(mode)) {
    return mode;
  }
  throw new Error(`unsupported output mode: ${mode || "(empty)"}`);
}

async function pollJobResult(config, jobId) {
  const timeoutMs = Number(config.timeoutMs ?? 10 * 60 * 1000);
  const pollIntervalMs = Number(config.pollIntervalMs ?? 1000);
  const startedAt = Date.now();

  while (Date.now() - startedAt <= timeoutMs) {
    const body = await getJobResult({ ...config, jobId });
    if (["completed", "failed", "stale", "cancelled"].includes(body.status)) {
      return body;
    }
    console.log(`${body.status} ${jobId}`);
    await delay(pollIntervalMs);
  }

  throw new Error(`job ${jobId} did not finish before timeout`);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
