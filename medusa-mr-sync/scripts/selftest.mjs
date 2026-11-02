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
} from './rules.mjs';

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

console.log('selftest passed');
