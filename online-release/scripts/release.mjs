#!/usr/bin/env node
/**
 * 上线发布（Online → 预发）：打 tag → 等流水线构建 → 发起部署审批 → 等审批 → 部署预发。
 *
 * 全程无界面：登录态从专用 Chrome profile（或 MEDUSA_COOKIE）里取，业务请求直接打平台 GraphQL。
 * 进度写 stderr，最终结果以一行 JSON 写 stdout，方便 agent 解析。
 *
 * 例：
 *   node release.mjs --jira OS-43558 --yes
 *   node release.mjs --tag OS-43558-20261029181532-online --stop-before-deploy
 *   node release.mjs --jira OS-43558 --status
 */
import { execFileSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import {
  AuthError,
  createClient,
  isDeployableStatus,
  matchesJiraTag,
  normalizeApproval,
  PLATFORM_APPROVAL_LABEL,
} from './medusa.mjs';
import { resolveCookie } from './session.mjs';

const DEFAULTS = {
  app: 'oversea/global-station',
  env: 'online',
  packageName: 'deploy',
  baseUrl: 'https://medusa-new.guazi-apps.com',
  branch: '',
  waitMinutes: 30,
  approveMinutes: 180,
  onDeployBusy: 'ask',
  // 建完发布任务后要不要替人点「快速部署 / 灰度部署」：fast=快速部署（直接全量到预发），flow=灰度，manual=只建任务
  deployMode: 'fast',
  // 预览实例所在的集群，与平台页面里 preview 段固定使用的值一致
  previewCluster: 'online1',
  pollMs: 15000,
  smoke: '是',
  health: '是',
  dbChange: '无',
};

// 一个部署包同时只能跑一个部署任务，占着的是别人的发布时谁也不知道要等多久，
// 所以「部署包有正在执行的部署任务」按等待处理，而不是直接失败。
const DEPLOY_BUSY_PATTERN = /正在执行的部署任务|部署任务正在执行|deploy\s*in\s*progress/i;
const DEPLOY_BUSY_RETRY_MS = Number(process.env.ONLINE_RELEASE_DEPLOY_RETRY_MS || 3 * 60 * 1000);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const log = (message) => process.stderr.write(`${message}\n`);

function parseArgs(argv) {
  const args = {
    ...DEFAULTS,
    jira: '',
    tag: '',
    yes: false,
    dryRun: false,
    status: false,
    stopBeforeDeploy: false,
    json: true,
  };
  const valueFlags = {
    '--jira': 'jira',
    '--tag': 'tag',
    '--app': 'app',
    '--env': 'env',
    '--branch': 'branch',
    '--package': 'packageName',
    '--base-url': 'baseUrl',
    '--wait-minutes': 'waitMinutes',
    '--approve-minutes': 'approveMinutes',
    '--on-deploy-busy': 'onDeployBusy',
    '--deploy-mode': 'deployMode',
    '--preview-cluster': 'previewCluster',
    '--poll-ms': 'pollMs',
    '--smoke': 'smoke',
    '--health': 'health',
    '--db-change': 'dbChange',
  };
  const boolFlags = {
    '--yes': 'yes',
    '--dry-run': 'dryRun',
    '--status': 'status',
    '--stop-before-deploy': 'stopBeforeDeploy',
    '--require-mr-merged': 'requireMrMerged',
    '--skip-preflight': 'skipPreflight',
    '--ignore-in-progress': 'ignoreInProgress',
    '--no-tsc': 'noTsc',
    '--no-mr': 'noMr',
    '--force': 'force',
  };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (boolFlags[key]) {
      args[boolFlags[key]] = true;
    } else if (valueFlags[key]) {
      const next = argv[i + 1];
      if (next === undefined) throw new Error(`${key} 缺少取值`);
      args[valueFlags[key]] = next;
      i += 1;
    } else {
      throw new Error(`未知参数：${key}`);
    }
  }
  args.waitMinutes = Number(args.waitMinutes);
  args.approveMinutes = Number(args.approveMinutes);
  if (!['ask', 'wait', 'exit'].includes(args.onDeployBusy)) {
    throw new Error('--on-deploy-busy 只支持 ask / wait / exit');
  }
  if (!['fast', 'flow', 'manual'].includes(args.deployMode)) {
    throw new Error('--deploy-mode 只支持 fast / flow / manual');
  }
  if (args.skipPreflight) args.requireMrMerged = false;
  args.pollMs = Number(args.pollMs);
  if (!args.jira && !args.tag) throw new Error('需要 --jira（自动打 tag）或 --tag（用已有 tag）');
  return args;
}

function git(args, { allowFail = false } = {}) {
  try {
    return execFileSync('git', args, { encoding: 'utf8' }).trim();
  } catch (error) {
    if (allowFail) return '';
    throw error;
  }
}

/** 透传输出的命令（pnpm tsc 之类） */
function run(command, args, { allowFail = false } = {}) {
  try {
    execFileSync(command, args, { stdio: ['ignore', 'inherit', 'inherit'] });
  } catch (error) {
    if (allowFail) return false;
    throw error;
  }
  return true;
}

function glabJson(args) {
  const out = execFileSync('glab', args, { encoding: 'utf8' });
  return JSON.parse(out);
}

/** 与 gton 一致：tag 名 = <当前分支>-<yyyymmddHHMMSS>-online */
function currentBranch(explicit) {
  if (explicit) return explicit;
  const branch = git(['rev-parse', '--abbrev-ref', 'HEAD']);
  if (branch === 'HEAD') throw new Error('当前是 detached HEAD，无法确定分支名；可显式传 --branch');
  return branch;
}

/**
 * 判断某个提交是否已经在 master 上。
 * 先查本地（最准，前提是 fetch 过），本地没有这个对象再走 GitLab API。
 */
function isMergedIntoMaster(sha) {
  if (!sha) return null;
  try {
    git(['merge-base', '--is-ancestor', sha, 'origin/master']);
    return true;
  } catch (error) {
    // 本地有这个提交但不在 master 上时，git 只返回退出码 1、没有输出；
    // 只有「对象不存在」才需要去 GitLab 兜底查
    const message = String(error.stderr || error.message || '');
    if (!/Not a valid|unknown revision|bad object/i.test(message)) return false;
  }
  try {
    const refs = glabJson([
      'api',
      `projects/:id/repository/commits/${sha}/refs?type=branch`,
    ]);
    return refs.some((item) => item.name === 'master');
  } catch {
    return null; // 查不到就返回 null，交给调用方按「未知」处理
  }
}

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** tag 是不是本分支的在线发布 tag：<branch>-<yyyyMMddHHmmss>-online */
function isBranchReleaseTag(tag, branch) {
  if (!tag || !branch) return false;
  return new RegExp(`^${escapeRegExp(branch)}-\\d{14}-online$`).test(String(tag));
}

/** 一段文本里是否提到本分支的发布 tag（平台某些字段会带后缀，如 "tag (sha)"） */
function mentionsBranchReleaseTag(text, branch) {
  if (!text || !branch) return false;
  return new RegExp(`${escapeRegExp(branch)}-\\d{14}-online`).test(String(text));
}

/**
 * 本分支最近一次在线发布用的 tag。
 * 本地 tag 和远端 tag 都算（可能别人推的），按时间戳倒序取最新。
 */
function findPreviousReleaseTag(branch) {
  if (!branch) return null;
  const local = git(['tag', '--list', `${branch}-*-online`], { allowFail: true }).split('\n');
  const remote = git(['ls-remote', '--tags', 'origin'], { allowFail: true })
    .split('\n')
    .map((line) => (line.split('refs/tags/')[1] || '').replace(/\^\{\}$/, ''));
  const tags = [...new Set([...local, ...remote].map((tag) => tag.trim()).filter(Boolean))]
    .filter((tag) => isBranchReleaseTag(tag, branch))
    .sort();
  return tags.length ? tags[tags.length - 1] : null;
}

/**
 * 距上次发布有没有新的非合并提交。
 * 只看第一父链：把自己分支的提交算进去，把 master 合进来带来的提交排除掉，
 * 避免 master 前移就误判成「有改动」。
 */
function hasNewCommitsSince(tag) {
  if (!tag) return true;
  const exists = git(['rev-parse', '--verify', '--quiet', `refs/tags/${tag}`], { allowFail: true });
  if (!exists) return true; // 本地没有该 tag，没法比较，按有更改处理
  const count = git(
    ['rev-list', '--no-merges', '--first-parent', '--count', `${tag}..HEAD`],
    { allowFail: true },
  );
  return Number(count) > 0;
}

/**
 * 发布前体检：平台上如果还有「发布中」的任务，确认它们的代码已经进了 master，
 * 避免别人发了线上但漏合 master，你在错误基线上再发一版。
 * 本分支自己上一次的发布不算「别人的任务」——新版本就绪后会取消它，所以不拦。
 */
async function precheckInProgress(client, args, branch) {
  const rows = await client.getDeployHistory(args.env, args.app, { itemsPerPage: 20 });
  const inProgress = rows.filter((row) => row.id && !['done', 'cancel'].includes(String(row.status)));
  if (!inProgress.length) {
    log('发布前体检：没有进行中的发布');
    return [];
  }
  log(`发布前体检：发现 ${inProgress.length} 个发布中任务`);
  const items = inProgress.map((row) => {
    const sha = (row.gitlabCommit && row.gitlabCommit.id) || '';
    // 平台的 gitlabCommit.branch 里装的其实是 tag 名，两个字段都看
    const tag =
      (row.gitlabCommit && (row.gitlabCommit.tag || row.gitlabCommit.branch)) || '';
    const ours = isBranchReleaseTag(tag, branch);
    const merged = isMergedIntoMaster(sha);
    const label = `${row.jiraID || '-'} / 任务 ${row.id} / ${tag || '-'}`;
    log(
      `  · ${label}（${sha.slice(0, 8) || '无 commit'}）→ ` +
        (ours
          ? '本分支上次发布，新版本就绪后取消'
          : merged === true
            ? '已合入 master'
            : merged === false
              ? '未合入 master'
              : '合并状态未知（本地无此提交且 GitLab 查不到）'),
    );
    return { ...row, sha, tag, ours, merged, label };
  });
  const unmerged = items.filter((item) => item.merged === false && !item.ours);
  if (unmerged.length && !args.ignoreInProgress) {
    const detail = unmerged.map((item) => `  · ${item.label}（${item.sha.slice(0, 8)} 不在 master 上）`).join('\n');
    throw Object.assign(
      new Error(
        `有发布中的任务还没合入 master，先确认合并再发布：\n${detail}\n（确认无碍可加 --ignore-in-progress 跳过）`,
      ),
      { code: 'IN_PROGRESS_UNMERGED', details: unmerged.map((i) => i.label) },
    );
  }
  return items;
}

/**
 * 可选的前置校验：该分支的 MR 是否已合入 master。
 * 你的发布流程是「pull master → push → tag」，不依赖 MR，所以默认不跑；
 * 需要额外保险时加 --require-mr-merged。
 */
function preflightMr(branch) {
  try {
    const mrs = glabJson([
      'api',
      `projects/:id/merge_requests?source_branch=${encodeURIComponent(branch)}&state=all&per_page=20`,
    ]);
    const merged = mrs.find((mr) => mr.state === 'merged');
    if (!merged) {
      const state = mrs.map((mr) => `${mr.iid}:${mr.state}`).join(', ') || '无 MR';
      throw Object.assign(
        new Error(`分支 ${branch} 还没有已合入 master 的 MR（${state}）；去掉 --require-mr-merged 可跳过此校验`),
        { code: 'MR_NOT_MERGED' },
      );
    }
    log(`MR 已合并：!${merged.iid} ${merged.title}`);
  } catch (error) {
    if (error.code === 'MR_NOT_MERGED') throw error;
    throw Object.assign(
      new Error(`无法确认 MR 合并状态（glab 不可用？${String(error.message).slice(0, 80)}）`),
      { code: 'PREFLIGHT_FAILED' },
    );
  }
}

/** 这条分支相对 origin/master 还有没有非合并提交（有才需要建 MR） */
function hasUnmergedCommits() {
  const count = git(['rev-list', '--no-merges', '--count', 'origin/master..HEAD'], { allowFail: true });
  return Number(count) > 0;
}

/** MR 标题：优先主功能 feat 提交（与 create-mr 技能口径一致），不要用收尾 fix 当标题 */
function buildMrTitle(branch, jira) {
  const subjects = git(['log', '--no-merges', '--reverse', '--format=%s', 'origin/master..HEAD'], {
    allowFail: true,
  })
    .split('\n')
    .filter(Boolean);
  const candidates = [
    jira && subjects.find((s) => s.startsWith(`feat(${jira})`)),
    subjects.find((s) => s.startsWith('feat(')),
    jira && subjects.find((s) => s.includes(`(${jira})`)),
    subjects[0],
  ];
  return candidates.find(Boolean) || `chore: 合并 ${branch} 到 master`;
}

/** 查 cwiki 需求父页（标题以 Jira 号开头）；查不到返回 null，不影响建 MR */
async function findWikiPage(jira) {
  if (!jira) return null;
  try {
    const script = `${process.env.HOME}/.zcode/skills/guazi-sso-login/run.sh`;
    // run.sh 没有执行位，必须经 bash 调起
    const cookie = execFileSync('bash', [script, 'wiki', '--validate', '--plain'], {
      encoding: 'utf8',
      timeout: 30000,
    }).trim();
    if (!cookie) return null;
    const cql = encodeURIComponent(`title ~ "${jira}"`);
    const res = await fetch(`https://cwiki.guazi.com/rest/api/content/search?cql=${cql}&limit=20`, {
      headers: { Cookie: cookie },
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) return null;
    const data = await res.json();
    return (data.results || []).find((page) => String(page.title).startsWith(jira)) || null;
  } catch {
    return null;
  }
}

/** MR 描述：四段模板（变更摘要 / 关联任务 / 影响环境 / 验证方式） */
async function buildMrBody(branch, jira) {
  const subjects = git(['log', '--no-merges', '--format=- %s', 'origin/master..HEAD'], {
    allowFail: true,
  })
    .split('\n')
    .filter(Boolean);
  const stat = git(['diff', '--shortstat', 'origin/master...HEAD'], { allowFail: true });
  const wiki = await findWikiPage(jira);
  const jiraLine = jira
    ? `- Jira：<https://cjira.guazi-corp.com/browse/${jira}>`
    : '- Jira：待补充';
  const wikiLine = wiki
    ? `- Wiki：<https://cwiki.guazi.com/pages/viewpage.action?pageId=${wiki.id}>（${wiki.title}）`
    : '- Wiki：待补充（cwiki 需求父页未找到）';

  return [
    '## 变更摘要',
    '',
    ...subjects,
    ...(stat ? [`- 改动面：${stat}`] : []),
    '',
    '## 关联任务',
    '',
    jiraLine,
    wikiLine,
    '',
    '## 影响环境',
    '',
    '待确认（发布流程自动创建）',
    '',
    '## 验证方式',
    '',
    '待补充（发布流程自动创建，可在 MR 上编辑）',
    '',
  ].join('\n');
}

/**
 * push 之后把当前分支建成到 master 的 MR。**只建不合**，合并仍由用户决定。
 * 幂等：已有 open MR 直接复用；分支提交都已在 master 时跳过。
 */
async function ensureMr(branch, jira) {
  if (!hasUnmergedCommits()) {
    log('分支的非合并提交都已在 master 上，不建 MR');
    return { created: false, reason: 'nothing-to-merge' };
  }

  let mrs = [];
  try {
    mrs = glabJson([
      'api',
      `projects/:id/merge_requests?source_branch=${encodeURIComponent(branch)}&state=all&per_page=20`,
    ]);
  } catch (error) {
    throw Object.assign(
      new Error(`查不到 ${branch} 的 MR 状态（glab 不可用？${String(error.message).slice(0, 120)}）`),
      { code: 'MR_CREATE_FAILED' },
    );
  }

  const open = mrs.find((mr) => mr.state === 'opened');
  if (open) {
    log(`MR 已存在，直接复用：!${open.iid} ${open.web_url}`);
    return { created: false, iid: open.iid, webUrl: open.web_url };
  }

  const title = buildMrTitle(branch, jira);
  const body = await buildMrBody(branch, jira);
  log(`建 MR：${branch} → master｜${title}`);
  let output = '';
  try {
    output = execFileSync(
      'glab',
      [
        'mr',
        'create',
        '--source-branch',
        branch,
        '--target-branch',
        'master',
        '--title',
        title,
        '--description',
        body,
        '--yes',
      ],
      { encoding: 'utf8' },
    );
  } catch (error) {
    throw Object.assign(
      new Error(`建 MR 失败：${String(error.stderr || error.message).slice(0, 300)}`),
      { code: 'MR_CREATE_FAILED' },
    );
  }

  const url = (output.match(/https?:\/\/\S+/) || [])[0] || '';
  const iid = Number((url.match(/merge_requests\/(\d+)/) || [])[1]) || null;
  log(`MR 已创建：${url || '(glab 未输出链接)'}`);
  return { created: true, iid, webUrl: url };
}

function stamp() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

async function confirm(question) {
  if (!process.stdin.isTTY) throw new Error('非交互环境，请显式加 --yes 确认');
  const rl = createInterface({ input: process.stdin, output: process.stderr });
  const answer = await new Promise((resolve) => rl.question(`${question} (y/N) `, resolve));
  rl.close();
  return answer.trim().toLowerCase() === 'y';
}

/** 按 <jira>-<时间戳>-online 打 tag 并推送；返回 tag 名 */
async function createTag(args) {
  const branch = currentBranch(args.branch);
  const tag = `${branch}-${stamp()}-online`;

  log(`发布分支：${branch}，将要打的 tag：${tag}`);
  if (args.dryRun) {
    log(`[dry-run] 将执行：git pull → git pull origin master → git push → 建 MR → ${args.noTsc ? '' : 'pnpm tsc → '}git tag ${tag} → git push origin ${tag}`);
    return { tag, mr: null };
  }

  const dirty = git(['status', '--porcelain']);
  if (dirty) {
    throw Object.assign(
      new Error(`工作区有未提交改动，先提交或 stash 再发布：\n${dirty.slice(0, 400)}`),
      { code: 'DIRTY_WORKTREE' },
    );
  }

  if (args.requireMrMerged) preflightMr(branch);
  if (!args.yes && !(await confirm(`确认按上面四步发布 ${branch} 并打 tag ${tag}？`))) {
    throw Object.assign(new Error('已取消'), { code: 'CANCELLED' });
  }

  // 1. 拉当前分支（本地新建、远端还没有的分支没有可拉的东西，跳过避免 couldn't find remote ref）
  const remoteBranch = git(['ls-remote', '--heads', 'origin', branch], { allowFail: true }).trim();
  if (remoteBranch) {
    log('1/4 git pull …');
    run('git', ['pull', 'origin', branch]);
  } else {
    log(`1/4 git pull …跳过：远端还没有 ${branch}，第 3 步会推上去`);
  }
  // 2. 把 master 合进当前分支
  log('2/4 git pull origin master …');
  try {
    run('git', ['pull', 'origin', 'master']);
  } catch {
    const conflicts = git(['diff', '--name-only', '--diff-filter=U'], { allowFail: true });
    throw Object.assign(
      new Error(`合并 master 有冲突，请手工解决后重来：\n${conflicts || '（见 git status）'}`),
      { code: 'MERGE_CONFLICT' },
    );
  }
  // 3. 推送
  log('3/4 git push …');
  run('git', ['push', 'origin', `HEAD:${branch}`]);

  // 3.5 push 之后自动建 MR（只建不合，合并仍由用户决定）；--no-mr 跳过
  const mr = args.noMr ? { skipped: true } : await ensureMr(branch, args.jira);

  // 4. 打 tag 并推送（与 gton 一致：tag 名 = 分支名 + 时间戳 + -online，打 tag 前跑 tsc）
  if (!args.noTsc) {
    log('打 tag 前跑 pnpm tsc（与 gton 一致，可 --no-tsc 跳过）…');
    run('pnpm', ['tsc']);
  }
  log(`4/4 git tag ${tag} && git push origin ${tag}`);
  git(['tag', tag]);
  git(['push', 'origin', tag]);
  log(`已推送 tag：${tag}`);
  return { tag, mr };
}

/** 可部署版本是分页的，命中就停，避免 tag 排到后面被误判成「还没构建好」 */
async function findImage(client, args, tag) {
  const deadline = Date.now() + args.waitMinutes * 60 * 1000;
  let attempt = 0;
  while (Date.now() < deadline) {
    attempt += 1;
    const collected = [];
    for (let page = 1; page <= 5; page += 1) {
      const { nodes, totalCount } = await client.listDeployImages(args.env, args.app, { page });
      collected.push(...nodes);
      const hit = nodes.some((node) => node.gitlabCommit && node.gitlabCommit.tag === tag);
      const exhausted = totalCount > 0 ? collected.length >= totalCount : nodes.length === 0;
      if (hit || exhausted) break;
    }
    const found = collected.find((node) => node.gitlabCommit && node.gitlabCommit.tag === tag);
    if (found) return found;
    log(`[等构建] 第 ${attempt} 次没有看到 ${tag}，${Math.round(args.pollMs / 1000)}s 后重试`);
    await sleep(args.pollMs);
  }
  throw Object.assign(new Error(`等了 ${args.waitMinutes} 分钟仍没看到 tag：${tag}`), {
    code: 'TAG_TIMEOUT',
  });
}

async function findApproval(client, args, appCommitId) {
  const nodes = await client.listDeployApprovals(args.env, args.app);
  return nodes.find(
    (item) =>
      (item.appCommit && String(item.appCommit.id) === String(appCommitId)) ||
      String(item.gitCommitID) === String(appCommitId),
  );
}

/** 校验 + 提交审批；已有通过的审批直接复用 */
async function ensureApproved(client, args, image) {
  const existing = await findApproval(client, args, image.id);
  const state = normalizeApproval(existing);
  if (state === 'passed') {
    log(`该镜像已有通过的审批（${existing.approvalID}，Jira ${existing.jiraID}），跳过发起`);
    return { record: existing, submitted: false, jiraId: existing.jiraID || args.jira };
  }

  const jiraId = args.jira || (existing && existing.jiraID) || '';
  if (!jiraId) {
    throw Object.assign(new Error('这个 tag 没有可用的 Jira 号，请用 --jira 指定'), { code: 'NO_JIRA' });
  }

  const jira = await client.queryJiraPMAndName(args.env, jiraId);
  if (!jira || !jira.pmName) {
    throw Object.assign(
      new Error(`取不到 ${jiraId} 的产品负责人（需为故事/线上缺陷且负责人可用）`),
      { code: 'JIRA_UNAVAILABLE' },
    );
  }
  const status = await client.checkJiraID(args.env, args.app, jiraId);
  if (!status.apiStatus) throw new Error('Jira 接口不可用，稍后重试');
  if (!status.canDeploy) throw new Error(`Jira ${jiraId} 状态不可部署（需已完成/已关闭）`);
  const check = await client.checkCanApprovalByJiraID(args.env, args.app, jiraId);
  const reasons = (check && check.JiraIDs) || [];
  if (reasons.length) {
    const text = reasons
      .map((item) => {
        if (item.Reason === 0) return `没有以 ${item.JiraIDs} 为前缀的分支`;
        if (item.Reason === 1) return `已用 ${item.JiraIDs} 部署过 online`;
        if (item.Reason === 2) return `正在用 ${item.JiraIDs} 部署中`;
        return `${item.JiraIDs} 校验未通过`;
      })
      .join('；');
    throw Object.assign(new Error(text), { code: 'PRECHECK_FAILED' });
  }

  const input = {
    appID: args.app,
    appCommitID: image.id,
    jiraID: jiraId,
    detail: [
      { name: '需求名称', value: jira.jiraName },
      { name: '产品负责人', value: jira.pmName },
      { name: '冒烟case是否通过', value: args.smoke },
      { name: '是否配置健康检查', value: args.health },
      { name: 'DB变动', value: args.dbChange },
    ],
  };
  log(`审批表单：需求「${jira.jiraName}」，负责人 ${jira.pmName}，冒烟 ${args.smoke}，健康检查 ${args.health}，DB ${args.dbChange}`);
  if (args.dryRun) {
    log('[dry-run] 不会提交审批');
    return { record: existing, submitted: false, jiraId, input };
  }
  const approvalId = await client.startDeployApproval(args.env, input);
  log(`已发起审批：${approvalId}`);
  return { record: null, submitted: true, jiraId, approvalId };
}

/** 审批结果：通过返回 record，驳回抛错 */
async function waitApproval(client, args, image, approvalId) {
  const deadline = Date.now() + args.approveMinutes * 60 * 1000;
  let last = '';
  while (Date.now() < deadline) {
    const record = await findApproval(client, args, image.id);
    const state = normalizeApproval(record);
    const text = record ? `${record.status || '-'} / ${record.result || '-'}` : '暂无记录';
    if (text !== last) {
      log(`[等审批] ${text}`);
      last = text;
    }
    if (state === 'passed') return record;
    if (state === 'rejected') {
      throw Object.assign(new Error(`审批被驳回：${text}`), { code: 'APPROVAL_REJECTED' });
    }
    await sleep(args.pollMs);
  }
  throw Object.assign(new Error(`审批 ${args.approveMinutes} 分钟仍未通过（审批ID ${approvalId || '-'}）`), {
    code: 'APPROVAL_TIMEOUT',
  });
}

/** 报错是不是「部署包被别的部署任务占着」 */
function isPackageBusy(error) {
  return DEPLOY_BUSY_PATTERN.test(String((error && error.message) || ''));
}

/** 从发布记录里找出占着这个部署包的任务，提示去催谁 */
async function findPackageHolder(client, args) {
  try {
    const rows = await client.getDeployHistory(args.env, args.app, { itemsPerPage: 20 });
    const holder = rows.find(
      (row) =>
        row.id &&
        !['done', 'cancel'].includes(String(row.status)) &&
        (!row.deploymentConfigName || row.deploymentConfigName === args.packageName),
    );
    if (!holder) return '';
    const branch = (holder.gitlabCommit && holder.gitlabCommit.branch) || '-';
    return `（占用者：${holder.operator || '未知操作人'}，${holder.jiraID || '-'} / 任务 ${holder.id} / ${branch}）`;
  } catch {
    return '';
  }
}

/** 问用户：部署包被别人占着，是等它释放还是就此停下。非交互环境没法问，直接停。 */
async function askWaitOrExit(args, holder) {
  if (args.onDeployBusy === 'wait') return 'wait';
  if (args.onDeployBusy === 'exit') return 'exit';
  if (!process.stdin.isTTY) return 'exit';
  const rl = createInterface({ input: process.stdin, output: process.stderr });
  const answer = await new Promise((resolve) =>
    rl.question(`部署包 ${args.packageName} 被占用${holder}，等它释放后自动重试吗？(Y/n) `, resolve),
  );
  rl.close();
  return /^n/i.test(answer.trim()) ? 'exit' : 'wait';
}

/** 提交部署；部署包被占用时先问人，选了等才按固定节奏重试，最多等 --wait-minutes */
async function submitDeploy(client, args, input) {
  const deadline = Date.now() + args.waitMinutes * 60 * 1000;
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await client.startDeploy(args.env, input);
    } catch (error) {
      if (!isPackageBusy(error)) throw error;
      const holder = await findPackageHolder(client, args);
      if (attempt === 1) {
        const choice = await askWaitOrExit(args, holder);
        if (choice === 'exit') {
          throw Object.assign(
            new Error(
              `部署包 ${args.packageName} 被占用${holder}，已停止部署。` +
                '镜像和审批都已就绪，等它释放后用 --on-deploy-busy wait 重跑即可',
            ),
            { code: 'DEPLOY_BUSY', details: holder },
          );
        }
      }
      const left = deadline - Date.now();
      if (left <= 0) {
        throw Object.assign(
          new Error(`部署包 ${args.packageName} 被占用，等了 ${args.waitMinutes} 分钟仍未释放${holder}`),
          { code: 'DEPLOY_BUSY_TIMEOUT', details: holder },
        );
      }
      const wait = Math.min(DEPLOY_BUSY_RETRY_MS, left);
      log(`[等部署包] ${args.packageName} 有正在执行的部署任务${holder}，${Math.ceil(wait / 1000)}s 后重试（第 ${attempt} 次）`);
      await sleep(wait);
    }
  }
}

/** 任务建出来会停在「预览实例部署」等人选灰度/快速，这里替人点下去 */
async function startPreviewDeploy(client, args, taskId) {
  const label = args.deployMode === 'fast' ? '快速部署' : '灰度部署';
  const input = {
    taskID: taskId,
    deployType: args.deployMode,
    cluster: args.previewCluster,
    section: 'preview',
    value: '0',
    action: 'deploy_new_version',
  };
  log(`[${label}] 触发预览实例部署（task ${taskId}，cluster ${args.previewCluster}）`);
  let lastError;
  // 任务刚建出来时偶发「任务不存在/未就绪」，给几次重试
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try {
      const section = await client.updateDeploySection(args.env, input);
      log(`[${label}] 已触发：${section.section || '-'} / ${section.eventName || '-'} / ${section.taskStatus || '-'}`);
      return section;
    } catch (error) {
      lastError = error;
      log(`[${label}] 第 ${attempt} 次触发失败：${error.message}`);
      await sleep(Math.max(args.pollMs, 3000));
    }
  }
  throw Object.assign(new Error(`触发${label}失败：${(lastError && lastError.message) || '未知错误'}`), {
    code: 'SECTION_DEPLOY_FAILED',
  });
}

/** 等预览实例把资源和流量走完；副本是否就绪仍需在任务页确认 */
async function waitPreviewDeploy(client, args, taskId) {
  const deadline = Date.now() + args.waitMinutes * 60 * 1000;
  let last = '';
  while (Date.now() < deadline) {
    const section = await client.getDeploySection(args.env, taskId);
    const text = `${section.section || '-'} / ${section.eventName || '-'} / deploy=${section.deploymentStatus || '-'} traffic=${section.trafficStatus || '-'}`;
    if (text !== last) {
      log(`[等部署] ${text}`);
      last = text;
    }
    if (['done', 'cancel'].includes(String(section.taskStatus))) return section;
    if (section.section === 'preview' && section.deploymentStatus === 'done' && section.trafficStatus === 'done') {
      return section;
    }
    await sleep(args.pollMs);
  }
  throw Object.assign(new Error(`预览实例部署 ${args.waitMinutes} 分钟仍未完成（task ${taskId}）`), {
    code: 'SECTION_DEPLOY_TIMEOUT',
  });
}

/**
 * 取消本分支上一次发布遗留的「发布中」任务。
 * 只认 tag 形如 <branch>-<时间戳>-online 的任务——别人分支/别人 Jira 的一律不动，
 * 也不碰已经 done / cancel 的任务。取消要等平台真正释放（部署包被占着时新任务起不来）。
 */
async function cancelPreviousReleases(client, args, branch) {
  const rows = await client.getDeployHistory(args.env, args.app, { itemsPerPage: 20 });
  const targets = rows.filter((row) => {
    if (!row.id || ['done', 'cancel'].includes(String(row.status))) return false;
    const tag = (row.gitlabCommit && (row.gitlabCommit.tag || row.gitlabCommit.branch)) || '';
    return isBranchReleaseTag(tag, branch);
  });
  if (!targets.length) return [];

  const canceled = [];
  for (const row of targets) {
    const tag = (row.gitlabCommit && (row.gitlabCommit.tag || row.gitlabCommit.branch)) || '';
    if (args.dryRun) {
      log(`[dry-run] 将取消本分支上次发布的任务 ${row.id}（${tag}）`);
      canceled.push({ taskId: row.id, tag, dryRun: true });
      continue;
    }

    const section = await client.getDeploySection(args.env, row.id);
    if (['done', 'cancel'].includes(String(section.taskStatus))) continue;
    // 只取消还停在预览段的旧任务；已经推进到生产段的不能随手取消（可能已切流量）
    if (section.section && section.section !== 'preview') {
      log(
        `[警告] 任务 ${row.id}（${tag}）已推进到 ${section.section} 段，跳过自动取消；` +
          '如确认要停，请到任务页人工处理',
      );
      continue;
    }

    log(`取消本分支上次发布的任务 ${row.id}（${tag}）…`);
    try {
      await client.updateDeploySection(args.env, {
        taskID: Number(row.id),
        deployType: section.sectionDeployType || args.deployMode,
        cluster: args.previewCluster,
        section: section.section || 'preview',
        value: '0',
        action: 'cancel',
      });
    } catch (error) {
      throw Object.assign(
        new Error(`取消任务 ${row.id}（${tag}）失败：${String(error.message).slice(0, 200)}`),
        { code: 'CANCEL_FAILED', details: { taskId: row.id, tag } },
      );
    }
    await waitTaskCanceled(client, args, row.id);
    log(`已取消任务 ${row.id}（${tag}）`);
    canceled.push({ taskId: row.id, tag });
  }
  return canceled;
}

/** 取消是异步的，等任务真正离开发布中，避免紧接着 deploy2 又撞上部署包被占用 */
async function waitTaskCanceled(client, args, taskId) {
  const deadline = Date.now() + Math.min(args.waitMinutes, 5) * 60 * 1000;
  let last = '';
  while (Date.now() < deadline) {
    const section = await client.getDeploySection(args.env, taskId);
    const status = String(section.taskStatus || '');
    if (status !== last) {
      log(`[等取消] 任务 ${taskId} 状态：${status || '-'}`);
      last = status;
    }
    if (['cancel', 'done'].includes(status)) return section;
    await sleep(args.pollMs);
  }
  throw Object.assign(
    new Error(`取消任务 ${taskId} 后 5 分钟内没有生效，部署包可能仍被占用`),
    { code: 'CANCEL_TIMEOUT' },
  );
}

async function deploy(client, args, image, jiraId) {
  const packages = await client.listDeployPackages(args.env, args.app);
  const pkg = packages.find((item) => item.name === args.packageName);
  if (!pkg) throw new Error(`没找到名为 ${args.packageName} 的部署包`);
  const input = {
    appID: args.app,
    appCommitID: image.id,
    deploymentConfigID: pkg.id,
    jiraID: jiraId,
  };
  const repeat = await client.checkRepeatCommitByJira(args.env, input);
  // 同一分支重发时，历史里必然有本分支旧的发布记录；只把别的分支/别的 Jira 的重复算冲突
  const branch = currentBranch(args.branch);
  const conflicts = (Array.isArray(repeat) ? repeat : []).filter(
    (item) => !mentionsBranchReleaseTag(item, branch),
  );
  if (conflicts.length) {
    throw new Error(`该部署包已用 ${jiraId} 部署过其它代码版本：${conflicts.join(',')}`);
  }
  if (args.dryRun) {
    log(`[dry-run] 将部署 ${pkg.name}(${pkg.id})，不会提交${input.deploymentConfigID ? '' : ''}`);
    return null;
  }
  const task = await submitDeploy(client, args, input);
  if (!task || !task.taskId) throw new Error('部署接口没有返回任务ID');
  const url = `${args.baseUrl}/app/application/${args.app}/deploy/deploytask/${task.taskId}?env=${args.env}`;
  log(`已提交部署任务：${task.taskId}\n${url}`);
  return task;
}

async function printStatus(client, args) {
  const tag = args.tag;
  const { nodes } = await client.listDeployImages(args.env, args.app, { itemsPerPage: 100 });
  const candidates = tag
    ? nodes.filter((node) => node.gitlabCommit && node.gitlabCommit.tag === tag)
    : nodes.filter((node) => matchesJiraTag(node.gitlabCommit && node.gitlabCommit.tag, args.jira));
  const image = candidates[0] || null;
  const record = image ? await findApproval(client, args, image.id) : null;
  return {
    ok: true,
    app: args.app,
    env: args.env,
    tag: image ? image.gitlabCommit.tag : null,
    appCommitID: image ? image.id : null,
    deploymentStatus: image
      ? {
          deployedToCluster: Boolean(image.lastDeployHistory && image.lastDeployHistory.createdAt),
          cluster: (image.lastDeployHistory && image.lastDeployHistory.cluster) || '',
        }
      : null,
    approvalStatus: image
      ? {
          raw: image.approvalStatus || '',
          label: PLATFORM_APPROVAL_LABEL[image.approvalStatus] || '无',
          deployable: isDeployableStatus(image.approvalStatus),
        }
      : null,
    approval: record
      ? {
          state: normalizeApproval(record),
          approvalID: record.approvalID,
          jiraID: record.jiraID,
          status: record.status,
          result: record.result,
        }
      : null,
    candidates: args.tag ? undefined : candidates.slice(0, 5).map((node) => node.gitlabCommit.tag),
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const cookie = await resolveCookie({ base: args.baseUrl });
  const client = createClient({ base: args.baseUrl, cookie });

  if (args.status) {
    const summary = await printStatus(client, args);
    const label = summary.approvalStatus ? summary.approvalStatus.label : '无';
    const record = summary.approval ? `${summary.approval.status}/${summary.approval.result}` : '无记录';
    log(`tag：${summary.tag || '未找到'}｜镜像审批状态：${label}（${record}）｜线上部署：${summary.deploymentStatus && summary.deploymentStatus.deployedToCluster ? summary.deploymentStatus.cluster || '已部署' : '未部署'}`);
    process.stdout.write(`${JSON.stringify(summary)}\n`);
    return;
  }

  // 重发判定：本分支上次发布之后没有新的非合并提交就不必再发一遍
  const branch = currentBranch(args.branch);
  const previousTag = findPreviousReleaseTag(branch);
  if (!args.force && !args.tag && previousTag && !hasNewCommitsSince(previousTag)) {
    log(`自上次发布 ${previousTag} 以来没有新提交，跳过本次发布（确需重发加 --force）`);
    process.stdout.write(
      `${JSON.stringify({ ok: true, skipped: true, reason: 'NO_CHANGES', branch, previousTag })}\n`,
    );
    return;
  }
  if (previousTag && !args.tag) log(`上次发布的 tag：${previousTag}`);

  // 发布前体检：先看平台上有没有「发布中」但没合 master 的任务
  const inProgress = await precheckInProgress(client, args, branch);

  const repoResult = args.tag
    ? { tag: args.tag, mr: { skipped: true, reason: 'tag-only' } }
    : await createTag(args);
  const tag = repoResult.tag;
  // dry-run 且没给现成 tag：tag 根本没推，没必要再等它构建
  if (args.dryRun && !args.tag) {
    log('这是演练：tag 未推送，流程到此为止。真实发布去掉 --dry-run。');
    process.stdout.write(
      `${JSON.stringify({ ok: true, dryRun: true, wouldTag: tag, app: args.app, env: args.env })}\n`,
    );
    return;
  }
  log(`目标 tag：${tag}`);

  log('等待镜像出现在可部署版本…');
  const image = await findImage(client, args, tag);
  log(`镜像就绪：appCommitID=${image.id}，审批状态=${image.approvalStatus || '无'}`);

  const approved = await ensureApproved(client, args, image);
  let record = approved.record;
  if (approved.submitted) {
    record = await waitApproval(client, args, image, approved.approvalId);
  } else if (normalizeApproval(record) === 'ongoing') {
    log('审批正在进行，等待结果…');
    record = await waitApproval(client, args, image, record && record.approvalID);
  }

  const jiraId = approved.jiraId || (record && record.jiraID) || args.jira;
  const summary = {
    ok: true,
    app: args.app,
    env: args.env,
    tag,
    appCommitID: image.id,
    jiraId,
    approval: record
      ? { approvalID: record.approvalID, status: record.status, result: record.result }
      : null,
    submittedApproval: approved.submitted,
    mr: repoResult.mr,
    canceled: [],
    deployed: null,
    deploySkipped: false,
  };

  if (args.stopBeforeDeploy) {
    log('已按 --stop-before-deploy 停在部署前（未取消本分支上次发布）');
    summary.deploySkipped = true;
  } else {
    // 新版本已构建且审批通过，才取消本分支上次的发布：先放掉部署包，再建新任务
    summary.canceled = await cancelPreviousReleases(client, args, branch);
    const task = await deploy(client, args, image, jiraId);
    summary.deployed = task ? { taskId: task.taskId, taskType: task.taskType } : null;
    if (args.dryRun) summary.deploySkipped = true;
    if (task && args.deployMode !== 'manual') {
      await startPreviewDeploy(client, args, task.taskId);
      const section = await waitPreviewDeploy(client, args, task.taskId);
      summary.sectionDeploy = {
        deployType: args.deployMode,
        section: section.section || '',
        taskStatus: section.taskStatus || '',
        deploymentStatus: section.deploymentStatus || '',
        trafficStatus: section.trafficStatus || '',
      };
      log('预览实例已更新（副本是否就绪请在任务页确认；任务关闭前会一直占用部署包）');
    }
  }

  process.stdout.write(`${JSON.stringify(summary)}\n`);
}

export { buildMrTitle, buildMrBody, ensureMr };

// 只在直接执行时跑主流程，被 import 用于校验 MR 文案时不触发发布
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    const auth = error instanceof AuthError;
    const code = error.code || (auth ? 'AUTH_REQUIRED' : 'FAILED');
    process.stderr.write(`\n失败：${error.message}\n`);
    if (auth) {
      process.stderr.write(
        '登录态不可用：确认 guazi-sso-login skill 已初始化（它会用缓存的账号密码换 GUAZISSO），或设置 MEDUSA_COOKIE="GUAZISSO=xxx"\n',
      );
    }
    process.stdout.write(
      `${JSON.stringify({ ok: false, code, message: error.message, ...(error.details ? { details: error.details } : {}) })}\n`,
    );
    process.exit(auth ? 2 : 1);
  });
}
