/**
 * medusa-mr-sync 的共享判定规则。
 *
 * 单次核对（sync-mr.mjs）与批量巡查（patrol.mjs）必须使用同一套规则，
 * 否则同一份数据会得出不同结论。本模块只放纯函数与常量，不做 IO。
 *
 * 判定口径（与 SKILL.md 一一对应，修改这里必须同步改文档）：
 * 1. 发布状态只分三类：done / canceled / running（除 done、cancel 之外都算 running）。
 * 2. 发布段只分四类：production / preview-only / unknown / not-applicable。
 * 3. merge commit 的检查范围是载荷区间 `target..source` 内的全部 merge 提交，
 *    每个 merge 只看非第一父节点；父节点在 target 历史里则放行，不在或无法确认则拦截。
 */

export const RELEASE_DONE = 'done';
export const RELEASE_CANCELED = 'cancel';

/** 发布任务状态归一化：done / canceled / running */
export function releaseState(status) {
  const value = String(status || '').toLowerCase();
  if (value === RELEASE_DONE) return 'done';
  if (value === RELEASE_CANCELED) return 'canceled';
  return 'running';
}

export function uniq(list) {
  return [...new Set(list.filter(Boolean))];
}

/** 记录按 createdAt 倒序，时间相同按 id 倒序（保证重复运行顺序一致） */
export function sortByCreatedAtDesc(rows, idKey = 'id') {
  return [...(rows || [])].sort((a, b) => {
    const ta = Date.parse((a && a.createdAt) || '') || 0;
    const tb = Date.parse((b && b.createdAt) || '') || 0;
    if (ta !== tb) return tb - ta;
    const na = Number(a && a[idKey]);
    const nb = Number(b && b[idKey]);
    if (Number.isFinite(na) && Number.isFinite(nb) && na !== nb) return nb - na;
    return String((b && b[idKey]) || '').localeCompare(String((a && a[idKey]) || ''));
  });
}

/** MR 列表按 iid 升序（同一状态可能有多条，取最小的那条，保证结果稳定） */
export function sortMrsById(mrs) {
  return [...(mrs || [])].sort((a, b) => {
    const na = Number(a && a.iid);
    const nb = Number(b && b.iid);
    if (Number.isFinite(na) && Number.isFinite(nb) && na !== nb) return na - nb;
    return String((a && a.web_url) || '').localeCompare(String((b && b.web_url) || ''));
  });
}

/* ------------------------------ 分支过滤 ------------------------------ */

/**
 * 集成分支：任何情况下都不作为合并源。
 * 判定是固定名单 + 固定正则，避免“像集成分支”这种主观判断：
 * - 名单内（忽略大小写）；
 * - 或以 v / release / release- / release_ / release/ 开头且包含 6 位以上连续数字。
 */
export const INTEGRATION_BRANCHES = new Set([
  'master',
  'main',
  'develop',
  'dev',
  'development',
  'online',
  'online1',
  'online2',
  'online3',
  'pre',
  'pre-online',
  'prerelease',
  'release',
  'released',
  'prod',
  'production',
  'staging',
  'stage',
  'uat',
  'test',
  'testing',
  'sit',
  'preview',
  'gray',
  'grey',
  'hotfix',
]);

export function isIntegrationBranch(name) {
  const value = String(name || '').toLowerCase();
  if (INTEGRATION_BRANCHES.has(value)) return true;
  return /^(v|release[-_/])/.test(value) && /\d{6,}/.test(value);
}

/* ------------------------------ 发布段判定 ------------------------------ */

/** 事件 reason 形如 `VirtualService: global-station-deploy-preview:请求创建资源:部署新实例` */
export function virtualServiceName(reason) {
  const match = String(reason || '').match(/^VirtualService:\s*([^:\s]+):/);
  return match ? match[1] : '';
}

/** 事件 reason 形如 `Deployment: global-station-deploy-pkltj:请求创建资源:部署新实例` */
export function deploymentName(reason) {
  const match = String(reason || '').match(/^Deployment:\s*([^:\s]+):/);
  return match ? match[1] : '';
}

const eventTime = (event) => Date.parse((event && event.createdAt) || '') || 0;

/** 同一时间的事件按 reason 排序，保证取到的“最早一条”稳定 */
function earliest(events) {
  if (!events.length) return null;
  return [...events].sort(
    (a, b) => eventTime(a) - eventTime(b) || String(a.reason || '').localeCompare(String(b.reason || '')),
  )[0];
}

const isCreate = (reason) => String(reason || '').includes('请求创建资源:部署新实例');
const isSuccess = (reason) => String(reason || '').includes('资源创建成功');

/**
 * 发布段判定。输入是任务的全部 K8s 事件（queryDeployEvent）。
 *
 * - 生产成功：出现 `<app>-*`（非 -preview）VirtualService 的「资源创建成功」→ production；
 * - 只发起过生产部署但没有成功事件 → unknown（PRODUCTION_REQUESTED_ONLY），不算生产；
 * - 只有预览事件 → preview-only；
 * - 没有部署事件 → unknown（NO_DEPLOY_EVENTS）。
 *
 * 切流只认「Deployment 名属于本次任务的生产部署」且 reason 含 `权重100%` 的事件：
 * Deployment 名通过“同一秒只有一个 VirtualService 创建事件”归属到生产或预览，
 * 归属不了的一律不算生产切流（不做文字猜测）。
 */
export function classifyDeployStage({ events, appName, taskId }) {
  const task = taskId ? Number(taskId) : null;
  const empty = {
    taskId: task,
    verdict: 'unknown',
    reason: '',
    previewDeployedAt: '',
    productionDeployedAt: '',
    productionRequestedAt: '',
    trafficSwitchedAt: '',
    detail: '',
  };
  if (!task) return { ...empty, reason: 'NO_TASK', detail: '没有部署任务（--branch 未匹配到发布记录）' };

  const list = (Array.isArray(events) ? events : []).filter(Boolean);
  if (!list.length) return { ...empty, reason: 'NO_EVENTS', detail: '任务没有任何 K8s 事件' };

  const vsEvents = list.filter((event) => virtualServiceName(event.reason));
  const previewVs = vsEvents.filter((event) => /-preview$/.test(virtualServiceName(event.reason)));
  const productionVs = vsEvents.filter((event) => {
    const name = virtualServiceName(event.reason);
    return name.startsWith(`${appName}-`) && !/-preview$/.test(name);
  });

  const previewCreate = previewVs.filter((event) => isCreate(event.reason));
  const previewSuccess = previewVs.filter((event) => isSuccess(event.reason));
  const productionCreate = productionVs.filter((event) => isCreate(event.reason));
  const productionSuccess = productionVs.filter((event) => isSuccess(event.reason));

  // Deployment 名 → 生产/预览：只在“同一秒恰好只有一个 VirtualService 创建事件”时归属
  const productionDeployments = new Set();
  for (const event of list) {
    const name = deploymentName(event.reason);
    if (!name || !isCreate(event.reason)) continue;
    const sameSecond = vsEvents.filter((item) => eventTime(item) === eventTime(event) && isCreate(item.reason));
    if (sameSecond.length !== 1) continue;
    const service = virtualServiceName(sameSecond[0].reason);
    if (!/-preview$/.test(service) && service.startsWith(`${appName}-`)) productionDeployments.add(name);
  }
  const trafficEvents = list
    .filter(
      (event) =>
        /权重100%/.test(String(event.reason || '')) && productionDeployments.has(deploymentName(event.reason)),
    )
    .sort((a, b) => eventTime(a) - eventTime(b) || String(a.reason || '').localeCompare(String(b.reason || '')));

  let verdict = 'unknown';
  let reason = 'NO_DEPLOY_EVENTS';
  let detail = '事件流里没有本项目的部署动作';
  if (productionSuccess.length) {
    verdict = 'production';
    reason = '';
    detail = trafficEvents.length
      ? '生产实例部署成功，出现过流量 100% 事件'
      : '生产实例部署成功，没有流量 100% 事件';
  } else if (productionCreate.length) {
    verdict = 'unknown';
    reason = 'PRODUCTION_REQUESTED_ONLY';
    detail = '发起过生产部署，但没有「资源创建成功」事件，不能算已部署';
  } else if (previewCreate.length || previewSuccess.length) {
    verdict = 'preview-only';
    reason = '';
    detail = '只有预览实例事件，生产段没有部署动作';
  }

  const previewAt = earliest(previewSuccess.length ? previewSuccess : previewCreate);
  const productionRequestedAt = earliest(productionCreate);
  const productionAt = earliest(productionSuccess);
  return {
    taskId: task,
    verdict,
    reason,
    previewDeployedAt: previewAt ? previewAt.createdAt : '',
    productionDeployedAt: productionAt ? productionAt.createdAt : '',
    productionRequestedAt: productionRequestedAt ? productionRequestedAt.createdAt : '',
    trafficSwitchedAt: trafficEvents.length ? trafficEvents[0].createdAt : '',
    detail,
  };
}

/** 非美杜莎仓库没有部署任务，发布段不适用（不是 unknown） */
export function notApplicableStage() {
  return {
    taskId: null,
    verdict: 'not-applicable',
    reason: 'NO_TASK',
    previewDeployedAt: '',
    productionDeployedAt: '',
    productionRequestedAt: '',
    trafficSwitchedAt: '',
    detail: '非美杜莎项目，没有部署任务可查',
  };
}

export function stageLabel(stage) {
  if (!stage) return '未判定';
  if (stage.verdict === 'production') {
    return stage.trafficSwitchedAt ? '生产已部署（出现过流量 100% 事件）' : '生产已部署';
  }
  if (stage.verdict === 'preview-only') return '仅预发（生产段未部署）';
  if (stage.verdict === 'not-applicable') return '不适用（非美杜莎项目）';
  return `无法判定（${stage.detail || stage.reason || '原因未知'}）`;
}

/** 汇总计数用的固定桶 */
export function stageBucket(verdict) {
  if (verdict === 'preview-only') return 'previewOnly';
  if (verdict === 'not-applicable') return 'notApplicable';
  if (verdict === 'production') return 'production';
  return 'unknown';
}

/* --------------------------- merge commit 判定 --------------------------- */

/**
 * 检查载荷区间内的 merge 提交。
 *
 * mergeCommits: [{ id, subject, parents: [第一父节点, ...其余父节点] }]
 * isAncestorOfTarget(parentSha): true / false / null（无法确认）
 *
 * 第一父节点是合并时所在分支的延续，不检查；其余父节点只要有一个不在目标分支历史里，
 * 或无法确认，就返回 ok=false。结果按 merge id、parent id 排序，保证重复运行一致。
 */
export async function analyzeMergeCommits(mergeCommits, isAncestorOfTarget) {
  const sorted = [...(mergeCommits || [])].sort((a, b) => String(a.id).localeCompare(String(b.id)));
  const foreign = [];
  const unverified = [];
  const checked = [];
  for (const commit of sorted) {
    const others = [...(commit.parents || [])].slice(1).sort();
    for (const parent of others) {
      const state = await isAncestorOfTarget(parent);
      checked.push({ merge: String(commit.id), parent: String(parent), state });
      if (state === false) {
        foreign.push({ merge: String(commit.id), mergeSubject: commit.subject || '', parent: String(parent) });
      } else if (state === null) {
        unverified.push({ merge: String(commit.id), mergeSubject: commit.subject || '', parent: String(parent) });
      }
    }
  }
  return { ok: !foreign.length && !unverified.length, foreign, unverified, checked };
}

const shortSha = (value) => String(value || '').slice(0, 8);

/** 生成拦截原因；ok=true 时返回空字符串 */
export function mergeRuleMessage({ source, target, analysis }) {
  if (!analysis || analysis.ok) return '';
  const parts = [];
  if (analysis.foreign.length) {
    const merges = uniq(
      analysis.foreign.map((item) => `${shortSha(item.merge)}${item.mergeSubject ? ` ${item.mergeSubject}` : ''}`),
    );
    parts.push(
      `合入的父提交 ${uniq(analysis.foreign.map((item) => shortSha(item.parent))).join('、')} 不在 ${target} 历史里` +
        `（来自 merge ${merges.join('；')}）`,
    );
  }
  if (analysis.unverified.length) {
    parts.push(
      `父提交 ${uniq(analysis.unverified.map((item) => shortSha(item.parent))).join('、')} 无法确认是否在 ${target} 历史里`,
    );
  }
  return `载荷区间 ${target}..${source} 的 merge 内容未通过检查：${parts.join('，')}；请人工确认 ${source} 的内容`;
}
