#!/usr/bin/env node
/**
 * 把发布分支合进 master：创建 MR（已存在就复用）并合并，幂等。
 *
 * 对应 gton 的前置要求「MR 已合入 master」——所以它应该在打 tag 之前跑。
 * 默认只做预演（打印将要发生什么），真正执行需要 --yes。
 *
 * 例：
 *   node mr-master.mjs                          # 预演当前分支 -> master
 *   node mr-master.mjs --yes                    # 真建 MR 并合并
 *   node mr-master.mjs --jira OS-43558 --yes    # 指定分支
 *   node mr-master.mjs --no-merge --yes         # 只建 MR，合并留给评审
 */
import { execFileSync } from 'node:child_process';

const DEFAULTS = { target: 'master', title: '', body: '', source: '' };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const log = (message) => process.stderr.write(`${message}\n`);

function parseArgs(argv) {
  const args = {
    ...DEFAULTS,
    jira: '',
    yes: false,
    merge: true,
    syncMaster: true,
    pollMs: 10000,
    mergeTimeoutMinutes: 30,
  };
  const valueFlags = {
    '--source': 'source',
    '--jira': 'jira',
    '--target': 'target',
    '--title': 'title',
    '--body': 'body',
    '--poll-ms': 'pollMs',
    '--merge-timeout-minutes': 'mergeTimeoutMinutes',
  };
  const boolFlags = { '--yes': 'yes', '--no-merge': 'merge', '--no-sync-master': 'syncMaster' };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === '--no-merge') {
      args.merge = false;
    } else if (key === '--no-sync-master') {
      args.syncMaster = false;
    } else if (boolFlags[key]) {
      args[boolFlags[key]] = true;
    } else if (valueFlags[key]) {
      if (argv[i + 1] === undefined) throw new Error(`${key} 缺少取值`);
      args[valueFlags[key]] = argv[i + 1];
      i += 1;
    } else {
      throw new Error(`未知参数：${key}`);
    }
  }
  args.pollMs = Number(args.pollMs);
  args.mergeTimeoutMinutes = Number(args.mergeTimeoutMinutes);
  return args;
}

function git(args, { allowFail = false } = {}) {
  try {
    return execFileSync('git', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (error) {
    if (allowFail) return '';
    throw Object.assign(new Error(String(error.stderr || error.message).trim()), { code: 'GIT_FAILED' });
  }
}

function glab(endpoint, { method = 'GET', fields = {} } = {}) {
  const argv = ['api', endpoint];
  if (method !== 'GET') argv.push('--method', method);
  Object.entries(fields).forEach(([key, value]) => {
    argv.push('--field', `${key}=${value}`);
  });
  const out = execFileSync('glab', argv, { encoding: 'utf8' });
  return out.trim() ? JSON.parse(out) : null;
}

function currentBranch(explicit) {
  if (explicit) return explicit;
  const branch = git(['rev-parse', '--abbrev-ref', 'HEAD']);
  if (branch === 'HEAD') throw new Error('当前是 detached HEAD，请用 --source 指定分支');
  return branch;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const source = args.jira && !args.source ? args.jira : currentBranch(args.source);

  log('同步远端引用…');
  git(['fetch', 'origin', '--tags', '--prune', '--quiet']);

  // 一律以本地 HEAD 为准：master 要合进的是当前工作区这条分支，MR 读的却是远端
  const revCount = (from, to) => Number(git(['rev-list', '--count', `${from}..${to}`]));
  const measure = () => ({
    ahead: revCount(`origin/${args.target}`, 'HEAD'),
    behind: revCount('HEAD', `origin/${args.target}`),
    unpushed: revCount(`origin/${source}`, 'HEAD'),
  });
  let stats = measure();
  log(
    `当前分支 ${source} 相对 origin/${args.target}：领先 ${stats.ahead}，落后 ${stats.behind}` +
      (stats.unpushed ? `（另有 ${stats.unpushed} 个提交未推送）` : ''),
  );

  // 发布前的固定动作：先把 master 合进当前分支，再把这条分支推上去
  let synced = false;
  let pushed = false;
  if (args.syncMaster && stats.behind > 0) {
    if (!args.yes) {
      log(`[计划] 把 origin/${args.target} 合进 ${source}（落后 ${stats.behind} 个提交）并推送`);
    } else {
      const dirty = git(['status', '--porcelain']);
      if (dirty) {
        throw Object.assign(
          new Error(`工作区有未提交改动，先提交或 stash 再同步 ${args.target}：\n${dirty.slice(0, 400)}`),
          { code: 'DIRTY_WORKTREE' },
        );
      }
      log(`合并 origin/${args.target} 到 ${source} …`);
      try {
        git(['merge', `origin/${args.target}`, '--no-edit']);
      } catch (error) {
        const conflicts = git(['diff', '--name-only', '--diff-filter=U'], { allowFail: true });
        git(['merge', '--abort'], { allowFail: true });
        throw Object.assign(
          new Error(`合并 ${args.target} 有冲突，已回滚本次合并，请手工解决后重来：\n${conflicts || error.message}`),
          { code: 'MERGE_CONFLICT' },
        );
      }
      synced = true;
      log('已合并 master，准备推送');
    }
  }

  if (args.yes && (synced || stats.unpushed > 0 || stats.behind > 0)) {
    git(['push', 'origin', `HEAD:${source}`]);
    pushed = true;
    log(`已推送 ${source} 到 origin`);
  } else if (!args.yes && stats.unpushed > 0) {
    log(`[计划] 推送 ${source}（${stats.unpushed} 个提交未推送）`);
  }

  if (args.yes) {
    stats = measure();
    if (stats.behind > 0 && !args.syncMaster) {
      log(`提示：${source} 仍落后 origin/${args.target} ${stats.behind} 个提交（--no-sync-master）`);
    }
  }
  const { ahead, behind } = args.yes
    ? stats
    : { ahead: stats.ahead, behind: stats.behind };

  if (ahead === 0 && behind === 0 && stats.unpushed === 0) {
    log('没有需要合并的提交，结束');
    process.stdout.write(`${JSON.stringify({ ok: true, action: 'nothing-to-merge', source, target: args.target })}\n`);
    return;
  }

  const existing = glab(
    `projects/:id/merge_requests?source_branch=${encodeURIComponent(source)}&target_branch=${encodeURIComponent(args.target)}&state=all&per_page=20`,
  );
  let mr = existing.find((item) => item.state === 'opened') || existing.find((item) => item.state === 'merged');
  if (mr && mr.state === 'merged') {
    log(`已有合并完成的 MR !${mr.iid}，无需再动`);
    process.stdout.write(
      `${JSON.stringify({ ok: true, action: 'already-merged', iid: mr.iid, url: mr.web_url, source, target: args.target })}\n`,
    );
    return;
  }

  const title = args.title || `chore(release): 合并 ${source} 到 ${args.target}`;
  const plan = {
    source,
    target: args.target,
    ahead,
    behind,
    syncedMaster: synced,
    pushed,
    willSyncMaster: behind > 0 && args.syncMaster,
    title,
    existingMr: mr ? `!${mr.iid}` : null,
  };

  if (!args.yes) {
    log('预演模式（未加 --yes）：将创建 MR 并合并。确认无误后加 --yes 执行。');
    if (mr) log(`  复用已有 MR !${mr.iid}：${mr.web_url}`);
    else log(`  新建 MR：${title}`);
    if (args.merge) log('  然后合并到 ' + args.target);
    process.stdout.write(`${JSON.stringify({ ok: true, dryRun: true, ...plan })}\n`);
    return;
  }

  if (!mr) {
    mr = glab('projects/:id/merge_requests', {
      method: 'POST',
      fields: {
        source_branch: source,
        target_branch: args.target,
        title,
        description: args.body || `自动创建的发布合并：${source} → ${args.target}`,
        remove_source_branch: 'false',
      },
    });
    log(`已创建 MR !${mr.iid}：${mr.web_url}`);
  } else {
    log(`复用已有 MR !${mr.iid}：${mr.web_url}`);
  }

  if (!args.merge) {
    process.stdout.write(
      `${JSON.stringify({ ok: true, action: 'created', iid: mr.iid, url: mr.web_url, source, target: args.target })}\n`,
    );
    return;
  }

  // 等待可合并状态：冲突直接失败，pipeline 需要则用 auto-merge
  const deadline = Date.now() + args.mergeTimeoutMinutes * 60 * 1000;
  let last = '';
  while (Date.now() < deadline) {
    const detail = glab(`projects/:id/merge_requests/${mr.iid}`);
    // 新版 GitLab 用 detailed_merge_status，老版只有 merge_status（can_be_merged/…），两种都要认
    const status = detail.detailed_merge_status || detail.merge_status || '';
    if (status !== last) {
      log(`合并状态：${status}`);
      last = status;
    }
    if (detail.has_conflicts || ['conflict', 'cannot_be_merged'].includes(status)) {
      throw Object.assign(new Error(`MR !${mr.iid} 存在冲突，请先解决：${mr.web_url}`), { code: 'MR_CONFLICT' });
    }
    if (['mergeable', 'can_be_merged'].includes(status)) break;
    if (['need_rebase', 'not_approved', 'blocked_status', 'ci_must_pass'].includes(status)) {
      throw Object.assign(new Error(`MR !${mr.iid} 当前不可合并：${status}（${mr.web_url}）`), {
        code: 'MR_NOT_MERGEABLE',
      });
    }
    await sleep(args.pollMs);
  }

  const merged = glab(`projects/:id/merge_requests/${mr.iid}/merge`, {
    method: 'PUT',
    fields: { merge_when_pipeline_succeeds: 'false' },
  });

  const state = merged && merged.state ? merged.state : 'merged';
  log(`MR !${mr.iid} 状态：${state}`);
  process.stdout.write(
    `${JSON.stringify({ ok: true, action: 'merged', iid: mr.iid, url: mr.web_url, state, source, target: args.target })}\n`,
  );
}

main().catch((error) => {
  log(`\n失败：${error.message}`);
  process.stdout.write(
    `${JSON.stringify({ ok: false, code: error.code || 'FAILED', message: error.message })}\n`,
  );
  process.exit(1);
});
