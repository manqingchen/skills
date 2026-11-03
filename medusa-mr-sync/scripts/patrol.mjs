#!/usr/bin/env node
/**
 * 批量巡查：guazi-flow inbox 里的所有项目 → 最近一次发布的分支是否合入主干；
 * 没合就建 <分支> → <主干> 的 MR 并合并。
 *
 * 发布来源分两类：
 *   1. 项目本身是美杜莎应用（gitlabPid 能对上）→ 用 `app.deployHistory` 记录；
 *      done 的继续检查合并，running / cancel 只上报（不建 MR、不合并）；
 *   2. 不是美杜莎应用 → 用仓库里最近 `--since-hours` 内打的 `*-online` tag。
 *
 * 不需要本地仓库：分支包含关系、提交列表、MR 全部走 GitLab REST，
 * 因此在 agent runtime（没有 glab、没有 checkout）里也能跑。
 *
 * 用法：
 *   node patrol.mjs --dry-run                 # 只读：列出所有项目里没合的分支
 *   node patrol.mjs                           # 没合就建 MR 并合并
 *   node patrol.mjs --project global-station  # 只巡查指定项目
 *   node patrol.mjs --no-merge                # 只建 MR，不合并
 *
 * 进度写 stderr，结果写一行 JSON 到 stdout。
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  releaseState,
  sortByCreatedAtDesc,
  sortMrsById,
  decideOpenedMrs,
  classifyDeployStage,
  stageLabel,
  analyzeMergeCommits,
  mergeRuleMessage,
  isIntegrationBranch,
  notApplicableStage,
  stageBucket,
} from './rules.mjs';
import { runGates } from './pipeline.mjs';
import { createMulticaGitlab } from './multica-mr.mjs';

const DEFAULTS = {
  workspace: 'guazi',
  flowUrl: 'https://guazi-flow.guazi-apps.com',
  baseUrl: 'https://medusa-new.guazi-apps.com',
  env: 'online',
  sinceHours: 72,
  medusaHistory: 50,
  target: '',
  dryRun: false,
  merge: true,
  waitPipeline: false,
  maxMerges: 20,
  pollMs: 3000,
  timeoutMinutes: 10,
  concurrency: 4,
  projects: [],
  include: '',
  exclude: '',
  enableWrites: false,
  // 默认用 multica gitlab mr 建/合并；--mr-backend gitlab 可退回旧的 REST 通道
  mrBackend: 'multica',
  asUser: '',
  noAsUser: false,
};

const JIRA_BASE_URL = process.env.JIRA_BASE_URL || 'https://cjira.guazi-corp.com/browse';
const GITLAB_HOST = (process.env.GITLAB_HOST || 'https://git.guazi-corp.com').replace(/\/+$/, '');

const log = (message) => process.stderr.write(`${message}\n`);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class AuthError extends Error {}
class SkillError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;
  }
}

function parseArgs(argv) {
  const args = { ...DEFAULTS };
  const valueFlags = {
    '--workspace': 'workspace',
    '--flow-url': 'flowUrl',
    '--base-url': 'baseUrl',
    '--env': 'env',
    '--since-hours': 'sinceHours',
    '--medusa-history': 'medusaHistory',
    '--target': 'target',
    '--max-merges': 'maxMerges',
    '--poll-ms': 'pollMs',
    '--timeout-minutes': 'timeoutMinutes',
    '--concurrency': 'concurrency',
    '--include': 'include',
    '--exclude': 'exclude',
    '--as-user': 'asUser',
    '--mr-backend': 'mrBackend',
  };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (['--dry-run', '--check', '--status', '--check-only'].includes(key)) args.dryRun = true;
    else if (key === '--enable-writes') args.enableWrites = true;
    else if (key === '--no-merge') args.merge = false;
    else if (key === '--merge') args.merge = true;
    else if (key === '--no-as-user') args.noAsUser = true;
    else if (key === '--wait-pipeline') args.waitPipeline = true;
    else if (key === '--patrol' || key === '--all') {
      /* 入口兼容：sync-mr.mjs --patrol 转发过来 */
    } else if (key === '--project') {
      if (argv[i + 1] === undefined) throw new Error('--project 缺少取值');
      args.projects.push(argv[i + 1]);
      i += 1;
    } else if (valueFlags[key]) {
      if (argv[i + 1] === undefined) throw new Error(`${key} 缺少取值`);
      args[valueFlags[key]] = argv[i + 1];
      i += 1;
    } else if (key.startsWith('-')) throw new Error(`未知参数：${key}`);
    else args.projects.push(key);
  }
  ['sinceHours', 'medusaHistory', 'maxMerges', 'pollMs', 'timeoutMinutes', 'concurrency'].forEach((key) => {
    args[key] = Number(args[key]);
    if (!Number.isFinite(args[key]) || args[key] < 0) throw new Error(`参数 ${key} 不是合法数字`);
  });
  if (!['multica', 'gitlab'].includes(args.mrBackend)) {
    throw new Error('--mr-backend 只能是 multica 或 gitlab');
  }
  if (args.noAsUser && args.asUser) throw new Error('--as-user 与 --no-as-user 不能同时使用');
  if (args.mrBackend === 'gitlab' && args.asUser) throw new Error('--as-user 只适用于 --mr-backend multica');
  // 运行环境的保险丝：不想让定时任务真的合并时，设 PATROL_DRY_RUN=1 / PATROL_NO_MERGE=1
  if (['1', 'true', 'yes'].includes(String(process.env.PATROL_DRY_RUN || '').toLowerCase())) args.dryRun = true;
  if (['1', 'true', 'yes'].includes(String(process.env.PATROL_NO_MERGE || '').toLowerCase())) args.merge = false;
  if (['1', 'true', 'yes'].includes(String(process.env.PATROL_ENABLE_WRITES || '').toLowerCase())) {
    args.enableWrites = true;
  }
  // 批量巡查默认只读：不显式解锁就绝不建 MR / 合并，避免定时任务被误触发
  if (!args.enableWrites) {
    args.dryRun = true;
    args.merge = false;
  }
  return args;
}

/* ---------------------------------- 凭据 ---------------------------------- */

/** 美杜莎 / guazi-flow 都认 GUAZISSO：优先环境变量，其次 guazi-sso-login 的缓存账号 */
function resolveGuaziCookie() {
  if (process.env.MEDUSA_COOKIE) return process.env.MEDUSA_COOKIE;
  if (process.env.GUAZISSO) return `GUAZISSO=${process.env.GUAZISSO.replace(/^GUAZISSO=/, '')}`;
  const candidates = [
    path.join(os.homedir(), '.zcode/skills/guazi-sso-login/scripts/guazi_sso_login.py'),
    path.join(os.homedir(), '.agents/skills/guazi-sso-login/scripts/guazi_sso_login.py'),
    path.join(os.homedir(), '.claude/skills/guazi-sso-login/scripts/guazi_sso_login.py'),
  ];
  const script = candidates.find((file) => fs.existsSync(file));
  if (!script) {
    throw new AuthError('没找到 guazi-sso-login skill，或设置 MEDUSA_COOKIE="GUAZISSO=xxx"');
  }
  let out;
  try {
    out = execFileSync('/usr/bin/python3', [script, 'sso', '--validate', '--plain'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 120000,
    });
  } catch (error) {
    throw new AuthError(`换取 GUAZISSO 失败：${String(error.stderr || error.message).slice(0, 160)}`);
  }
  const match = out.match(/GUAZISSO=[^\s;]+/);
  if (!match) throw new AuthError('SSO 返回里没有 GUAZISSO');
  return match[0];
}

/** 用 GUAZISSO 换 guazi-flow 的会话 cookie（multica_auth）；也支持直接给 FLOW_COOKIE */
async function resolveFlowCookie(args, guaziCookie) {
  if (process.env.FLOW_COOKIE) return process.env.FLOW_COOKIE;
  let response;
  try {
    response = await fetch(`${args.flowUrl}/auth/sso/identity`, {
      method: 'POST',
      headers: { Cookie: guaziCookie, 'Content-Type': 'application/json' },
      body: '{}',
    });
  } catch (error) {
    throw new SkillError(`连不上 guazi-flow：${error.message}`, 'FLOW_FAILED');
  }
  const text = await response.text();
  if (response.status === 401 || response.status === 403) {
    throw new AuthError(`guazi-flow 拒绝 GUAZISSO（${response.status}）：${text.slice(0, 120)}`);
  }
  if (!response.ok) throw new SkillError(`guazi-flow SSO 交换失败 ${response.status}：${text.slice(0, 160)}`, 'FLOW_FAILED');
  const cookies = typeof response.headers.getSetCookie === 'function' ? response.headers.getSetCookie() : [];
  const jar = cookies.map((item) => item.split(';')[0]).join('; ');
  if (!/multica_auth=/.test(jar)) {
    throw new AuthError('guazi-flow SSO 没有返回 multica_auth，检查 GUAZISSO 是否过期');
  }
  return jar;
}

/** GitLab token：环境变量 → ~/.git-credentials → glab 配置 */
function resolveGitlabToken() {
  for (const key of ['GITLAB_TOKEN', 'PRIVATE_TOKEN', 'GLAB_TOKEN']) {
    if (process.env[key]) return process.env[key];
  }
  const credFile = path.join(os.homedir(), '.git-credentials');
  if (fs.existsSync(credFile)) {
    const host = new URL(GITLAB_HOST).host;
    for (const line of fs.readFileSync(credFile, 'utf8').split('\n')) {
      const match = line.match(/^https?:\/\/([^:]+):([^@]+)@(.+)$/);
      if (match && match[3].replace(/\/+$/, '') === host) {
        return decodeURIComponent(match[2]);
      }
    }
  }
  try {
    const token = execFileSync('glab', ['config', 'get', 'token', '--host', new URL(GITLAB_HOST).host], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    if (token) return token;
  } catch {
    /* 没有 glab 就算了，后面报明确错误 */
  }
  return '';
}

/* --------------------------------- GitLab --------------------------------- */

function createGitlab(token) {
  const base = `${GITLAB_HOST}/api/v4`;
  async function request(apiPath, { method = 'GET', body } = {}) {
    const url = `${base}${apiPath}`;
    let response;
    try {
      response = await fetch(url, {
        method,
        headers: {
          'PRIVATE-TOKEN': token,
          ...(body ? { 'Content-Type': 'application/json' } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
    } catch (error) {
      throw new SkillError(`连不上 GitLab（${method} ${apiPath.split('?')[0]}）：${error.message}`, 'GITLAB_FAILED');
    }
    if (response.status === 404) return null;
    const text = await response.text();
    if (!response.ok) {
      let detail = text;
      try {
        const payload = JSON.parse(text);
        detail = payload.message || payload.error || text;
      } catch {
        /* 保持原文 */
      }
      throw new SkillError(
        `GitLab 接口失败（${method} ${apiPath.split('?')[0]}，${response.status}）：${String(detail).replace(/\s+/g, ' ').slice(0, 300)}`,
        'GITLAB_FAILED',
      );
    }
    return text.trim() ? JSON.parse(text) : null;
  }

  const projectPath = (repo) => encodeURIComponent(repo.replace(/^\/+|\/+$/g, ''));

  return {
    request,
    project: (repo) => request(`/projects/${projectPath(repo)}`),
    branch: (repo, name) =>
      request(`/projects/${projectPath(repo)}/repository/branches/${encodeURIComponent(name)}`),
    tags: (repo, { page = 1, perPage = 100 } = {}) =>
      request(`/projects/${projectPath(repo)}/repository/tags?per_page=${perPage}&page=${page}`),
    branchesWithCommit: (repo, sha) =>
      request(`/projects/${projectPath(repo)}/repository/commits/${sha}/refs?type=branch`),
    compare: (repo, from, to) =>
      request(
        `/projects/${projectPath(repo)}/repository/compare?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
      ),
    findMRs: (repo, source, target) =>
      request(
        `/projects/${projectPath(repo)}/merge_requests?source_branch=${encodeURIComponent(source)}&target_branch=${encodeURIComponent(target)}&state=all&per_page=20`,
      ),
    findOpenedMRs: (repo, source) =>
      request(
        `/projects/${projectPath(repo)}/merge_requests?source_branch=${encodeURIComponent(source)}&state=opened&per_page=20`,
      ),
    getMR: (repo, iid) => request(`/projects/${projectPath(repo)}/merge_requests/${iid}`),
    createMR: (repo, fields) =>
      request(`/projects/${projectPath(repo)}/merge_requests`, { method: 'POST', body: fields }),
    mergeMR: (repo, iid, fields) =>
      request(`/projects/${projectPath(repo)}/merge_requests/${iid}/merge`, { method: 'PUT', body: fields }),
  };
}

/* --------------------------------- guazi-flow --------------------------------- */

async function fetchInboxProjects(args, flowCookie) {
  const url = `${args.flowUrl}/api/projects/repositories?workspace_slug=${encodeURIComponent(args.workspace)}`;
  let response;
  try {
    response = await fetch(url, { headers: { Cookie: flowCookie } });
  } catch (error) {
    throw new SkillError(`连不上 guazi-flow 项目列表：${error.message}`, 'FLOW_FAILED');
  }
  const text = await response.text();
  if (response.status === 401) throw new AuthError('guazi-flow 会话无效（multica_auth）');
  if (!response.ok) throw new SkillError(`guazi-flow 项目列表 ${response.status}：${text.slice(0, 160)}`, 'FLOW_FAILED');
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new SkillError('guazi-flow 项目列表不是 JSON', 'FLOW_FAILED');
  }
  return (payload.repositories || []).filter((item) => item.is_available !== false);
}

/* --------------------------------- Medusa --------------------------------- */

const APPS_QUERY = `query($page: Int!, $itemsPerPage: Int!) {
  apps(page: $page, itemsPerPage: $itemsPerPage) { id name namespace gitlabPid }
}`;

const APPS_PROBE_QUERY = `query($page: Int!, $itemsPerPage: Int!, $namespace: String!, $appName: String!) {
  apps(page: $page, itemsPerPage: $itemsPerPage, namespace: $namespace, appName: $appName) { id name namespace gitlabPid }
}`;

const HISTORY_QUERY = `query($id: String!, $itemsPerPage: Int!) {
  app(id: $id) {
    deployHistory(page: 1, itemsPerPage: $itemsPerPage) {
      id status jiraID operator createdAt
      gitlabCommit { id branch }
    }
  }
}`;

async function medusaGql(args, cookie, query, variables) {
  let response;
  try {
    response = await fetch(`${args.baseUrl.replace(/\/+$/, '')}/graphql?env=${encodeURIComponent(args.env)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Cookie: cookie },
      body: JSON.stringify({ query, variables }),
    });
  } catch (error) {
    throw new SkillError(`连不上美杜莎：${error.message}`, 'MEDUSA_FAILED');
  }
  const text = await response.text();
  if (response.status === 401 || /token is empty/i.test(text)) throw new AuthError('GUAZISSO 无效或已过期');
  if (!response.ok) throw new SkillError(`美杜莎返回 ${response.status}：${text.slice(0, 160)}`, 'MEDUSA_FAILED');
  const payload = JSON.parse(text);
  if (Array.isArray(payload.errors) && payload.errors.length) {
    throw new SkillError(payload.errors[0].message || '美杜莎 GraphQL 报错', 'MEDUSA_FAILED');
  }
  return payload.data || {};
}

/** 把一页 apps 行合并进 map（按 gitlabPid 小写去重，先到先得） */
export function mergeAppRows(map, rows) {
  for (const app of rows || []) {
    if (!app || !app.gitlabPid) continue;
    const key = String(app.gitlabPid).toLowerCase();
    if (map.has(key)) continue;
    map.set(key, {
      appId: `${app.namespace}/${app.name}`,
      gitlabPid: app.gitlabPid,
    });
  }
  return map;
}

/**
 * 翻页取全量美杜莎应用。服务端单页硬上限 500，全量可能超过一页（2026-11-03 实测 1011 行 / 去重 958 个），
 * 只取第一页会把排在后面的 namespace（如 oversea 整组 29 个应用）整个丢掉，导致这些仓库被误判为非美杜莎项目。
 * 防死循环：某整页没有带来任何新 key（服务端忽略 page 参数、重复返回同一页）时停止。
 */
export async function collectMedusaApps(fetchPage, { itemsPerPage = 500, maxPages = 50 } = {}) {
  const map = new Map();
  for (let page = 1; page <= maxPages; page += 1) {
    const rows = (await fetchPage(page, itemsPerPage)) || [];
    const before = map.size;
    mergeAppRows(map, rows);
    if (rows.length < itemsPerPage) break;
    if (map.size === before) break;
  }
  return map;
}

async function fetchMedusaApps(args, cookie) {
  return collectMedusaApps((page, itemsPerPage) =>
    medusaGql(args, cookie, APPS_QUERY, { page, itemsPerPage }).then((data) => data.apps || []),
  );
}

/**
 * 定向兜底：apps(namespace, appName) 是服务端模糊过滤（appName 为包含匹配），
 * 客户端再对 gitlabPid 做大小写不敏感的精确匹配。用于列表视图缺组时逐项目确认。
 */
export function matchAppByRepoPath(rows, repositoryPath) {
  const want = String(repositoryPath || '').toLowerCase();
  for (const app of rows || []) {
    if (app && app.gitlabPid && String(app.gitlabPid).toLowerCase() === want) {
      return {
        appId: `${app.namespace}/${app.name}`,
        gitlabPid: app.gitlabPid,
        probed: true,
      };
    }
  }
  return null;
}

async function probeMedusaApp(args, cookie, repositoryPath) {
  const parts = String(repositoryPath || '').split('/');
  const namespace = parts.shift();
  const appName = parts.join('/');
  if (!namespace || !appName) return null;
  try {
    const data = await medusaGql(args, cookie, APPS_PROBE_QUERY, {
      page: 1,
      itemsPerPage: 100,
      namespace,
      appName,
    });
    return matchAppByRepoPath(data.apps || [], repositoryPath);
  } catch {
    return null; // 兜底失败不阻断，维持 tags 口径
  }
}

async function fetchDeployHistory(args, cookie, appId) {
  const data = await medusaGql(args, cookie, HISTORY_QUERY, { id: appId, itemsPerPage: args.medusaHistory });
  const rows = (data.app && data.app.deployHistory) || [];
  return Array.isArray(rows) ? rows : [];
}

const EVENTS_QUERY = `query($taskID: Int!) {
  queryDeployEvent(taskID: $taskID) { reason createdAt creator }
}`;

/**
 * 发布段判定（强制步骤）。规则在 rules.mjs，与 sync-mr.mjs 共用同一实现。
 */
async function fetchDeployStage(args, cookie, appId, taskId) {
  if (!taskId) return notApplicableStage();
  const data = await medusaGql(args, cookie, EVENTS_QUERY, { taskID: Number(taskId) });
  const events = Array.isArray(data.queryDeployEvent) ? data.queryDeployEvent : [];
  return classifyDeployStage({ events, appName: String(appId).split('/').pop(), taskId });
}

/* --------------------------------- 工具 --------------------------------- */

function extractJira(text) {
  const match = String(text || '').match(/\b[A-Z]{2,5}-\d{2,6}\b/);
  return match ? match[0] : '';
}

/** tag 名 → 分支名：`OS-43551-202610152045-online` → `OS-43551`，`CTB-45911.20261030-v1-online` → `CTB-45911` */
function deriveBranch(tag) {
  let name = String(tag || '');
  name = name.replace(/-pre-online$/, '').replace(/-online$/, '');
  name = name.replace(/([-.])\d{6,14}(-v\d+)?$/, '');
  return name;
}

function parseConventional(subject) {
  const match = String(subject || '').match(
    /^(feat|fix|refactor|perf|style|docs|test|build|ci|chore)(\(([^)]+)\))?!?:\s*(.+)$/,
  );
  if (!match) return null;
  return { type: match[1], scope: match[3] || '', subject: match[4], raw: subject };
}

function isRecent(iso, hours) {
  const when = Date.parse(iso || '');
  if (!Number.isFinite(when)) return false;
  return Date.now() - when <= hours * 3600 * 1000;
}

function uniq(list) {
  return [...new Set(list.filter(Boolean))];
}

async function mapLimit(items, limit, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      const index = cursor;
      cursor += 1;
      if (index >= items.length) return;
      results[index] = await worker(items[index], index);
    }
  });
  await Promise.all(runners);
  return results;
}

/* --------------------------------- 巡查 --------------------------------- */

async function resolveTarget(gitlab, project, medusaApp, forced) {
  if (forced) return forced;
  const candidates = medusaApp
    ? ['master', 'main', project.default_ref]
    : [project.default_ref, 'master', 'main', 'develop'];
  for (const name of uniq(candidates)) {
    const hit = await gitlab.branch(project.repository_path, name);
    if (hit) return name;
  }
  return '';
}

async function collectCandidates(args, gitlab, project, medusaApp, cookie, warnings) {
  if (medusaApp) {
    const rows = sortByCreatedAtDesc(await fetchDeployHistory(args, cookie, medusaApp.appId));
    const seen = new Set();
    const list = [];
    for (const row of rows) {
      if (!row.gitlabCommit || !row.gitlabCommit.id) continue;
      if (!(args.sinceHours <= 0 || isRecent(row.createdAt, args.sinceHours))) continue;
      // 同一 commit 只保留 createdAt 最新的一条（rows 已按 createdAt、id 倒序）
      if (seen.has(row.gitlabCommit.id)) continue;
      seen.add(row.gitlabCommit.id);
      // 发布段判定不在这里做：统一由 deploy-stage 门禁拉事件并判定
      list.push({
        source: 'medusa',
        sha: row.gitlabCommit.id,
        tag: row.gitlabCommit.branch || '',
        jira: row.jiraID || '',
        taskId: row.id,
        status: row.status,
        operator: row.operator || '',
        releaseState: releaseState(row.status),
        createdAt: row.createdAt,
      });
    }
    return list;
  }

  // 非美杜莎项目：用最近的 *-online tag 当「发布记录」（排除 -pre-online 预发）
  const tags = [];
  for (const page of [1, 2]) {
    const rows = await gitlab.tags(project.repository_path, { page, perPage: 100 });
    if (!Array.isArray(rows) || !rows.length) break;
    tags.push(...rows);
    if (rows.length < 100) break;
  }
  if (!tags.length) warnings.push('仓库没有 tag');
  const valid = tags
    .filter((tag) => typeof tag === 'object' && tag && tag.name && tag.commit && tag.commit.id)
    .filter((tag) => /-online$/.test(tag.name) && !/-pre-online$/.test(tag.name))
    .filter(
      (tag) =>
        args.sinceHours <= 0 ||
        isRecent(tag.commit.created_at || tag.commit.committed_date, args.sinceHours),
    )
    .sort((a, b) => {
      const ta = Date.parse(a.commit.created_at || a.commit.committed_date || '') || 0;
      const tb = Date.parse(b.commit.created_at || b.commit.committed_date || '') || 0;
      if (ta !== tb) return tb - ta;
      return String(a.name).localeCompare(String(b.name));
    });
  const seen = new Set();
  const list = [];
  for (const tag of valid) {
    // 同一 commit 只保留最新 tag（提交时间倒序、tag 名升序）
    if (seen.has(tag.commit.id)) continue;
    seen.add(tag.commit.id);
    list.push({
      source: 'tag',
      sha: tag.commit.id,
      tag: tag.name,
      jira: extractJira(tag.name),
      taskId: '',
      status: 'online-tag',
      operator: '',
      releaseState: 'done',
      createdAt: tag.commit.created_at || tag.commit.committed_date || '',
      // 非美杜莎仓库没有部署任务事件可查，发布段不适用
      stage: notApplicableStage(),
    });
  }
  return list;
}

/**
 * 在「包含该提交的分支」里挑发版分支（确定性）：
 * 候选按名称升序；先检查前 8 个候选的分支尖端是否等于发布提交；
 * 再看 tag 名剥掉时间戳后是否精确等于某个候选；最后只有唯一候选才用。
 * 其余情况报 NO_SOURCE_BRANCH，不猜。
 */
async function pickSource(gitlab, repo, refs, target, tag, sha) {
  const candidates = uniq(refs)
    .filter((name) => name !== target && !isIntegrationBranch(name))
    .sort();
  if (!candidates.length) return { source: '', reason: 'only-integration-branches', candidates };

  // 1) 分支尖端正好是发布提交：tag 就是在这条分支 HEAD 上打的（最多检查前 8 个候选）
  for (const name of candidates.slice(0, 8)) {
    const info = await gitlab.branch(repo, name);
    if (info && info.commit && info.commit.id === sha) return { source: name, reason: 'tip', candidates };
  }

  // 2) tag 名剥掉时间戳后能直接对上分支
  const derived = deriveBranch(tag);
  const exact = candidates.find((name) => name === derived);
  if (exact) return { source: exact, reason: 'tag-name', candidates };

  // 3) 只有一个候选
  if (candidates.length === 1) return { source: candidates[0], reason: 'unique', candidates };
  return { source: '', reason: 'ambiguous', candidates };
}

function buildTitle(source, target, commits, explicit) {
  if (explicit) return explicit;
  const feats = commits.map((item) => parseConventional(item.title)).filter((item) => item && item.type === 'feat');
  if (feats.length === 1) return feats[0].raw;
  return `合并 ${source} 到 ${target}${commits.length ? `（${commits.length} 个提交）` : ''}`;
}

function buildDescription({ project, source, target, tag, jira, taskId, origin, compare, stage }) {
  const commits = (compare && compare.commits) || [];
  const lines = ['## 发布信息'];
  if (jira) lines.push(`- Jira：[${jira}](${JIRA_BASE_URL}/${jira})`);
  if (tag) lines.push(`- 发布 tag：${tag}`);
  if (taskId) lines.push(`- 部署任务：${taskId}`);
  // 强制体现发布段：done/lastDeployHistory 都不能说明生产是否真的部署过
  lines.push(`- 发布段：${stageLabel(stage)}`);
  lines.push(`- 项目：${project.project_title}（${project.repository_path}）`);
  lines.push(`- 分支：${source} → ${target}，${commits.length} 个提交`);
  lines.push(`- 发布来源：${origin === 'medusa' ? '美杜莎发布记录（status=done）' : '仓库 *-online tag'}`);

  const diffCount = ((compare && compare.diffs) || []).length;
  lines.push('', '## 改动规模', `- ${diffCount} 个文件${compare && compare.compare_timeout ? '（对比结果被截断）' : ''}`);

  const feats = commits
    .map((item) => ({ subject: item.title, parsed: parseConventional(item.title) }))
    .filter((item) => item.parsed && item.parsed.type === 'feat');
  if (feats.length) {
    const LIMIT = 8;
    lines.push('', '## 功能改动');
    feats.slice(0, LIMIT).forEach((item) => lines.push(`- ${item.parsed.subject}`));
    if (feats.length > LIMIT) lines.push(`- …等 ${feats.length} 项`);
  }
  lines.push('', '> 由 medusa-mr-sync 批量巡查自动生成，内容取自提交信息。');
  return lines.join('\n');
}

/**
 * 等 MR 变成可合并状态（读类检查仍走 REST，与旧通道保持一致）。
 * `--wait-pipeline` 时把流水线未完成视为暂态继续等。
 */
async function waitMergeable(gitlab, repo, iid, { url = '', waitPipeline, timeoutMinutes, pollMs, label = '' }) {
  const deadline = Date.now() + timeoutMinutes * 60 * 1000;
  const blocked = ['need_rebase', 'not_approved', 'blocked_status', 'not_open'];
  const where = url ? `：${url}` : '';
  let last = '';
  for (;;) {
    const detail = await gitlab.getMR(repo, iid);
    const state = detail.detailed_merge_status || detail.merge_status || '';
    if (state !== last) {
      last = state;
      log(`  ${label}: MR !${iid} 合并状态 ${state || '-'}`);
    }
    if (detail.has_conflicts || ['conflict', 'cannot_be_merged'].includes(state)) {
      throw new SkillError(`MR !${iid} 有冲突${where}`, 'MR_CONFLICT');
    }
    if (['mergeable', 'can_be_merged'].includes(state)) return state;
    if (blocked.includes(state) || (state === 'ci_must_pass' && !waitPipeline)) {
      throw new SkillError(`MR !${iid} 不可合并：${state}${where}`, 'MR_NOT_MERGEABLE');
    }
    if (Date.now() > deadline) {
      throw new SkillError(`等 MR !${iid} 可合并超时（最后状态 ${state || '-'}）`, 'MR_TIMEOUT');
    }
    await sleep(pollMs);
  }
}

async function patrolProject(ctx, project) {
  const { args, gitlab, medusaApps, cookie } = ctx;
  const warnings = [];
  const result = {
    project: project.project_title,
    repo: project.repository_path,
    kind: '',
    target: '',
    candidates: 0,
    events: [],
    status: 'ok',
  };

  let medusaApp = medusaApps.get(project.repository_path.toLowerCase()) || null;
  if (!medusaApp) {
    // 列表没命中不直接判「非美杜莎」：先定向兜底一次，防账号列表视图缺组（2026-11-03 GZF-300 诊断）
    medusaApp = await probeMedusaApp(args, cookie, project.repository_path);
    if (medusaApp) {
      result.probed = true;
      log(`  ${project.project_title}：应用列表未命中，定向探测确认是美杜莎应用（${medusaApp.gitlabPid}）`);
    }
  }
  result.kind = medusaApp ? 'medusa' : 'tags';

  const target = await resolveTarget(gitlab, project, medusaApp, args.target);
  if (!target) {
    result.status = 'skipped';
    result.events.push({ status: 'NO_TARGET_BRANCH', detail: 'default_ref / master / main / develop 都不存在' });
    result.summary = summarize(result.events);
    return result;
  }
  result.target = target;

  let candidates;
  try {
    candidates = await collectCandidates(args, gitlab, project, medusaApp, cookie, warnings);
  } catch (error) {
    result.status = 'failed';
    result.events.push({ status: error.code || 'FAILED', detail: error.message });
    result.summary = summarize(result.events);
    return result;
  }
  result.candidates = candidates.length;
  if (!candidates.length) {
    result.status = 'no-release';
    if (warnings.length) result.events.push({ status: 'NO_RECENT_RELEASE', detail: warnings.join('；') });
    result.summary = summarize(result.events);
    return result;
  }

  const handledBranches = new Set();
  for (const candidate of candidates) {
    const event = {
      jira: candidate.jira,
      tag: candidate.tag,
      sha: candidate.sha.slice(0, 8),
      origin: candidate.source,
      releaseStatus: candidate.status,
    };
    try {
      const gateCtx = {
        candidate,
        project,
        target,
        event,
        handledBranches,
        outer: ctx,
        appId: medusaApp ? medusaApp.appId : '',
        stage: null,
        refs: [],
        source: '',
        compare: null,
        commits: [],
      };

      const gates = {
        /** 强制步骤：拉任务事件并判定 production / preview-only / unknown / not-applicable */
        'deploy-stage': async (c) => {
          c.stage = await fetchDeployStage(args, cookie, c.appId, c.candidate.taskId);
          c.candidate.stage = c.stage;
          c.event.deployStage = c.stage.verdict;
          c.event.deployStageDetail = c.stage.detail || '';
          if (c.stage.verdict !== 'production') {
            c.event.deployStageLabel = stageLabel(c.stage);
          }
          return { action: 'continue' };
        },

        /** 只自动合并 status=done 的发布；running / cancel 查完发布段后原样上报 */
        'release-state': (c) => {
          if (c.candidate.releaseState === 'done') return { action: 'continue' };
          const code =
            c.candidate.releaseState === 'canceled' ? 'RELEASE_CANCELED' : 'RELEASE_NOT_FINISHED';
          return {
            action: 'bail',
            code,
            message: `发布状态 ${c.candidate.status}；发布段：${stageLabel(c.stage)}；按规则只自动合并 status=done 的发布`,
          };
        },

        /** 批量模式不依赖本地仓库，没有 refs 需要刷新 */
        'refresh-refs': () => ({ action: 'continue' }),

        'resolve-commit': (c) => {
          c.sha = c.candidate.sha;
          return { action: 'continue' };
        },

        /** 发布提交已被目标分支包含 */
        'already-merged': async (c) => {
          c.refs = ((await gitlab.branchesWithCommit(c.project.repository_path, c.candidate.sha)) || [])
            .map((item) => item.name)
            .sort();
          c.event.branches = c.refs;
          if (!c.refs.includes(c.target)) return { action: 'continue' };
          c.event.status = 'already-merged';
          return { action: 'done' };
        },

        /** 选源分支：排除目标分支与集成分支；同一分支多个 tag 只处理一次 */
        'source-branch': async (c) => {
          const { source, reason, candidates: ambiguous } = await pickSource(
            gitlab,
            c.project.repository_path,
            c.refs,
            c.target,
            c.candidate.tag,
            c.candidate.sha,
          );
          if (!source) {
            const message = !c.refs.length
              ? 'tag 提交在远端已没有任何分支包含（分支多半已删）'
              : reason === 'ambiguous'
                ? `可疑分支太多，无法唯一确定：${(ambiguous || []).slice(0, 6).join(', ')}`
                : `只被集成分支包含：${c.refs.join(', ')}`;
            return { action: 'bail', code: 'NO_SOURCE_BRANCH', message };
          }
          c.source = source;
          c.event.source = source;
          c.event.pick = reason;
          if (c.handledBranches.has(source)) {
            return { action: 'bail', code: 'duplicate-branch' };
          }
          c.handledBranches.add(source);
          return { action: 'continue' };
        },

        /** 强制检查：载荷区间 target..source 内全部 merge 的非第一父节点必须在目标历史里 */
        'payload-merge': async (c) => {
          c.compare = (await gitlab.compare(c.project.repository_path, c.target, c.source)) || {};
          if (c.compare.compare_timeout) {
            return {
              action: 'bail',
              code: 'PAYLOAD_MERGE_COMMIT',
              message: `载荷区间 ${c.target}..${c.source} 的提交列表被 GitLab 截断（compare_timeout），无法完整检查，按不能确认处理`,
            };
          }
          c.commits = (Array.isArray(c.compare.commits) ? c.compare.commits : []).slice().sort((a, b) => {
            const ta = Date.parse(a.committed_date || a.created_at || '') || 0;
            const tb = Date.parse(b.committed_date || b.created_at || '') || 0;
            if (ta !== tb) return tb - ta;
            return String(a.id).localeCompare(String(b.id));
          });
          const mergeCommits = c.commits
            .filter((item) => Array.isArray(item.parent_ids) && item.parent_ids.length > 1)
            .map((item) => ({ id: item.id, parents: item.parent_ids, subject: item.title || '' }));
          const analysis = await analyzeMergeCommits(mergeCommits, async (parent) => {
            const refs = await gitlab.branchesWithCommit(c.project.repository_path, parent);
            if (!Array.isArray(refs)) return null;
            return refs.some((branch) => branch.name === c.target);
          });
          if (!analysis.ok) {
            return {
              action: 'bail',
              code: 'PAYLOAD_MERGE_COMMIT',
              message: `按要求不自动合并：${mergeRuleMessage({ source: c.source, target: c.target, analysis })}`,
            };
          }
          return { action: 'continue' };
        },

        /** 源分支已有 opened MR：同目标且非 Draft → 复用交给 write 合并；Draft / 异目标 → 仍只上报 */
        'opened-mr': async (c) => {
          const openedMrs = sortMrsById(
            (await gitlab.findOpenedMRs(c.project.repository_path, c.source)) || [],
          );
          if (!openedMrs.length) return { action: 'continue' };
          const { reuse, blockers } = decideOpenedMrs(openedMrs, c.target);
          if (reuse) {
            c.reuseMr = reuse;
            log(`  ${c.project.project_title}: 源分支已有 opened MR !${reuse.iid}（目标 ${c.target}，非 Draft），复用并合并`);
            if (blockers.length) {
              log(`  ${c.project.project_title}: 其余 opened MR 不动：${blockers.map((mr) => `!${mr.iid}`).join('、')}`);
            }
            return { action: 'continue' };
          }
          const hit = blockers[0];
          return {
            action: 'bail',
            code: 'OPENED_MR_EXISTS',
            message: `按要求不自动合并：源分支 ${c.source} 的 opened MR !${hit.iid}（${hit.web_url}，目标 ${hit.target_branch || '-'}${hit.draft ? '，Draft' : ''}）不可复用（只自动合并目标相同且非 Draft 的 MR），请人工处理`,
            mr: hit.iid,
            url: hit.web_url,
          };
        },

        /** 目标分支上已有 merged MR：视为已合 */
        'merged-mr': async (c) => {
          const mergedMrs = sortMrsById(
            (await gitlab.findMRs(c.project.repository_path, c.source, c.target)) || [],
          );
          const mergedMr = mergedMrs.find((item) => item.state === 'merged') || null;
          if (!mergedMr) return { action: 'continue' };
          c.event.status = 'already-merged';
          c.event.mr = mergedMr.iid;
          return { action: 'done' };
        },

        /** 提交不在 target 祖先上，但区间没有任何提交：内容已在目标分支（squash/rebase） */
        'squash-check': (c) => {
          if (c.commits.length !== 0) return { action: 'continue' };
          c.event.status = 'already-merged';
          c.event.detail = '分支内容已在目标分支（squash/rebase 合并）';
          return { action: 'done' };
        },

        /** 唯一允许产生副作用的门禁 */
        write: async (c) => {
          const title = buildTitle(c.source, c.target, c.commits, '');
          const description = buildDescription({
            project: c.project,
            source: c.source,
            target: c.target,
            tag: c.candidate.tag,
            jira: c.candidate.jira,
            taskId: c.candidate.taskId,
            origin: c.candidate.source,
            compare: c.compare,
            stage: c.stage,
          });
          c.event.commits = c.commits.length;

          if (args.dryRun) {
            if (c.reuseMr) {
              c.event.status = 'would-merge-mr';
              c.event.mr = c.reuseMr.iid;
              c.event.url = c.reuseMr.web_url;
              c.event.detail = `复用已有 opened MR !${c.reuseMr.iid}（目标 ${c.target}，非 Draft）并合并`;
            } else {
              c.event.status = 'would-create-mr';
              c.event.title = title;
            }
            c.event.mrBackend = args.mrBackend;
            c.event.asUser = args.noAsUser ? '' : args.asUser || c.candidate.operator || '';
            return { action: 'done' };
          }

          if (c.outer.mergeCount >= args.maxMerges) {
            return {
              action: 'bail',
              code: 'SKIPPED_MAX_MERGES',
              message: `本轮已达 --max-merges ${args.maxMerges}`,
            };
          }

          let iid;
          let url;
          let projectBindingId = '';
          let asUser = '';
          let headSha = '';
          if (args.mrBackend !== 'gitlab') {
            const branchInfo = await gitlab.branch(c.project.repository_path, c.source);
            headSha = (branchInfo && branchInfo.commit && branchInfo.commit.id) || c.candidate.sha;
            projectBindingId = c.outer.multica.resolveProjectBinding(c.project.repository_path);
            const account = c.outer.multica.resolveAccount({
              explicit: args.asUser,
              noAsUser: args.noAsUser,
              owner: c.candidate.operator,
            });
            asUser = account.asUser;
            c.event.asUser = account.asUser;
            c.event.asUserLabel = account.label;
            log(`  ${c.project.project_title}: MR 账号 ${account.label}；绑定 ${projectBindingId}`);
          }
          if (c.reuseMr) {
            iid = c.reuseMr.iid;
            url = c.reuseMr.web_url;
            c.event.reused = true;
            log(`  ${c.project.project_title}: 复用已有 opened MR !${iid} ${c.source} → ${c.target}`);
          } else if (args.mrBackend === 'gitlab') {
            const mr = await gitlab.createMR(c.project.repository_path, {
              source_branch: c.source,
              target_branch: c.target,
              title,
              description,
              remove_source_branch: false,
            });
            iid = mr.iid;
            url = mr.web_url;
            log(`  ${c.project.project_title}: 已创建 MR !${iid} ${c.source} → ${c.target}`);
          } else {
            const mr = c.outer.multica.ensureMR({
              projectBindingId,
              asUser,
              source: c.source,
              target: c.target,
              headSha,
              title,
              description,
            });
            iid = mr.iid;
            url = mr.url;
            log(`  ${c.project.project_title}: 已确保 MR !${iid} ${c.source} → ${c.target}`);
          }
          c.event.mr = iid;
          c.event.url = url;

          if (!args.merge) {
            c.event.status = c.reuseMr ? 'reused' : 'created';
            return { action: 'done' };
          }

          await waitMergeable(gitlab, c.project.repository_path, iid, {
            url,
            waitPipeline: args.waitPipeline,
            timeoutMinutes: args.timeoutMinutes,
            pollMs: args.pollMs,
            label: c.project.project_title,
          });

          c.outer.mergeCount += 1;
          if (args.mrBackend === 'gitlab') {
            const merged = await gitlab.mergeMR(c.project.repository_path, iid, {
              merge_when_pipeline_succeeds: args.waitPipeline ? 'true' : 'false',
            });
            c.event.status = 'merged';
            c.event.state = (merged && merged.state) || 'merged';
          } else {
            const merged = c.outer.multica.mergeMR({
              projectBindingId,
              asUser,
              iid,
              headSha,
            });
            c.event.status = 'merged';
            c.event.state = merged.state || 'merged';
          }
          return { action: 'done' };
        },
      };

      const outcome = await runGates(gateCtx, gates);
      if (outcome.action === 'bail') {
        event.status = outcome.code;
        if (outcome.message) event.detail = outcome.message;
        if (outcome.mr !== undefined) event.mr = outcome.mr;
        if (outcome.url !== undefined) event.url = outcome.url;
      }
      result.events.push(event);
    } catch (error) {
      event.status = error.code || 'FAILED';
      event.detail = error.message;
      result.events.push(event);
      if (error instanceof AuthError) throw error;
    }
  }

  // 发布段统计在全部候选跑完门禁后汇总（deploy-stage 门禁已为每个候选设置 stage）
  const stageCounts = { production: 0, previewOnly: 0, unknown: 0, notApplicable: 0 };
  candidates.forEach((item) => {
    stageCounts[stageBucket(item.stage && item.stage.verdict)] += 1;
  });
  result.deployStages = stageCounts;
  result.deployStageIssues = candidates
    .filter((item) => {
      const verdict = item.stage && item.stage.verdict;
      return !item.stage || verdict === 'preview-only' || verdict === 'unknown';
    })
    .sort((a, b) => {
      const ta = Date.parse(a.createdAt || '') || 0;
      const tb = Date.parse(b.createdAt || '') || 0;
      if (ta !== tb) return tb - ta;
      return String(a.tag).localeCompare(String(b.tag)) || String(a.sha).localeCompare(String(b.sha));
    })
    .map((item) => ({
      jira: item.jira,
      tag: item.tag,
      taskId: item.taskId,
      sha: item.sha.slice(0, 8),
      verdict: item.stage ? item.stage.verdict : 'unknown',
      detail: item.stage ? item.stage.detail || '' : '发布段判定失败',
    }));

  // 项目级概要只留真正需要人看的
  const meaningful = result.events.filter((item) =>
    [
      'merged',
      'created',
      'would-create-mr',
      'MR_CONFLICT',
      'MR_NOT_MERGEABLE',
      'MR_TIMEOUT',
      'GITLAB_FAILED',
      'NO_SOURCE_BRANCH',
      'SKIPPED_MAX_MERGES',
      'PAYLOAD_MERGE_COMMIT',
      'OPENED_MR_EXISTS',
      'RELEASE_NOT_FINISHED',
      'RELEASE_CANCELED',
      'FAILED',
    ].includes(item.status),
  );
  result.summary = summarize(result.events);
  result.events = meaningful;
  result.status = meaningful.some((item) => ['MR_CONFLICT', 'MR_NOT_MERGEABLE', 'MR_TIMEOUT', 'GITLAB_FAILED', 'FAILED'].includes(item.status))
    ? 'has-error'
    : 'ok';
  return result;
}

function summarize(events) {
  const counts = {};
  events.forEach((item) => {
    counts[item.status] = (counts[item.status] || 0) + 1;
  });
  return counts;
}

export async function main(argv) {
  const args = parseArgs(argv);
  if (!args.enableWrites) {
    log('写入未解锁：批量巡查默认只读，不会建 MR、不会合并（解锁用 --enable-writes 或 PATROL_ENABLE_WRITES=1）');
  }

  log(`巡查范围：guazi-flow 工作区 ${args.workspace} 的全部项目${args.dryRun ? '（只读）' : ''}`);
  const guaziCookie = resolveGuaziCookie();
  const flowCookie = await resolveFlowCookie(args, guaziCookie);

  let projects = await fetchInboxProjects(args, flowCookie);
  log(`inbox 项目：${projects.length} 个`);

  if (args.projects.length) {
    const wanted = args.projects.map((item) => item.toLowerCase());
    projects = projects.filter(
      (item) =>
        wanted.includes(item.project_title.toLowerCase()) || wanted.includes(item.repository_path.toLowerCase()),
    );
    if (!projects.length) throw new SkillError(`没有匹配的项目：${args.projects.join(', ')}`, 'PROJECT_NOT_FOUND');
  }
  if (args.include) {
    const list = args.include.split(',').map((item) => item.trim().toLowerCase()).filter(Boolean);
    projects = projects.filter(
      (item) => list.includes(item.project_title.toLowerCase()) || list.includes(item.repository_path.toLowerCase()),
    );
  }
  if (args.exclude) {
    const list = args.exclude.split(',').map((item) => item.trim().toLowerCase()).filter(Boolean);
    projects = projects.filter(
      (item) => !list.includes(item.project_title.toLowerCase()) && !list.includes(item.repository_path.toLowerCase()),
    );
  }
  // 项目顺序固定按 repository_path 升序，保证重复运行输出一致
  projects = [...projects].sort((a, b) => a.repository_path.localeCompare(b.repository_path));
  log(`本轮巡查：${projects.length} 个项目`);

  const token = resolveGitlabToken();
  if (!token) {
    throw new SkillError('拿不到 GitLab token：设置 GITLAB_TOKEN，或在 ~/.git-credentials 配 git.guazi-corp.com，或登录 glab', 'GITLAB_FAILED');
  }
  const gitlab = createGitlab(token);

  let medusaApps = new Map();
  try {
    medusaApps = await fetchMedusaApps(args, guaziCookie);
    log(`美杜莎应用：${medusaApps.size} 个（翻页取全量；能对上的项目走发布记录，未命中的先定向探测，其余走 tag）`);
  } catch (error) {
    log(`提示：拿不到美杜莎应用列表（${error.message}），全部项目按 tag 巡查`);
  }

  const ctx = {
    args,
    gitlab,
    medusaApps,
    cookie: guaziCookie,
    mergeCount: 0,
    // 写入默认走 multica；dry-run 不会调用任何 multica 命令
    multica: args.mrBackend === 'multica' ? createMulticaGitlab({ log }) : null,
  };
  // 写入模式固定串行：--max-merges 的取舍依赖执行顺序，并发会让重复运行合并到不同项目。
  const concurrency = args.enableWrites ? 1 : args.concurrency;
  const results = await mapLimit(projects, concurrency, async (project) => {
    try {
      return await patrolProject(ctx, project);
    } catch (error) {
      if (error instanceof AuthError) throw error;
      return {
        project: project.project_title,
        repo: project.repository_path,
        status: 'failed',
        events: [{ status: error.code || 'FAILED', detail: error.message }],
        summary: { [error.code || 'FAILED']: 1 },
      };
    }
  });

  const summary = {};
  results.forEach((item) => {
    Object.entries(item.summary || {}).forEach(([key, value]) => {
      summary[key] = (summary[key] || 0) + value;
    });
  });
  const payload = {
    ok: true,
    action: 'patrol',
    dryRun: args.dryRun,
    scope: { workspace: args.workspace, projects: projects.length, sinceHours: args.sinceHours },
    summary: {
      merged: summary.merged || 0,
      created: summary.created || 0,
      wouldCreate: summary['would-create-mr'] || 0,
      alreadyMerged: summary['already-merged'] || 0,
      noSource: summary.NO_SOURCE_BRANCH || 0,
      conflicts: summary.MR_CONFLICT || 0,
      notMergeable: summary.MR_NOT_MERGEABLE || 0,
      manualReview: (summary.PAYLOAD_MERGE_COMMIT || 0) + (summary.OPENED_MR_EXISTS || 0),
      releaseNotFinished: summary.RELEASE_NOT_FINISHED || 0,
      releaseCanceled: summary.RELEASE_CANCELED || 0,
      failed: (summary.FAILED || 0) + (summary.GITLAB_FAILED || 0),
      skipped: summary.SKIPPED_MAX_MERGES || 0,
    },
    projects: results,
  };
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function runIfDirect() {
  // 技能目录是软链，比较 realpath 才能判断是不是直接运行
  const entry = process.argv[1] ? fs.realpathSync(path.resolve(process.argv[1])) : '';
  const self = fs.realpathSync(fileURLToPath(import.meta.url));
  if (entry !== self) return;
  main(process.argv.slice(2)).catch((error) => {
    const auth = error instanceof AuthError;
    const code = auth ? 'AUTH_REQUIRED' : error.code || 'FAILED';
    log(`\n失败：${error.message}`);
    process.stdout.write(`${JSON.stringify({ ok: false, code, message: error.message })}\n`);
    process.exit(auth ? 2 : 1);
  });
}

runIfDirect();
