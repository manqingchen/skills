#!/usr/bin/env node
/**
 * 盯着一个发布任务，等它结束（点完「完成发布」）后自动执行后续动作。
 *
 * 平台不发通知（事件流只读、报警订阅在 NIER），所以这里用轮询 queryDeploySection 的 taskStatus
 * 判定任务是否结束：离开下面的「进行中」集合即视为结束。
 *
 * 例：
 *   node watch-release.mjs --task 2092183 --once          # 只看当前状态
 *   node watch-release.mjs --task 2092183 --merge-master --yes
 */
import { createClient } from './medusa.mjs';
import { resolveCookie } from './session.mjs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// 这些状态代表流程还没结束
const ACTIVE = new Set(['running', 'created', 'creating', 'steppaused', 'deploying', 'pending', 'waiting', '']);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const log = (message) => process.stderr.write(`${message}\n`);

function parseArgs(argv) {
  const args = {
    task: '',
    env: 'online',
    baseUrl: 'https://medusa-new.guazi-apps.com',
    pollMs: 20000,
    timeoutMinutes: 180,
    once: false,
    mergeMaster: false,
    yes: false,
    source: '',
  };
  const valueFlags = {
    '--task': 'task',
    '--env': 'env',
    '--base-url': 'baseUrl',
    '--poll-ms': 'pollMs',
    '--timeout-minutes': 'timeoutMinutes',
    '--source': 'source',
  };
  const boolFlags = { '--once': 'once', '--merge-master': 'mergeMaster', '--yes': 'yes' };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (boolFlags[key]) args[boolFlags[key]] = true;
    else if (valueFlags[key]) {
      if (argv[i + 1] === undefined) throw new Error(`${key} 缺少取值`);
      args[valueFlags[key]] = argv[i + 1];
      i += 1;
    } else throw new Error(`未知参数：${key}`);
  }
  if (!args.task) throw new Error('需要 --task <发布任务ID>（部署任务页 URL 里的数字）');
  args.pollMs = Number(args.pollMs);
  args.timeoutMinutes = Number(args.timeoutMinutes);
  return args;
}

function runMergeMaster(args) {
  const extra = ['--yes'];
  if (args.source) extra.push('--source', args.source);
  const out = execFileSync('node', [path.join(HERE, 'mr-master.mjs'), ...extra], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  return JSON.parse(out.trim().split('\n').pop());
}

(async () => {
  const args = parseArgs(process.argv.slice(2));
  const client = createClient({ base: args.baseUrl, cookie: await resolveCookie() });

  if (args.once) {
    const section = await client.getDeploySection(args.env, args.task);
    log(`任务 ${args.task} 当前状态：${section.taskStatus || '未知'}（${section.section || '-'} / ${section.eventName || '-'}）`);
    process.stdout.write(`${JSON.stringify({ ok: true, ...section })}\n`);
    return;
  }

  const deadline = Date.now() + args.timeoutMinutes * 60 * 1000;
  let last = '';
  while (Date.now() < deadline) {
    const section = await client.getDeploySection(args.env, args.task);
    const status = String(section.taskStatus || '').toLowerCase();
    if (section.taskStatus && section.taskStatus !== last) {
      log(`任务 ${args.task} 状态：${section.taskStatus}（${section.eventName || '-'}）`);
      last = section.taskStatus;
    }
    if (!ACTIVE.has(status)) {
      log(`发布流程已结束：${section.taskStatus}`);
      const summary = { ok: true, task: args.task, taskStatus: section.taskStatus, merged: null };
      if (args.mergeMaster) {
        if (!args.yes) {
          log('检测到流程结束；未加 --yes，跳过自动合并 master');
          summary.merged = 'skipped(need --yes)';
        } else {
          summary.merged = runMergeMaster(args);
          log(`合并结果：${JSON.stringify(summary.merged)}`);
        }
      }
      process.stdout.write(`${JSON.stringify(summary)}\n`);
      return;
    }
    await sleep(args.pollMs);
  }

  throw Object.assign(new Error(`等了 ${args.timeoutMinutes} 分钟任务仍未结束，可稍后用 --once 复查`), {
    code: 'TASK_TIMEOUT',
  });
})().catch((error) => {
  log(`\n失败：${error.message}`);
  process.stdout.write(`${JSON.stringify({ ok: false, code: error.code || 'FAILED', message: error.message })}\n`);
  process.exit(1);
});
