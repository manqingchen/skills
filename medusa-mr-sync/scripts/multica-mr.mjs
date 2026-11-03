/**
 * 通过 `multica gitlab mr ensure/merge` 建/合并 MR，并支持 `--as-user` 指定需求负责人的账号。
 *
 * 文档：cwiki「用指定账号创建和合并 MR」（pageId=735803048）。
 * 所需 ID：
 *   - 仓库的 project_binding_id ← `multica repo list --output json`
 *   - 执行人的 user_id         ← `multica workspace member list --output json`
 *     （取成员记录的 user_id，不是 id）
 *
 * 本模块只负责「写」：创建/复用 MR、合并 MR。
 * 读取类检查（opened MR、已合并 MR、分支 refs）仍由调用方现有的 glab / REST 通道完成。
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

class MulticaError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;
  }
}

/* --------------------------------- 纯函数（可单测） --------------------------------- */

/** 仓库路径归一化：去协议/主机、`.git`、首尾斜杠，统一小写 */
export function normalizeRepoPath(value) {
  let text = String(value || '').trim();
  if (!text) return '';
  text = text.replace(/^[a-z]+:\/\/[^/]+\//i, '');
  text = text.replace(/^git@[^:]+:/i, '');
  text = text.replace(/\.git$/i, '');
  return text.replace(/^\/+|\/+$/g, '').toLowerCase();
}

/** 兼容数组 / { data } / { repositories } / { repos } / { items } / { results } 等返回形态 */
export function extractList(payload, keys = []) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== 'object') return [];
  for (const key of [...keys, 'repositories', 'repos', 'members', 'data', 'items', 'results']) {
    if (Array.isArray(payload[key])) return payload[key];
    if (payload[key] && typeof payload[key] === 'object') {
      const nested = extractList(payload[key], keys);
      if (nested.length) return nested;
    }
  }
  return [];
}

/** 仓库绑定记录里的 project_binding_id（兼容常见命名） */
export function pickBindingId(row) {
  if (!row || typeof row !== 'object') return '';
  return (
    row.project_binding_id ||
    row.projectBindingId ||
    row.project_binding?.id ||
    row.projectBinding?.id ||
    row.binding_id ||
    row.bindingId ||
    ''
  );
}

/** 仓库绑定记录里用于匹配的仓库路径/地址 */
export function pickRepoPaths(row) {
  if (!row || typeof row !== 'object') return [];
  const candidates = [
    row.repository_path,
    row.repo_path,
    row.path,
    row.repository?.path,
    row.repository_url,
    row.repo_url,
    row.url,
    row.html_url,
    row.ssh_url,
    row.name,
    row.repository?.name,
  ];
  return candidates.filter(Boolean).map(String);
}

/** 成员记录里的 user_id；文档明确要求用 user_id，不要用成员记录的 id */
export function pickMemberUserId(row) {
  if (!row || typeof row !== 'object') return '';
  return row.user_id || row.userId || row.user?.user_id || row.member?.user_id || '';
}

/** 成员的可用名称（用于把 operator / --as-user 的 username 对上） */
export function pickMemberNames(row) {
  if (!row || typeof row !== 'object') return [];
  const user = row.user || row.member || {};
  return [
    row.username,
    row.user_name,
    row.name,
    row.display_name,
    row.displayName,
    row.email,
    user.username,
    user.user_name,
    user.name,
    user.display_name,
    user.email,
  ]
    .filter(Boolean)
    .map(String);
}

/**
 * 把 username / name / email / user_id 解析成 multica user_id。
 * 只做精确匹配（忽略大小写），不做模糊/前缀匹配——匹配不到就报错，不猜。
 * 显式传入的 UUID 直接接受（可能不在当前页的成员列表里）。
 */
export function resolveAsUserFromMembers(members, value) {
  const wanted = String(value || '').trim();
  if (!wanted) return { userId: '', label: '', exact: false };
  const lower = wanted.toLowerCase();
  const byId = members.find((item) => String(pickMemberUserId(item)).toLowerCase() === lower);
  if (byId) return { userId: pickMemberUserId(byId), label: pickMemberNames(byId)[0] || wanted, exact: true };
  const byName = members.find((item) => pickMemberNames(item).some((name) => name.toLowerCase() === lower));
  if (byName) {
    return { userId: pickMemberUserId(byName), label: pickMemberNames(byName)[0] || wanted, exact: true };
  }
  if (UUID_RE.test(wanted)) return { userId: wanted, label: wanted, exact: true };
  return { userId: '', label: '', exact: false };
}

/** 从命令输出里找 JSON（命令默认返回 JSON，但可能夹带提示行） */
export function parseJsonOutput(stdout) {
  const text = String(stdout || '').trim();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    /* 继续尝试截取 */
  }
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(text.slice(start, end + 1));
    } catch {
      /* 放弃 */
    }
  }
  return null;
}

/** 递归找第一个满足条件的值 */
function deepFind(value, predicate, depth = 0) {
  if (depth > 6 || value === null || value === undefined) return '';
  if (typeof value === 'string') return predicate(value) ? value : '';
  if (Array.isArray(value)) {
    for (const item of value) {
      const hit = deepFind(item, predicate, depth + 1);
      if (hit) return hit;
    }
    return '';
  }
  if (typeof value === 'object') {
    for (const [key, item] of Object.entries(value)) {
      if (predicate(key) && typeof item !== 'object' && item !== null && item !== '') return String(item);
      const hit = deepFind(item, predicate, depth + 1);
      if (hit) return hit;
    }
  }
  return '';
}

/** 从 ensure/merge 输出里提取 MR 编号、地址、状态 */
export function extractMrResult(payload) {
  const url =
    deepFind(payload, (key) => ['web_url', 'html_url', 'mr_url', 'url', 'merge_request_url'].includes(key)) ||
    (typeof payload === 'string' ? (payload.match(/https?:\/\/[^\s"']+/g) || []).find((item) => /merge_requests\/\d+/.test(item)) || '' : '');
  const iid =
    deepFind(payload, (key) => ['iid', 'mr_iid', 'number', 'merge_request_iid'].includes(key)) ||
    (url.match(/merge_requests\/(\d+)/) || [])[1] ||
    (typeof payload === 'string' ? (payload.match(/!(\d+)/) || [])[1] : '') ||
    '';
  const state =
    deepFind(payload, (key) => ['state', 'merge_status', 'status'].includes(key)) ||
    (typeof payload === 'string' ? (/\b(merged|open|closed)\b/.exec(payload) || [])[1] || '' : '');
  return { iid: iid ? String(iid) : '', url: url || '', state: state || '' };
}

/* ----------------------------------- 客户端 ----------------------------------- */

const AUTH_PATTERNS = /not signed in|session has expired|invalid token|unauthori[sz]ed|401|Run .?multica login/i;
const UPGRADE_PATTERNS = /unknown command "gitlab"|unknown command .*gitlab|does not support|please upgrade/i;

function runMultica(argv) {
  try {
    return execFileSync('multica', argv, {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 120000,
    }).trim();
  } catch (error) {
    if (error.code === 'ENOENT') {
      throw new MulticaError('找不到 multica CLI；请先安装/升级 multica，并执行 multica login', 'MULTICA_UNAVAILABLE');
    }
    const stderr = String(error.stderr || '').trim();
    const stdout = String(error.stdout || '').trim();
    const detail = (stderr || stdout || error.message || '').replace(/\s+/g, ' ').slice(0, 400);
    if (AUTH_PATTERNS.test(detail)) {
      throw new MulticaError(`multica 未登录或会话已过期：执行 multica login 后重试（${detail}）`, 'MULTICA_AUTH_REQUIRED');
    }
    if (UPGRADE_PATTERNS.test(detail)) {
      throw new MulticaError(
        `当前 multica 不支持 gitlab mr 命令，需要先升级（multica update）：${detail}`,
        'MULTICA_GITLAB_UNAVAILABLE',
      );
    }
    throw new MulticaError(`multica ${argv.slice(0, 3).join(' ')} 失败：${detail}`, 'MULTICA_MR_FAILED');
  }
}

function tempDescriptionFile(description) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'medusa-mr-sync-'));
  const file = path.join(dir, 'mr.md');
  fs.writeFileSync(file, String(description || ''), 'utf8');
  return {
    file,
    cleanup: () => {
      try {
        fs.rmSync(dir, { recursive: true, force: true });
      } catch {
        /* 临时目录清理失败不影响结果 */
      }
    },
  };
}

/**
 * 创建 multica gitlab 写入客户端。repo/member 列表按进程缓存，
 * 批量巡查只会各拉一次。
 */
export function createMulticaGitlab({ log = () => {} } = {}) {
  let available = null;
  let repoCache = null;
  let memberCache = null;

  function ensureAvailable() {
    if (available !== null) return available;
    try {
      execFileSync('multica', ['gitlab', 'mr', '--help'], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 30000,
      });
      available = true;
    } catch (error) {
      const detail = String(error.stderr || error.stdout || error.message || '').replace(/\s+/g, ' ').slice(0, 300);
      if (AUTH_PATTERNS.test(detail)) {
        throw new MulticaError(`multica 未登录或会话已过期：执行 multica login 后重试（${detail}）`, 'MULTICA_AUTH_REQUIRED');
      }
      available = false;
      throw new MulticaError(
        `当前 multica 不支持 gitlab mr 命令，需要先升级（multica update）：${detail || 'unknown command'}`,
        'MULTICA_GITLAB_UNAVAILABLE',
      );
    }
    return available;
  }

  function loadRepos() {
    if (repoCache) return repoCache;
    ensureAvailable();
    const payload = parseJsonOutput(runMultica(['repo', 'list', '--output', 'json']));
    if (!payload) {
      throw new MulticaError('multica repo list 没有返回可解析的 JSON', 'PROJECT_BINDING_NOT_FOUND');
    }
    repoCache = extractList(payload, ['repositories', 'repos']);
    return repoCache;
  }

  function loadMembers() {
    if (memberCache) return memberCache;
    ensureAvailable();
    const payload = parseJsonOutput(runMultica(['workspace', 'member', 'list', '--output', 'json']));
    if (!payload) {
      throw new MulticaError('multica workspace member list 没有返回可解析的 JSON', 'AS_USER_NOT_FOUND');
    }
    memberCache = extractList(payload, ['members']);
    return memberCache;
  }

  /** 按仓库路径找 project_binding_id；找不到就说清楚，不回退成别的仓库 */
  function resolveProjectBinding(repositoryPath) {
    const wanted = normalizeRepoPath(repositoryPath);
    if (!wanted) {
      throw new MulticaError('拿不到仓库路径，无法解析 project_binding_id', 'PROJECT_BINDING_NOT_FOUND');
    }
    const repos = loadRepos();
    // 先精确匹配，再允许带上命名空间前缀；不按模糊/包含匹配，避免绑错仓库
    const exact = repos.find((row) => pickRepoPaths(row).some((item) => normalizeRepoPath(item) === wanted));
    const hit =
      exact ||
      repos.find((row) => pickRepoPaths(row).some((item) => normalizeRepoPath(item).endsWith(`/${wanted}`)));
    const bindingId = hit ? pickBindingId(hit) : '';
    if (!bindingId) {
      throw new MulticaError(
        `multica 仓库列表里没有 ${repositoryPath} 的 project_binding_id；确认仓库已加入工作区并在“设置 → GitLab”完成绑定`,
        'PROJECT_BINDING_NOT_FOUND',
      );
    }
    return bindingId;
  }

  /**
   * 决定这次用谁的账号：
   * - --no-as-user：不传，使用调用者自己的账号；
   * - --as-user / 需求负责人（发布 operator）：解析成 multica user_id，解析不到就报错；
   * - 都没有：不传，使用调用者自己的账号，并提示。
   */
  function resolveAccount({ explicit = '', noAsUser = false, owner = '' } = {}) {
    if (noAsUser) return { asUser: '', label: '调用者账号（--no-as-user）' };
    const wanted = String(explicit || owner || '').trim();
    if (!wanted) return { asUser: '', label: '调用者账号（未识别到需求负责人）' };
    const hit = resolveAsUserFromMembers(loadMembers(), wanted);
    if (!hit.userId) {
      throw new MulticaError(
        `multica 工作区成员里找不到「${wanted}」；确认该成员已加入工作区并连接 GitLab，或用 --as-user 指定 user_id（不要用成员记录的 id）`,
        'AS_USER_NOT_FOUND',
      );
    }
    return { asUser: hit.userId, label: `${hit.label}（${hit.userId}）` };
  }

  /**
   * 创建或复用 MR（相同 source→target 已存在时直接复用，原创建者不变）。
   * headSha 必须是源分支当前完整提交 SHA。
   */
  function ensureMR({ projectBindingId, asUser = '', source, target, headSha, title, description = '' }) {
    ensureAvailable();
    const temp = tempDescriptionFile(description);
    try {
      const argv = [
        'gitlab',
        'mr',
        'ensure',
        '--project-binding-id',
        projectBindingId,
        '--source-branch',
        source,
        '--target-branch',
        target,
        '--head-sha',
        headSha,
        '--title',
        title,
      ];
      if (asUser) argv.push('--as-user', asUser);
      if (description) argv.push('--description-file', temp.file);
      const stdout = runMultica(argv);
      const payload = parseJsonOutput(stdout) || stdout;
      const result = extractMrResult(payload);
      if (!result.iid) {
        throw new MulticaError(
          `multica gitlab mr ensure 的输出里没有 MR 编号：${String(stdout).replace(/\s+/g, ' ').slice(0, 300)}`,
          'MULTICA_MR_PARSE_FAILED',
        );
      }
      log(`multica：已确保 MR !${result.iid}${asUser ? `（账号 ${asUser}）` : ''}`);
      return result;
    } finally {
      temp.cleanup();
    }
  }

  /** 合并 MR；执行人需要有目标分支的合并权限 */
  function mergeMR({ projectBindingId, asUser = '', iid, headSha }) {
    ensureAvailable();
    const argv = ['gitlab', 'mr', 'merge', String(iid), '--project-binding-id', projectBindingId];
    if (asUser) argv.push('--as-user', asUser);
    argv.push('--head-sha', headSha);
    const stdout = runMultica(argv);
    const payload = parseJsonOutput(stdout) || stdout;
    const result = extractMrResult(payload);
    return { ...result, raw: payload };
  }

  return { ensureAvailable, resolveProjectBinding, resolveAccount, ensureMR, mergeMR };
}
