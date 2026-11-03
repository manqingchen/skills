#!/usr/bin/env node
/**
 * 自测：管線顺序 / bail 语义 / 共享判定规则。
 * 用法：node scripts/selftest.mjs
 */
import assert from 'node:assert/strict';
import { GATE_ORDER, runGates } from './pipeline.mjs';
import {
  releaseState,
  classifyDeployStage,
  analyzeMergeCommits,
  mergeRuleMessage,
  sortByCreatedAtDesc,
  stageBucket,
  isIntegrationBranch,
  decideOpenedMrs,
} from './rules.mjs';
import {
  normalizeRepoPath,
  extractList,
  pickBindingId,
  pickMemberUserId,
  resolveAsUserFromMembers,
  extractMrResult,
  parseJsonOutput,
} from './multica-mr.mjs';
import { collectMedusaApps, mergeAppRows, matchAppByRepoPath } from './patrol.mjs';

/* ------------------------------ 管線 ------------------------------ */

assert.deepEqual(GATE_ORDER, [
  'deploy-stage',
  'release-state',
  'refresh-refs',
  'resolve-commit',
  'already-merged',
  'source-branch',
  'payload-merge',
  'opened-mr',
  'merged-mr',
  'squash-check',
  'write',
]);

function makeGates(overrides = {}) {
  const calls = [];
  const gates = {};
  for (const name of GATE_ORDER) {
    gates[name] = async () => {
      calls.push(name);
      return { action: 'continue' };
    };
  }
  gates.write = async () => {
    calls.push('write');
    return { action: 'done', outcome: 'written' };
  };
  Object.entries(overrides).forEach(([name, fn]) => {
    gates[name] = async (ctx) => {
      calls.push(name);
      return fn(ctx);
    };
  });
  return { gates, calls };
}

// 全通过：按固定顺序执行到 write
{
  const { gates, calls } = makeGates();
  const result = await runGates({}, gates);
  assert.equal(result.action, 'done');
  assert.equal(result.stoppedAt, 'write');
  assert.deepEqual(calls, GATE_ORDER);
}

// bail：停在该门禁，后续（包括 write）不执行
{
  const { gates, calls } = makeGates({
    'payload-merge': () => ({ action: 'bail', code: 'PAYLOAD_MERGE_COMMIT', message: 'blocked' }),
  });
  const result = await runGates({}, gates);
  assert.equal(result.action, 'bail');
  assert.equal(result.code, 'PAYLOAD_MERGE_COMMIT');
  assert.equal(result.stoppedAt, 'payload-merge');
  assert.deepEqual(calls, GATE_ORDER.slice(0, GATE_ORDER.indexOf('payload-merge') + 1));
  assert.equal(calls.includes('write'), false);
}

// done：提前结束，后续不执行
{
  const { gates, calls } = makeGates({
    'already-merged': () => ({ action: 'done', outcome: 'already-merged' }),
  });
  const result = await runGates({}, gates);
  assert.equal(result.action, 'done');
  assert.equal(result.stoppedAt, 'already-merged');
  assert.equal(calls.includes('write'), false);
  assert.equal(calls.includes('source-branch'), false);
}

// 缺少门禁实现：直接报错，不存在“跳过”
await assert.rejects(async () => runGates({}, {}), /管線缺少门禁实现/);

// 未知 action：直接报错
{
  const { gates } = makeGates({ 'deploy-stage': () => ({ action: 'skip' }) });
  await assert.rejects(async () => runGates({}, gates), /未知 action/);
}

// 末尾没有产出：直接报错
{
  const gates = {};
  for (const name of GATE_ORDER) gates[name] = async () => ({ action: 'continue' });
  await assert.rejects(async () => runGates({}, gates), /没有 write 门禁产出结果/);
}

/* ------------------------------ 规则 ------------------------------ */

assert.equal(releaseState('done'), 'done');
assert.equal(releaseState('cancel'), 'canceled');
assert.equal(releaseState('running'), 'running');
assert.equal(releaseState('create'), 'running');
assert.equal(releaseState(''), 'running');

const APP = 'global-station';
const ev = (reason, createdAt) => ({ reason, createdAt });
const preview = [
  ev('Deployment: global-station-deploy-ixknv:请求创建资源:部署新实例', '2026-10-30T17:51:07+08:00'),
  ev('VirtualService: global-station-deploy-preview:请求创建资源:部署新实例', '2026-10-30T17:51:07+08:00'),
  ev('Deployment: global-station-deploy-ixknv:资源创建成功', '2026-10-30T17:51:07+08:00'),
  ev('VirtualService: global-station-deploy-preview:资源创建成功', '2026-10-30T17:51:11+08:00'),
];
let stage = classifyDeployStage({ events: preview, appName: APP, taskId: 1 });
assert.equal(stage.verdict, 'preview-only');
assert.equal(stage.productionDeployedAt, '');

const production = [
  ...preview,
  ev('Deployment: global-station-deploy-pkltj:请求创建资源:部署新实例', '2026-10-30T20:54:34+08:00'),
  ev('VirtualService: global-station-deploy:请求创建资源:部署新实例', '2026-10-30T20:54:34+08:00'),
  ev('Deployment: global-station-deploy-pkltj:资源创建成功', '2026-10-30T20:54:43+08:00'),
  ev('VirtualService: global-station-deploy:资源创建成功', '2026-10-30T20:54:43+08:00'),
  ev('Deployment: global-station-deploy-pkltj:请求变更实例流量配置:权重100%', '2026-10-30T21:06:47+08:00'),
];
stage = classifyDeployStage({ events: production, appName: APP, taskId: 2 });
assert.equal(stage.verdict, 'production');
assert.equal(stage.trafficSwitchedAt, '2026-10-30T21:06:47+08:00');

// 预览 Deployment 上的权重事件不能算生产切流
stage = classifyDeployStage({
  events: [...preview, ev('Deployment: global-station-deploy-ixknv:请求变更实例流量配置:权重100%', '2026-10-30T17:52:00+08:00')],
  appName: APP,
  taskId: 3,
});
assert.equal(stage.verdict, 'preview-only');
assert.equal(stage.trafficSwitchedAt, '');

// 只发起生产部署但没有成功事件
stage = classifyDeployStage({
  events: [
    ...preview,
    ev('Deployment: global-station-deploy-zzzzz:请求创建资源:部署新实例', '2026-10-30T22:00:00+08:00'),
    ev('VirtualService: global-station-deploy:请求创建资源:部署新实例', '2026-10-30T22:00:00+08:00'),
  ],
  appName: APP,
  taskId: 4,
});
assert.equal(stage.verdict, 'unknown');
assert.equal(stage.reason, 'PRODUCTION_REQUESTED_ONLY');

stage = classifyDeployStage({ events: [], appName: APP, taskId: 5 });
assert.equal(stage.reason, 'NO_EVENTS');

const mergeAnalysis = await analyzeMergeCommits(
  [
    { id: 'b', parents: ['b1', 'foreign'], subject: 'Merge foreign' },
    { id: 'a', parents: ['a1', 'masterParent'], subject: 'Merge master' },
  ],
  async (parent) => ({ masterParent: true, foreign: false })[parent] ?? null,
);
assert.equal(mergeAnalysis.ok, false);
assert.deepEqual(mergeAnalysis.foreign.map((item) => item.parent), ['foreign']);
assert.match(mergeRuleMessage({ source: 'OS-1', target: 'master', analysis: mergeAnalysis }), /master\.\.OS-1/);

const allowed = await analyzeMergeCommits(
  [{ id: 'a', parents: ['a1', 'masterParent'], subject: 'Merge master' }],
  async () => true,
);
assert.equal(allowed.ok, true);

assert.equal(stageBucket('preview-only'), 'previewOnly');
assert.equal(stageBucket('not-applicable'), 'notApplicable');
assert.equal(stageBucket('production'), 'production');
assert.equal(stageBucket('unknown'), 'unknown');
assert.equal(isIntegrationBranch('develop'), true);
assert.equal(isIntegrationBranch('online3'), true);
assert.equal(isIntegrationBranch('OS-43595'), false);

assert.deepEqual(
  sortByCreatedAtDesc([
    { id: 1, createdAt: '2026-10-30T14:00:00+08:00' },
    { id: 3, createdAt: '2026-10-30T20:00:00+08:00' },
    { id: 2, createdAt: '2026-10-30T20:00:00+08:00' },
  ]).map((item) => item.id),
  [3, 2, 1],
);

/* --------------------------- multica 写入辅助 --------------------------- */

assert.equal(normalizeRepoPath('https://git.guazi-corp.com/c2b-fe/global-station.git'), 'c2b-fe/global-station');
assert.equal(normalizeRepoPath('git@git.guazi-corp.com:c2b-fe/global-station.git'), 'c2b-fe/global-station');
assert.equal(normalizeRepoPath('/c2b-fe/global-station/'), 'c2b-fe/global-station');

assert.equal(extractList({ data: { repositories: [{ name: 'a' }] } }).length, 1);
assert.equal(extractList({ repos: [{ name: 'a' }, { name: 'b' }] }).length, 2);
assert.equal(extractList(null).length, 0);

assert.equal(pickBindingId({ project_binding_id: 'bind-1' }), 'bind-1');
assert.equal(pickBindingId({ projectBindingId: 'bind-2' }), 'bind-2');
assert.equal(pickBindingId({}), '');

// 成员记录同时有 id 和 user_id 时必须取 user_id
assert.equal(pickMemberUserId({ id: 'member-record-id', user_id: 'user-1' }), 'user-1');
assert.equal(pickMemberUserId({ user: { user_id: 'user-2' } }), 'user-2');

const members = [
  { id: 'm1', user_id: 'u1', username: 'zhangbohan1' },
  { id: 'm2', user_id: 'u2', name: '王浩岩', email: 'wanghaoyan1@guazi.com' },
];
assert.equal(resolveAsUserFromMembers(members, 'ZhangBohan1').userId, 'u1');
assert.equal(resolveAsUserFromMembers(members, 'wanghaoyan1@guazi.com').userId, 'u2');
assert.equal(resolveAsUserFromMembers(members, '王浩岩').userId, 'u2');
assert.equal(resolveAsUserFromMembers(members, 'u1').userId, 'u1');
// 显式 UUID 直接接受；匹配不到的普通名字必须为空，不能猜
assert.equal(resolveAsUserFromMembers(members, '11111111-2222-3333-4444-555555555555').userId, '11111111-2222-3333-4444-555555555555');
assert.equal(resolveAsUserFromMembers(members, 'nobody').userId, '');

assert.equal(parseJsonOutput('log line\n{"a":1}\n').a, 1);
assert.equal(parseJsonOutput('not json'), null);

const mrFromJson = extractMrResult({ merge_request: { iid: 42, web_url: 'https://git/merge_requests/42', state: 'opened' } });
assert.equal(mrFromJson.iid, '42');
assert.equal(mrFromJson.url, 'https://git/merge_requests/42');
assert.equal(mrFromJson.state, 'opened');

const mrFromText = extractMrResult('created https://git.guazi-corp.com/c2b-fe/csp/merge_requests/1689');
assert.equal(mrFromText.iid, '1689');
assert.equal(mrFromText.url, 'https://git.guazi-corp.com/c2b-fe/csp/merge_requests/1689');

/* --------------------- 美杜莎应用列表：翻页取全量 + 定向兜底 --------------------- */

// 三页取完（500/500/1）：必须翻到最后一页，排在尾部的 oversea/global-station 不能丢
{
  const pageOf = (prefix) => Array.from({ length: 500 }, (_, i) => ({ name: `${prefix}${i}`, namespace: 'n', gitlabPid: `n/${prefix}${i}` }));
  const pages = [pageOf('a'), pageOf('b'), [{ name: 'global-station', namespace: 'oversea', gitlabPid: 'oversea/global-station' }]];
  let calls = 0;
  const map = await collectMedusaApps(async (page) => { calls += 1; return pages[page - 1]; });
  assert.equal(calls, 3);
  assert.equal(map.size, 1001);
  assert.equal(map.get('oversea/global-station').appId, 'oversea/global-station');
}

// 单页不满：只请求一次就停
{
  let calls = 0;
  const map = await collectMedusaApps(async () => { calls += 1; return [{ namespace: 'fe', name: 'x', gitlabPid: 'fe/x' }]; });
  assert.equal(calls, 1);
  assert.equal(map.size, 1);
}

// 服务端忽略 page 参数、永远回同一整页：第二页没有新 key 必须停，不能死循环
{
  const samePage = Array.from({ length: 500 }, (_, i) => ({ name: `a${i}`, namespace: 'n', gitlabPid: `n/a${i}` }));
  let calls = 0;
  const map = await collectMedusaApps(async () => { calls += 1; return samePage; });
  assert.equal(calls, 2);
  assert.equal(map.size, 500);
}

// mergeAppRows：小写去重先到先得，跳过没有 gitlabPid 的行
{
  const map = mergeAppRows(new Map(), [
    { namespace: 'oversea', name: 'global-station', gitlabPid: 'Oversea/Global-Station' },
    { namespace: 'dup', name: 'dup', gitlabPid: 'oversea/global-station' },
    { namespace: 'n', name: 'empty' },
  ]);
  assert.equal(map.size, 1);
  assert.equal(map.get('oversea/global-station').appId, 'oversea/global-station');
}

// matchAppByRepoPath：服务端 appName 模糊过滤后，客户端必须做精确（大小写不敏感）匹配
{
  const rows = [
    { namespace: 'oversea', name: 'global-station', gitlabPid: 'oversea/global-station' },
    { namespace: 'oversea', name: 'global-station-seo', gitlabPid: 'oversea/global-station-seo' },
  ];
  assert.equal(matchAppByRepoPath(rows, 'oversea/global-station').appId, 'oversea/global-station');
  assert.equal(matchAppByRepoPath(rows, 'oversea/global-station-seo').gitlabPid, 'oversea/global-station-seo');
  assert.equal(matchAppByRepoPath(rows, 'oversea/global-station-x'), null);
  assert.equal(matchAppByRepoPath([], 'oversea/global-station'), null);
  assert.equal(
    matchAppByRepoPath([{ namespace: 'c2b', name: 'x', gitlabPid: 'C2B/X' }], 'c2b/x').appId,
    'c2b/x',
  );
}

/* ------------------- opened MR 处置：同目标非 Draft 复用合并 ------------------- */

// 同目标、非 Draft → 复用（用户自己建的 MR 直接自动合并）
{
  const { reuse, blockers } = decideOpenedMrs(
    [{ iid: 1941, target_branch: 'master', draft: false, web_url: 'https://git/mr/1941' }],
    'master',
  );
  assert.equal(reuse.iid, 1941);
  assert.equal(blockers.length, 0);
}

// Draft 不复用：不能替人合并未准备好的 MR
{
  const { reuse, blockers } = decideOpenedMrs(
    [{ iid: 1, target_branch: 'master', draft: true }],
    'master',
  );
  assert.equal(reuse, null);
  assert.equal(blockers.length, 1);
}

// 目标分支不同（如 → develop）不复用，仍上报
{
  const { reuse, blockers } = decideOpenedMrs(
    [{ iid: 2, target_branch: 'develop', draft: false }],
    'master',
  );
  assert.equal(reuse, null);
  assert.equal(blockers.length, 1);
}

// 混合场景：Draft + 异目标 + 同目标可复用 → 只复用同目标那条，其余进 blockers；多条同目标取 iid 最小
{
  const mrs = [
    { iid: 30, target_branch: 'master', draft: false },
    { iid: 20, target_branch: 'develop', draft: false },
    { iid: 10, target_branch: 'master', draft: true },
    { iid: 25, target_branch: 'master', draft: false },
  ];
  const { reuse, blockers } = decideOpenedMrs(mrs, 'master');
  assert.equal(reuse.iid, 25);
  assert.deepEqual(blockers.map((mr) => mr.iid).sort((a, b) => a - b), [10, 20, 30]);
}

// 空列表
assert.equal(decideOpenedMrs([], 'master').reuse, null);
assert.equal(decideOpenedMrs(null, 'master').blockers.length, 0);

console.log('selftest passed');
