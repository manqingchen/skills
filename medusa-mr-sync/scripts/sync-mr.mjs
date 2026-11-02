#!/usr/bin/env node
/**
 * 核对一次 Medusa 发布对应的代码分支有没有进 master；没进就在 GitLab 建 MR 并合并。
 *
 * 固定流程（没有跳过开关）：
 *   1. 取发布记录（按 createdAt 倒序，取对应 tag/Jira 的最新一条）并判定发布段；
 *   2. 只有 status=done 的发布继续；running 报 RELEASE_NOT_FINISHED，cancel 报 RELEASE_CANCELED；
 *   3. 检查载荷区间 target..source 内的全部 merge commit（非第一父节点必须在目标历史里）；
 *   4. 检查源分支上是否有 opened MR；
 *   5. 全部通过才建 MR / 合并。
 *
 * `--branch` 同样会查发布记录：tag 匹配 `<branch>[-<14位时间戳>]-online` 时使用该记录；
 * 匹配不到才用远端分支 HEAD（此时发布段为 unknown / NO_TASK）。
 *
 * 进度写 stderr，结果写一行 JSON 到 stdout。
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  releaseState,
  sortByCreatedAtDesc,
  sortMrsById,
  classifyDeployStage,
  stageLabel,
  analyzeMergeCommits,
  mergeRuleMessage,
  isIntegrationBranch,
} from './rules.mjs';
import { runGates } from './pipeline.mjs';

const DEFAULTS = {
  app: 'oversea/global-station',
  env: 'online',
  baseUrl: 'https://medusa-new.guazi-apps.com',
  target: 'master',
  pollMs: 10000,
  timeoutMinutes: 30,
  waitPipeline: false,
};

const log = (message) => process.stderr.write(`${message}\n`);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// 平台的 jiraLink 字段还是旧的 http://project.guazi-corp.com（已连不通），实际 Jira 在这
const JIRA_BASE_URL = process.env.JIRA_BASE_URL || 'https://cjira.guazi-corp.com/browse';

class AuthError extends Error {}
class SkillError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;
  }
}

function parseArgs(argv) {
  // 默认建 MR 并合并；发布状态不是 done 会被 pickRelease 拦掉
  const args = { ...DEFAULTS, jira: '', tag: '', branch: '', title: '', dryRun: false, merge: true };
  const valueFlags = {
    '--jira': 'jira',
    '--tag': 'tag',
    '--branch': 'branch',
    '--app': 'app',
    '--env': 'env',
    '--base-url': 'baseUrl',
    '--target': 'target',
    '--poll-ms': 'pollMs',
    '--timeout-minutes': 'timeoutMinutes',
    '--title': 'title',
  };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    // 默认只建 MR，合并留给人点；只读用 --dry-run，要自动合并加 --merge
    if (['--dry-run', '--check', '--status', '--check-only'].includes(key)) args.dryRun = true;
    else if (key === '--yes') {
      /* 兼容老写法：默认已经是真执行，这个参数不再改变行为 */
    } else if (key === '--merge') args.merge = true;
    else if (key === '--no-merge') args.merge = false;
    else if (key === '--wait-pipeline') args.waitPipeline = true;
    else if (valueFlags[key]) {
      if (argv[i + 1] === undefined) throw new Error(`${key} 缺少取值`);
      args[valueFlags[key]] = argv[i + 1];
      i += 1;
    } else throw new Error(`未知参数：${key}`);
  }
  args.pollMs = Number(args.pollMs);
  args.timeoutMinutes = Number(args.timeoutMinutes);
  if (!args.jira && !args.tag && !args.branch) throw new Error('需要 --jira / --tag / --branch 之一');
  return args;
}

function git(args, { allowFail = false } = {}) {
  try {
    return execFileSync('git', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (error) {
    if (allowFail) return '';
    throw new SkillError(String(error.stderr || error.message).trim(), 'GIT_FAILED');
  }
}

function glab(apiPath, { method = 'GET', fields = {} } = {}) {
  const argv = ['api', apiPath];
  if (method !== 'GET') argv.push('--method', method);
  Object.entries(fields).forEach(([key, value]) => argv.push('--field', `${key}=${value}`));
  let out;
  try {
    out = execFileSync('glab', argv, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (error) {
    const stderr = String(error.stderr || '').trim();
    const detail = (stderr.match(/\{[\s\S]*\}/) || [])[0] || stderr || error.message;
    throw new SkillError(
      `GitLab 接口失败（${method} ${apiPath.split('?')[0]}）：${String(detail).replace(/\s+/g, ' ').slice(0, 300)}`,
      'GITLAB_FAILED',
    );
  }
  return out.trim() ? JSON.parse(out) : null;
}

/** 登录态：MEDUSA_COOKIE 优先，否则用 guazi-sso-login 的缓存账号换 GUAZISSO */
function resolveCookie() {
  if (process.env.MEDUSA_COOKIE) return process.env.MEDUSA_COOKIE;
  const candidates = [
    path.join(os.homedir(), '.zcode/skills/guazi-sso-login/scripts/guazi_sso_login.py'),
    path.join(os.homedir(), '.agents/skills/guazi-sso-login/scripts/guazi_sso_login.py'),
  ];
  const script = candidates.find((file) => fs.existsSync(file));
  if (!script) throw new AuthError('没找到 guazi-sso-login skill，或设置 MEDUSA_COOKIE="GUAZISSO=xxx"');
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

/** 通用 Medusa GraphQL 请求 */
async function medusaGql(args, cookie, query, variables) {
  let response;
  try {
    response = await fetch(`${args.baseUrl.replace(/\/+$/, '')}/graphql?env=${encodeURIComponent(args.env)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Cookie: cookie },
      body: JSON.stringify({ query, variables }),
    });
  } catch (error) {
    throw new SkillError(`连不上 Medusa：${error.message}`, 'MEDUSA_FAILED');
  }
  const text = await response.text();
  if (response.status === 401 || /token is empty/i.test(text)) throw new AuthError('GUAZISSO 无效或已过期');
  if (!response.ok) throw new SkillError(`Medusa 返回 ${response.status}：${text.slice(0, 120)}`, 'MEDUSA_FAILED');
  const payload = JSON.parse(text);
  if (Array.isArray(payload.errors) && payload.errors.length) {
    throw new SkillError(payload.errors[0].message || 'Medusa GraphQL 报错', 'MEDUSA_FAILED');
  }
  return payload.data || {};
}

/** 发布记录：谁在哪次发布里打了哪个 tag、对应哪个提交 */
async function fetchDeployHistory(args, cookie) {
  const query = `query($id: String!, $page: Int, $itemsPerPage: Int) {
    app(id: $id) {
      deployHistory(page: $page, itemsPerPage: $itemsPerPage) {
        id status taskType jiraID operator createdAt
        gitlabCommit { id branch }
      }
    }
  }`;
  const data = await medusaGql(args, cookie, query, { id: args.app, page: 1, itemsPerPage: 50 });
  return (data.app && data.app.deployHistory) || [];
}

const EVENTS_QUERY = `query($taskID: Int!) {
  queryDeployEvent(taskID: $taskID) { reason createdAt creator }
}`;

/**
 * 发布段判定（强制步骤）：拉任务事件并判定 production / preview-only / unknown。
 * 只在 deploy-stage 门禁里调用，单次和批量都是同一条路径。
 */
async function fetchDeployStage(args, cookie, taskId, appId = args.app) {
  if (!taskId) {
    return {
      taskId: null,
      verdict: 'unknown',
      reason: 'NO_TASK',
      previewDeployedAt: '',
      productionDeployedAt: '',
      productionRequestedAt: '',
      trafficSwitchedAt: '',
      detail: '没有部署任务（--branch 未匹配到发布记录）',
    };
  }
  const data = await medusaGql(args, cookie, EVENTS_QUERY, { taskID: Number(taskId) });
  const events = Array.isArray(data.queryDeployEvent) ? data.queryDeployEvent : [];
  return classifyDeployStage({ events, appName: String(appId).split('/').pop(), taskId });
}

/**
 * 从发布记录里选这次要核对的记录（确定性）：
 * - 记录先按 createdAt 倒序、id 倒序排序；
 * - `--tag`：gitlabCommit.branch 与 tag 完全相等，取排序后的第一条；
 * - `--jira`：该 Jira 的最新一条；
 * - `--branch`：tag 匹配 `^<branch>(-\d{14})?-online$`，取最新一条；匹配不到返回 null。
 * 状态不在这里判断，由 requireDone 统一处理（保证发布段先判定）。
 */
function pickRelease(rows, args) {
  const sorted = sortByCreatedAtDesc(rows);
  if (args.tag) {
    const hit = sorted.find((row) => row.gitlabCommit && row.gitlabCommit.branch === args.tag);
    if (!hit) throw new SkillError(`发布记录里没有 tag ${args.tag}（只回看最近 50 条）`, 'RELEASE_NOT_FOUND');
    return hit;
  }
  if (args.jira) {
    const hit = sorted.find((row) => row.jiraID === args.jira);
    if (!hit) throw new SkillError(`发布记录里没有 Jira ${args.jira}（只回看最近 50 条）`, 'RELEASE_NOT_FOUND');
    return hit;
  }
  const escaped = String(args.branch).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`^${escaped}(-\\d{14})?-online$`);
  return sorted.find((row) => row.gitlabCommit && pattern.test(String(row.gitlabCommit.branch || ''))) || null;
}

/** 只有 done 的发布才继续；running / cancel 直接上报，错误消息里带发布段 */
/** tag 名形如 <branch>-<yyyyMMddHHmmSS>-online，去掉后缀就是分支名 */
function branchFromTag(tag) {
  return String(tag || '').replace(/-\d{14}-online$/, '');
}

/** 远端分支的 HEAD；分支不存在时给个干净的错误 */
function branchHead(branch) {
  const ref = `refs/remotes/origin/${branch}`;
  const sha = git(['rev-parse', '--verify', '--quiet', ref], { allowFail: true });
  if (!sha) throw new SkillError(`远端没有分支 ${branch}`, 'BRANCH_NOT_FOUND');
  return sha;
}

/**
 * 远端包含该提交的分支（排序后返回，排除目标分支与集成分支）。
 * ignored 是原始候选里被排除的集成分支，用于报错时说明原因。
 */
function remoteBranchesWith(sha, target) {
  const raw = git(
    ['for-each-ref', `--contains=${sha}`, '--format=%(refname:short)', 'refs/remotes/origin'],
    { allowFail: true },
  )
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.replace(/^origin\//, ''))
    .filter((name) => name && name !== 'HEAD');
  const ignored = raw.filter((name) => name === target || isIntegrationBranch(name)).sort();
  const all = raw
    .filter((name) => name !== target && !isIntegrationBranch(name))
    .sort();
  return { all, ignored };
}

/** 其中「分支尖端正好就是这个提交」的，最可能是打 tag 时所在的分支 */
function tipBranchesOf(sha, branches) {
  return branches.filter(
    (name) => git(['rev-parse', `refs/remotes/origin/${name}`], { allowFail: true }) === sha,
  );
}

/**
 * 挑出用来建 MR 的源分支（确定性）：
 * 候选 = 远端包含该提交、且不是目标分支/集成分支的分支，按名称升序；
 * 顺序 = 尖端匹配的分支在前，其余按名称升序；再按 tag 名推出的分支、Jira 号分支优先。
 */
function pickSourceBranch({ sha, derived, jira, target }) {
  const { all, ignored } = remoteBranchesWith(sha, target);
  const tips = tipBranchesOf(sha, all);
  const ordered = [...tips, ...all.filter((name) => !tips.includes(name))];
  const preferred = [derived, jira].filter(Boolean);

  const source =
    preferred.map((name) => ordered.find((item) => item === name)).find(Boolean) ||
    (ordered.length === 1 ? ordered[0] : '');
  return { source, all, tips, ignored };
}

function isMerged(sha, target) {
  try {
    git(['merge-base', '--is-ancestor', sha, `origin/${target}`]);
    return true;
  } catch (error) {
    if (!/Not a valid|unknown revision|bad object/i.test(String(error.stderr || error.message))) return false;
  }
  // 本地没有这个提交时问 GitLab
  try {
    const refs = glab(`projects/:id/repository/commits/${sha}/refs?type=branch`);
    return Array.isArray(refs) && refs.some((item) => item.name === target);
  } catch {
    return null;
  }
}

/** 提交是否已在目标分支历史里；本地判断不了时问 GitLab（null = 无法确认） */
function isInTargetHistory(sha, target) {
  try {
    git(['merge-base', '--is-ancestor', sha, `origin/${target}`]);
    return true;
  } catch (error) {
    if (!/Not a valid|unknown revision|bad object/i.test(String(error.stderr || error.message))) return false;
  }
  try {
    const refs = glab(`projects/:id/repository/commits/${sha}/refs?type=branch`);
    if (!Array.isArray(refs)) return null;
    return refs.some((item) => item.name === target);
  } catch {
    return null;
  }
}

/**
 * 读取载荷区间 `origin/target..origin/source` 内的全部 merge 提交。
 * 区间枚举失败或任一 merge 的父节点读不出来，直接抛 PAYLOAD_MERGE_COMMIT（不能确认就不放行）。
 */
function loadPayloadMerges(target, source) {
  const range = `origin/${target}..origin/${source}`;
  let output;
  try {
    output = git(['rev-list', '--merges', range]);
  } catch (error) {
    throw new SkillError(
      `无法枚举载荷区间 ${range} 的 merge 提交，按不能确认处理：${error.message}`,
      'PAYLOAD_MERGE_COMMIT',
    );
  }
  const ids = output
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .sort();
  return ids.map((id) => {
    const line = git(['rev-list', '--parents', '-n', '1', id], { allowFail: true });
    const parts = line.split(/\s+/).filter(Boolean);
    if (!parts.length || parts[0] !== id) {
      throw new SkillError(
        `无法读取 merge 提交 ${id.slice(0, 8)} 的父节点，按不能确认处理`,
        'PAYLOAD_MERGE_COMMIT',
      );
    }
    return {
      id,
      parents: parts.slice(1),
      subject: git(['log', '-1', '--format=%s', id], { allowFail: true }),
    };
  });
}

/** 解析 conventional commit；解析不出来返回 null */
function parseConventional(subject) {
  const match = subject.match(/^(feat|fix|refactor|perf|style|docs|test|build|ci|chore)(\(([^)]+)\))?!?:\s*(.+)$/);
  if (!match) return null;
  return { type: match[1], scope: match[3] || '', subject: match[4], raw: subject };
}

/**
 * MR 标题要代表整条分支，不能拿最后一次提交硬套，更不能写死 chore(release)。
 * 规则：显式 --title > 唯一 feat 提交原样用（主功能）> 兜底「合并 X 到 master（N 个提交）」。
 * 多个 feat 时不去挑「最重要」的——机器挑不准，宁可给个中性标题让人改。
 */
function buildTitle(source, target, explicit) {
  if (explicit) return explicit;
  const subjects = git(['log', '--no-merges', '--format=%s', `origin/${target}..origin/${source}`], { allowFail: true })
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  const feats = subjects.map(parseConventional).filter((item) => item && item.type === 'feat');
  if (feats.length === 1) return feats[0].raw;
  return `合并 ${source} 到 ${target}${subjects.length ? `（${subjects.length} 个提交）` : ''}`;
}

/**
 * MR 描述：取提交信息和改动统计，不做代码分析。
 * 分支可能几十上百个提交，分析型摘要既贵又不可靠；commit 分组 + shortstat 足够追溯。
 */
function buildDescription({ source, target, tag, jira, taskId, stage }) {
  const commits = git(['log', '--no-merges', '--format=%h%x09%s', `origin/${target}..origin/${source}`], {
    allowFail: true,
  })
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [hash, ...rest] = line.split('\t');
      const subject = rest.join('\t');
      const parsed = parseConventional(subject);
      return { hash, subject: parsed ? parsed.subject : subject, group: parsed ? parsed.type : '其他' };
    });

  const groups = new Map();
  commits.forEach((item) => {
    if (!groups.has(item.group)) groups.set(item.group, []);
    groups.get(item.group).push(item);
  });

  const stats = git(['diff', '--shortstat', `origin/${target}...origin/${source}`], { allowFail: true });

  const lines = ['## 发布信息'];
  if (jira) lines.push(`- Jira：[${jira}](${JIRA_BASE_URL}/${jira})`);
  if (tag) lines.push(`- 发布 tag：${tag}`);
  if (taskId) lines.push(`- 部署任务：${taskId}`);
  // 强制体现发布段：done/lastDeployHistory 都不能说明生产是否真的部署过
  lines.push(`- 发布段：${stageLabel(stage)}`);
  lines.push(`- 分支：${source} → ${target}，${commits.length} 个提交（不含 merge）`);
  lines.push('', '## 改动规模', stats ? `- ${stats}` : '- （取不到统计）');

  // 只列 feat：发布合并的读者关心「上了什么功能」，fix/chore 是过程噪音，去 MR 的 Commits 页看
  const feats = groups.get('feat') || [];
  if (feats.length) {
    const LIMIT = 8;
    lines.push('', '## 功能改动');
    feats.slice(0, LIMIT).forEach((item) => lines.push(`- ${item.subject}`));
    if (feats.length > LIMIT) lines.push(`- …等 ${feats.length} 项`);
  }
  lines.push('', '> 由 medusa-mr-sync 自动生成，内容取自提交信息。');
  return lines.join('\n');
}

(async () => {
  // 批量模式：巡查 guazi-flow inbox 里的全部项目（不需要本地仓库）
  if (process.argv.slice(2).some((item) => item === '--patrol' || item === '--all')) {
    const { main: patrol } = await import('./patrol.mjs');
    return patrol(process.argv.slice(2));
  }
  const args = parseArgs(process.argv.slice(2));
  const cookie = resolveCookie();

  const rows = await fetchDeployHistory(args, cookie);
  const release = pickRelease(rows, args);
  if (release) {
    log(
      `发布记录：任务 ${release.id} / ${release.jiraID} / tag ${(release.gitlabCommit && release.gitlabCommit.branch) || '-'} / 提交 ${((release.gitlabCommit && release.gitlabCommit.id) || '').slice(0, 8)} / 状态 ${release.status}`,
    );
  } else {
    log(`发布记录里没有匹配 ${args.branch} 的 tag（^${args.branch}(-14位时间戳)?-online$），改用远端分支 HEAD`);
  }

  const ctx = {
    args,
    cookie,
    release,
    stage: null,
    sha: '',
    tag: '',
    jira: '',
    merged: false,
    source: '',
    extra: 0,
    ahead: 0,
    behind: 0,
  };

  const gates = {
    /** 强制步骤：任何一次核对都必须判定发布段；接口失败直接抛错终止 */
    'deploy-stage': async (c) => {
      c.stage = await fetchDeployStage(args, cookie, c.release ? c.release.id : null);
      log(`发布段：${stageLabel(c.stage)}`);
      if (c.stage.verdict === 'unknown') {
        log('警告：未能确认生产部署，后续结论不能写「已上线生产」');
      }
      return { action: 'continue' };
    },

    /** 只有 done 的发布继续；running / cancel 直接上报，错误消息带发布段 */
    'release-state': (c) => {
      if (!c.release) return { action: 'continue' };
      const state = releaseState(c.release.status);
      if (state === 'done') return { action: 'continue' };
      const name = `任务 ${c.release.id}（${c.release.jiraID || '-'} / ${(c.release.gitlabCommit && c.release.gitlabCommit.branch) || '-'}，状态 ${c.release.status}）`;
      const suffix = `发布段：${stageLabel(c.stage)}`;
      if (state === 'canceled') {
        return { action: 'bail', code: 'RELEASE_CANCELED', message: `这次发布已取消，不建 MR：${name}；${suffix}` };
      }
      return { action: 'bail', code: 'RELEASE_NOT_FINISHED', message: `这次发布未结单，不建 MR：${name}；${suffix}` };
    },

    /** 只读：同步远端引用 */
    'refresh-refs': () => {
      log('同步远端引用…');
      git(['fetch', 'origin', '--tags', '--prune', '--quiet']);
      return { action: 'continue' };
    },

    /** 确定本次要核对的提交和 tag/Jira */
    'resolve-commit': (c) => {
      c.sha = c.release ? (c.release.gitlabCommit && c.release.gitlabCommit.id) || '' : branchHead(args.branch);
      c.tag = c.release ? (c.release.gitlabCommit && c.release.gitlabCommit.branch) || '' : '';
      c.jira = c.release ? c.release.jiraID : '';
      if (!c.sha) {
        return { action: 'bail', code: 'NO_SHA', message: '拿不到要核对的提交（发布记录里没有 commit）' };
      }
      return { action: 'continue' };
    },

    /** 提交已在目标分支：直接结束 */
    'already-merged': (c) => {
      c.merged = isMerged(c.sha, args.target);
      if (c.merged === null) {
        log(
          `提示：无法确认提交是否已在 ${args.target}（本地与 GitLab 都失败），按未合并处理，输出标记 mergeStateUnknown=true`,
        );
      }
      if (!c.merged) return { action: 'continue' };
      log(`提交 ${c.sha.slice(0, 8)} 已在 origin/${args.target} 上，无需合并`);
      return {
        action: 'done',
        outcome: 'already-merged',
        output: {
          action: 'already-merged',
          jira: c.jira,
          tag: c.tag,
          sha: c.sha,
          deployStage: c.stage,
          mergeStateUnknown: false,
          target: args.target,
        },
      };
    },

    /** 选源分支：排除目标分支和集成分支，结果确定性排序 */
    'source-branch': (c) => {
      const derived = args.branch || branchFromTag(c.tag);
      const { source, all, tips, ignored } = pickSourceBranch({
        sha: c.sha,
        derived,
        jira: c.jira,
        target: args.target,
      });
      if (all.length) {
        log(`可用源分支：${all.join(', ')}` + (tips.length ? `（尖端就是该提交：${tips.join(', ')}）` : ''));
      }
      if (ignored.length) log(`已排除集成分支/目标分支：${ignored.join(', ')}`);
      if (!source) {
        const candidates = all.length ? `候选：${all.join(', ')}` : '没有非集成分支包含该提交';
        return {
          action: 'bail',
          code: 'NO_SOURCE_BRANCH',
          message: `提交 ${c.sha.slice(0, 8)} 无法唯一确定源分支（${candidates}），请用 --branch 指定`,
        };
      }
      c.source = source;
      if (derived && source !== derived) {
        log(`提示：tag 名推出来的分支是 ${derived}，但提交实际在 ${source} 上，按 ${source} 处理`);
      }
      c.extra = Number(git(['rev-list', '--count', `${c.sha}..origin/${source}`], { allowFail: true }) || 0);
      if (c.extra > 0) {
        log(`注意：${source} 在该提交之后还有 ${c.extra} 个提交，合并会把它们一并带进 ${args.target}`);
      }
      c.ahead = Number(
        git(['rev-list', '--count', `origin/${args.target}..origin/${source}`], { allowFail: true }) || 0,
      );
      c.behind = Number(
        git(['rev-list', '--count', `origin/${source}..origin/${args.target}`], { allowFail: true }) || 0,
      );
      log(`分支 ${source} 相对 origin/${args.target}：领先 ${c.ahead}，落后 ${c.behind}`);
      return { action: 'continue' };
    },

    /** 强制检查：载荷区间内所有 merge 的非第一父节点必须已在目标历史里 */
    'payload-merge': async (c) => {
      const payloadMerges = loadPayloadMerges(args.target, c.source);
      const analysis = await analyzeMergeCommits(payloadMerges, (parent) => isInTargetHistory(parent, args.target));
      if (!analysis.ok) {
        return {
          action: 'bail',
          code: 'PAYLOAD_MERGE_COMMIT',
          message: `按要求不自动合并：${mergeRuleMessage({ source: c.source, target: args.target, analysis })}`,
        };
      }
      if (payloadMerges.length) {
        log(
          `载荷区间 ${args.target}..${c.source} 有 ${payloadMerges.length} 个 merge 提交，非第一父节点都已在 ${args.target} 历史里，放行`,
        );
      }
      return { action: 'continue' };
    },

    /** 强制检查：源分支任意 opened MR（含 Draft，不区分目标分支） */
    'opened-mr': (c) => {
      const openedMrs = sortMrsById(
        glab(`projects/:id/merge_requests?source_branch=${encodeURIComponent(c.source)}&state=opened&per_page=20`) || [],
      );
      if (!openedMrs.length) return { action: 'continue' };
      const hit = openedMrs[0];
      return {
        action: 'bail',
        code: 'OPENED_MR_EXISTS',
        message: `按要求不自动合并：源分支 ${c.source} 存在 opened MR !${hit.iid}（${hit.web_url}，目标 ${hit.target_branch || '-'}${hit.draft ? '，Draft' : ''}）`,
      };
    },

    /** 目标分支上已有 merged MR：视为已合 */
    'merged-mr': (c) => {
      const mergedMrs = sortMrsById(
        glab(
          `projects/:id/merge_requests?source_branch=${encodeURIComponent(c.source)}&target_branch=${encodeURIComponent(args.target)}&state=merged&per_page=20`,
        ) || [],
      );
      const mergedMr = mergedMrs[0];
      if (!mergedMr) return { action: 'continue' };
      log(`目标分支上已有合并完成的 MR !${mergedMr.iid}`);
      return {
        action: 'done',
        outcome: 'already-merged',
        output: {
          action: 'already-merged',
          iid: mergedMr.iid,
          url: mergedMr.web_url,
          deployStage: c.stage,
          mergeStateUnknown: c.merged === null,
          extraCommits: c.extra,
          source: c.source,
          target: args.target,
        },
      };
    },

    /** 单次模式用 git 祖先关系判断，不需要额外的 squash 检查 */
    'squash-check': () => ({ action: 'continue' }),

    /** 唯一允许产生副作用的门禁 */
    write: async (c) => {
      const title = buildTitle(c.source, args.target, args.title);
      const description = buildDescription({
        source: c.source,
        target: args.target,
        tag: c.tag,
        jira: c.jira,
        taskId: c.release ? c.release.id : '',
        stage: c.stage,
      });

      if (args.dryRun) {
        log('只读模式（--dry-run）：只检查，不建 MR、不合并');
        log(`  将新建 MR：${title}\n${description.replace(/^/gm, '    ')}`);
        if (args.merge) log(`  然后合并到 ${args.target}`);
        return {
          action: 'done',
          outcome: 'dry-run',
          output: {
            dryRun: true,
            jira: c.jira,
            tag: c.tag,
            sha: c.sha,
            deployStage: c.stage,
            mergeStateUnknown: c.merged === null,
            extraCommits: c.extra,
            source: c.source,
            target: args.target,
            ahead: c.ahead,
            behind: c.behind,
            title,
            description,
          },
        };
      }

      const mr = glab('projects/:id/merge_requests', {
        method: 'POST',
        fields: {
          source_branch: c.source,
          target_branch: args.target,
          title,
          description,
          remove_source_branch: 'false',
        },
      });
      log(`已创建 MR !${mr.iid}：${mr.web_url}`);

      if (!args.merge) {
        return {
          action: 'done',
          outcome: 'created',
          output: {
            action: 'created',
            iid: mr.iid,
            url: mr.web_url,
            deployStage: c.stage,
            mergeStateUnknown: c.merged === null,
            extraCommits: c.extra,
            source: c.source,
            target: args.target,
          },
        };
      }

      const deadline = Date.now() + args.timeoutMinutes * 60 * 1000;
      let last = '';
      for (;;) {
        const detail = glab(`projects/:id/merge_requests/${mr.iid}`);
        // 新版本 GitLab 用 detailed_merge_status，老版本只有 merge_status，两种都认
        const status = detail.detailed_merge_status || detail.merge_status || '';
        if (status !== last) {
          log(`合并状态：${status}`);
          last = status;
        }
        if (detail.has_conflicts || ['conflict', 'cannot_be_merged'].includes(status)) {
          throw new SkillError(`MR !${mr.iid} 有冲突，需人工解决：${mr.web_url}`, 'MR_CONFLICT');
        }
        if (['mergeable', 'can_be_merged'].includes(status)) break;
        if (['need_rebase', 'not_approved', 'blocked_status', 'ci_must_pass', 'not_open'].includes(status)) {
          throw new SkillError(`MR !${mr.iid} 不可合并：${status}（${mr.web_url}）`, 'MR_NOT_MERGEABLE');
        }
        if (Date.now() > deadline) {
          throw new SkillError(`等 MR !${mr.iid} 可合并超时（最后状态 ${status || '-'}）`, 'MR_TIMEOUT');
        }
        await sleep(args.pollMs);
      }

      const mergedRes = glab(`projects/:id/merge_requests/${mr.iid}/merge`, {
        method: 'PUT',
        fields: { merge_when_pipeline_succeeds: args.waitPipeline ? 'true' : 'false' },
      });
      const state = (mergedRes && mergedRes.state) || 'merged';
      log(`MR !${mr.iid} 状态：${state}`);
      return {
        action: 'done',
        outcome: 'merged',
        output: {
          action: 'merged',
          iid: mr.iid,
          url: mr.web_url,
          state,
          deployStage: c.stage,
          mergeStateUnknown: c.merged === null,
          extraCommits: c.extra,
          source: c.source,
          target: args.target,
        },
      };
    },
  };

  const result = await runGates(ctx, gates);
  if (result.action === 'bail') {
    throw new SkillError(result.message, result.code);
  }
  process.stdout.write(`${JSON.stringify({ ok: true, ...result.output })}\n`);
})().catch((error) => {
  const auth = error instanceof AuthError;
  const code = auth ? 'AUTH_REQUIRED' : error.code || 'FAILED';
  log(`\n失败：${error.message}`);
  process.stdout.write(`${JSON.stringify({ ok: false, code, message: error.message })}\n`);
  process.exit(auth ? 2 : 1);
});
