/**
 * 固定门禁管線。
 *
 * 单次核对（sync-mr.mjs）与批量巡查（patrol.mjs）必须按同一份顺序执行；
 * 任何一个门禁返回 `bail` 就停止，后续门禁（包括 write）不会执行。
 * `write` 是唯一允许产生副作用的门禁，固定在最后一步。
 *
 * 门禁返回协议：
 *   { action: 'continue' }                     继续下一个门禁
 *   { action: 'bail', code, message }          终止，调用方按 code 上报/抛错
 *   { action: 'done', outcome, output }        终止并返回结果（write 必须返回 done）
 *
 * 缺任何一个门禁实现都会直接抛错，不存在“跳过某个检查”的写法。
 */

export const GATE_ORDER = [
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
];

export async function runGates(ctx, gates) {
  for (const name of GATE_ORDER) {
    const gate = gates[name];
    if (typeof gate !== 'function') {
      throw new Error(`管線缺少门禁实现：${name}`);
    }
    const result = (await gate(ctx)) || { action: 'continue' };
    if (result.action === 'bail' || result.action === 'done') {
      return { ...result, stoppedAt: name, ctx };
    }
    if (result.action !== 'continue') {
      throw new Error(`门禁 ${name} 返回了未知 action：${result.action}`);
    }
  }
  throw new Error('管線执行到末尾但没有 write 门禁产出结果');
}
