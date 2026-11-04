#!/usr/bin/env node
/**
 * 离线自测：起 mock 网关，跑一遍完整发布流程（等构建 → 发起审批 → 等通过 → 部署），
 * 断言最终 JSON 与关键请求体。不碰线上、不需要登录态。
 */
import { spawn } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PORT = 4601;
const BUSY_PORT = 4602;
const RE_PORT = 4603;
const TAG = 'OS-43558-202610291458-online';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function ping(port, tries = 40) {
  return new Promise((resolve, reject) => {
    const attempt = (left) => {
      http
        .get({ host: '127.0.0.1', port, path: '/graphql' }, (res) => {
          res.resume();
          resolve();
        })
        .on('error', () => (left <= 0 ? reject(new Error('mock 没起来')) : setTimeout(() => attempt(left - 1), 150)));
    };
    attempt(tries);
  });
}

function run(args, env) {
  return new Promise((resolve) => {
    const child = spawn('node', [path.join(HERE, 'release.mjs'), ...args], {
      env: { ...process.env, ...env },
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => {
      stdout += d;
    });
    child.stderr.on('data', (d) => {
      stderr += d;
    });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

(async () => {
  const mock = spawn('node', [path.join(HERE, 'mock-server.mjs')], {
    env: { ...process.env, PORT: String(PORT), ODH_TAG: TAG, ODH_TAG_DELAY: '2' },
    stdio: 'ignore',
  });
  await ping(PORT);
  await sleep(150);

  const common = ['--base-url', `http://127.0.0.1:${PORT}`, '--poll-ms', '200', '--wait-minutes', '1'];
  const cases = [];

  // 1. 完整流程
  const full = await run([...common, '--tag', TAG, '--jira', 'OS-43558', '--yes'], { MEDUSA_COOKIE: 'dummy' });
  const fullJson = JSON.parse(full.stdout.trim().split('\n').pop());
  cases.push({ name: '完整流程', code: full.code, json: fullJson, stderr: full.stderr });

  // 2. 审批通过但停在部署前
  const stop = await run(
    [...common, '--tag', TAG, '--jira', 'OS-43558', '--yes', '--stop-before-deploy'],
    { MEDUSA_COOKIE: 'dummy' },
  );
  const stopJson = JSON.parse(stop.stdout.trim().split('\n').pop());
  cases.push({ name: '停部署前', code: stop.code, json: stopJson });

  // 3. 登录态缺失应报 AUTH_REQUIRED（清空 token，指向不存在的 SSO 脚本）
  const noAuth = await run([...common, '--tag', TAG, '--jira', 'OS-43558', '--yes'], {
    MEDUSA_COOKIE: '',
    ONLINE_RELEASE_SSO_SCRIPT: '/dev/null', // 空脚本，换不出 token
  });
  cases.push({
    name: '无登录态',
    code: noAuth.code,
    json: JSON.parse(noAuth.stdout.trim().split('\n').pop()),
  });

  mock.kill();

  // 4. 部署包被别人的任务占着：非交互环境默认停下，把占用者报出来（ask 模式）
  // 5. 同一场景加 --on-deploy-busy wait：自动等待重试后部署成功
  const busyMock = spawn('node', [path.join(HERE, 'mock-server.mjs')], {
    env: { ...process.env, PORT: String(BUSY_PORT), ODH_TAG: TAG, ODH_TAG_DELAY: '1', ODH_DEPLOY_BUSY: '2' },
    stdio: 'ignore',
  });
  await ping(BUSY_PORT);
  await sleep(150);
  const busyCommon = ['--base-url', `http://127.0.0.1:${BUSY_PORT}`, '--poll-ms', '200', '--wait-minutes', '1'];

  const busyAsk = await run([...busyCommon, '--tag', TAG, '--jira', 'OS-43558', '--yes'], {
    MEDUSA_COOKIE: 'dummy',
  });
  cases.push({
    name: '部署包被占用-默认停下',
    code: busyAsk.code,
    json: JSON.parse(busyAsk.stdout.trim().split('\n').pop()),
    stderr: busyAsk.stderr,
  });

  const busyWait = await run([...busyCommon, '--tag', TAG, '--jira', 'OS-43558', '--yes', '--on-deploy-busy', 'wait'], {
    MEDUSA_COOKIE: 'dummy',
    ONLINE_RELEASE_DEPLOY_RETRY_MS: '300',
  });
  busyMock.kill();
  cases.push({
    name: '部署包被占用-等待重试',
    code: busyWait.code,
    json: JSON.parse(busyWait.stdout.trim().split('\n').pop()),
    stderr: busyWait.stderr,
  });

  // 6. 重发：本分支上次发布还在进行中 → 新版本就绪后取消旧任务，再部署
  const PREV_TAG = 'OS-43558-20261029150000-online';
  const reMock = spawn('node', [path.join(HERE, 'mock-server.mjs')], {
    env: {
      ...process.env,
      PORT: String(RE_PORT),
      ODH_TAG: TAG,
      ODH_TAG_DELAY: '1',
      ODH_PREVIOUS_TAG: PREV_TAG,
    },
    stdio: 'ignore',
  });
  await ping(RE_PORT);
  await sleep(150);
  const reRun = await run(
    [
      '--base-url',
      `http://127.0.0.1:${RE_PORT}`,
      '--poll-ms',
      '200',
      '--wait-minutes',
      '1',
      '--tag',
      TAG,
      '--branch',
      'OS-43558',
      '--jira',
      'OS-43558',
      '--yes',
    ],
    { MEDUSA_COOKIE: 'dummy' },
  );
  reMock.kill();
  cases.push({
    name: '重发取消上次发布',
    code: reRun.code,
    json: JSON.parse(reRun.stdout.trim().split('\n').pop()),
    stderr: reRun.stderr,
  });

  const failures = [];
  if (cases[0].code !== 0) failures.push('完整流程退出码非 0');
  if (!cases[0].json.deployed || cases[0].json.deployed.taskId !== 2092183) failures.push('部署任务ID不对');
  if (!cases[0].json.submittedApproval) failures.push('没有提交审批');
  if (!cases[0].json.sectionDeploy || cases[0].json.sectionDeploy.deployType !== 'fast') failures.push('默认没有触发快速部署');
  if (!/快速部署/.test(cases[0].stderr || '')) failures.push('没有打印「快速部署」日志');
  if (!/cluster online1/.test(cases[0].stderr || '')) failures.push('快速部署没有带预览集群 online1');
  if (cases[1].json.sectionDeploy) failures.push('--stop-before-deploy 竟然触发了分部署');
  if (cases[1].json.deployed !== null || !cases[1].json.deploySkipped) failures.push('--stop-before-deploy 没生效');
  if (cases[2].code !== 2 || cases[2].json.code !== 'AUTH_REQUIRED') failures.push('无登录态没报 AUTH_REQUIRED');
  if (cases[3].json.code !== 'DEPLOY_BUSY') failures.push('部署包被占用时没停下来报 DEPLOY_BUSY');
  if (!/someone/.test(cases[3].json.details || '')) failures.push('DEPLOY_BUSY 没带出占用者');
  if (cases[4].code !== 0) failures.push('--on-deploy-busy wait 没有自动重试成功');
  if (!cases[4].json.deployed || cases[4].json.deployed.taskId !== 2092183) failures.push('重试后部署任务ID不对');
  if (!/等部署包/.test(cases[4].stderr || '')) failures.push('重试时没有打印「等部署包」');
  if (cases[5].code !== 0) failures.push('重发取消上次发布：退出码非 0');
  const canceled = (cases[5].json && cases[5].json.canceled) || [];
  if (!canceled.some((item) => item.taskId === 777001)) failures.push('没有取消本分支上次发布的旧任务');
  if (!canceled.some((item) => item.tag === PREV_TAG)) failures.push('取消记录里没有旧 tag');
  if (!/已取消任务 777001/.test(cases[5].stderr || '')) failures.push('没有打印取消旧任务的日志');
  if (!cases[5].json.deployed || cases[5].json.deployed.taskId !== 2092183) failures.push('取消旧任务后没有部署新任务');

  process.stdout.write(
    `${JSON.stringify({ ok: failures.length === 0, failures, cases: cases.map((c) => ({ name: c.name, code: c.code, json: c.json })) }, null, 2)}\n`,
  );
  process.exit(failures.length ? 1 : 0);
})();
