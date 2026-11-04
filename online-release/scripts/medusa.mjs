/**
 * Medusa（saber）发布平台的接口封装。
 *
 * 全部走平台前端自己用的 GraphQL：POST {base}/graphql?env=<env>，
 * 需要本人 SSO 登录态（cookie）。字段来源于平台前端构建产物，平台升级时按报错对齐。
 */
import { createRequire } from 'node:module';

export class AuthError extends Error {
  constructor(message) {
    super(message);
    this.name = 'AuthError';
    this.code = 'AUTH_REQUIRED';
  }
}

export const OPS = {
  getDeployImages: `
    query($id: String!, $itemsPerPage: Int = 100, $page: Int = 1, $deployed: Boolean = false) {
      app(id: $id) {
        commitsWithPage(page: $page, itemsPerPage: $itemsPerPage, deployed: $deployed) {
          totalCount
          nodes {
            id
            createAt
            approvalStatus
            status
            gitlabCommit { id message tag branch committedDate }
            lastDeployHistory { operator createdAt cluster }
          }
        }
      }
    }
  `,
  queryJiraPMAndName: `
    query($jiraID: String!) {
      queryJiraPMAndName(jiraID: $jiraID) { pmName jiraName }
    }
  `,
  checkJiraID: `
    query($appID: String!, $jiraID: String!) {
      jiraIDStatus(appID: $appID, jiraID: $jiraID) { canDeploy apiStatus }
    }
  `,
  checkCanApprovalByJiraID: `
    query($appID: String!, $jiraID: String!) {
      checkCanApprovalByJiraID(appID: $appID, jiraID: $jiraID) { Reason JiraIDs }
    }
  `,
  startDeployApproval: `
    mutation($input: ReleaseApprovalInput!) {
      startReleaseApproval(input: $input)
    }
  `,
  getDeployApprovals: `
    query($id: String!, $page: Int, $itemsPerPage: Int) {
      app(id: $id) {
        releaseApprovals(page: $page, itemsPerPage: $itemsPerPage) {
          nodes { id approvalID jiraID status result createAt gitCommitID appCommit { id } }
        }
      }
    }
  `,
  getDeployPackageOptions: `
    query($appId: String!, $page: Int, $itemsPerPage: Int) {
      app(id: $appId) {
        deploymentConfig(page: $page, itemsPerPage: $itemsPerPage) {
          nodes { name id }
        }
      }
    }
  `,
  checkRepeatCommitByJira: `
    query($input: DeployInput!) {
      queryRepeatCommitByJira(input: $input)
    }
  `,
  getDeployHistory: `
    query($id: String!, $page: Int, $itemsPerPage: Int) {
      app(id: $id) {
        deployHistory(page: $page, itemsPerPage: $itemsPerPage) {
          id
          status
          taskType
          jiraID
          cluster
          deploymentConfigName
          operator
          createdAt
          gitlabCommit { id branch tag }
        }
      }
    }
  `,
  queryDeploySection: `
    query($taskID: Int!) {
      queryDeploySection(taskID: $taskID) {
        taskId
        taskStatus
        taskType
        taskCluster
        section
        sectionDeployType
        deploymentStatus
        trafficStatus
        eventName
        eventCluster
        deploymentRsName
        updatedAt
      }
    }
  `,
  // 任务卡片上「灰度部署 / 快速部署 / 跳过」背后的接口（快速部署 = deployType:fast）
  updateDeploySection: `
    mutation($taskID: Int!, $deployType: String!, $cluster: String!, $section: String!, $value: String = "", $action: String = "") {
      updateDeploySection(taskID: $taskID, deployType: $deployType, cluster: $cluster, section: $section, value: $value, action: $action) {
        taskId
        taskStatus
        section
        sectionDeployType
        eventName
        deploymentStatus
        trafficStatus
      }
    }
  `,
  deployAppV2: `
    mutation($input: DeployInput!) {
      deploy2(input: $input) { taskId taskType eventName taskStatus }
    }
  `,
};

/** Playwright 只用于取登录 cookie，业务请求走原生 fetch */
export function loadPlaywright() {
  const require_ = createRequire(import.meta.url);
  const paths = [process.cwd(), process.env.HOME, '/Users/xxxxx/Documents/workspace/global-station'].filter(Boolean);
  for (const name of ['playwright', 'playwright-core']) {
    try {
      return require_(name);
    } catch {
      /* 换下一个 */
    }
    for (const p of paths) {
      try {
        return require_(require_.resolve(name, { paths: [p] }));
      } catch {
        /* 换下一个 */
      }
    }
  }
  throw new Error('没找到 playwright：请在装了 playwright 的项目里运行，或先 pnpm add -D playwright');
}

export function createClient({ base = 'https://medusa-new.guazi-apps.com', cookie = '' } = {}) {
  const endpoint = (env) => `${base.replace(/\/+$/, '')}/graphql?env=${encodeURIComponent(env)}`;

  async function gql(env, query, variables) {
    const response = await fetch(endpoint(env), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(cookie ? { Cookie: cookie } : {}),
      },
      body: JSON.stringify({ query, variables }),
    });
    const text = await response.text();
    if (response.status === 401 || /token is empty/i.test(text)) {
      throw new AuthError(text.slice(0, 120) || '未登录');
    }
    // 错误响应体多留一些：422 的 GraphQL_VALIDATION_FAILED 会指明是哪个字段错了
    if (!response.ok) throw new Error(`接口返回 ${response.status}：${text.slice(0, 600)}`);
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new Error(`接口返回的不是 JSON：${text.slice(0, 120)}`);
    }
    if (Array.isArray(payload.errors) && payload.errors.length) {
      throw new Error(payload.errors[0].message || 'GraphQL 返回错误');
    }
    return payload.data;
  }

  return {
    base,
    gql,

    /** 拉可部署版本；返回 {nodes, totalCount} */
    async listDeployImages(env, appId, { page = 1, itemsPerPage = 100 } = {}) {
      const data = await gql(env, OPS.getDeployImages, { id: appId, page, itemsPerPage, deployed: false });
      const pageData = (data && data.app && data.app.commitsWithPage) || {};
      return { nodes: pageData.nodes || [], totalCount: pageData.totalCount || 0 };
    },

    async queryJiraPMAndName(env, jiraId) {
      const data = await gql(env, OPS.queryJiraPMAndName, { jiraID: jiraId });
      return (data && data.queryJiraPMAndName) || {};
    },

    async checkJiraID(env, appId, jiraId) {
      const data = await gql(env, OPS.checkJiraID, { appID: appId, jiraID: jiraId });
      return (data && data.jiraIDStatus) || {};
    },

    async checkCanApprovalByJiraID(env, appId, jiraId) {
      const data = await gql(env, OPS.checkCanApprovalByJiraID, { appID: appId, jiraID: jiraId });
      return (data && data.checkCanApprovalByJiraID) || {};
    },

    async startDeployApproval(env, input) {
      const data = await gql(env, OPS.startDeployApproval, { input });
      return data && data.startReleaseApproval;
    },

    async listDeployApprovals(env, appId, { itemsPerPage = 50 } = {}) {
      const data = await gql(env, OPS.getDeployApprovals, { id: appId, page: 1, itemsPerPage });
      const list = data && data.app && data.app.releaseApprovals;
      return (list && list.nodes) || [];
    },

    async listDeployPackages(env, appId) {
      const data = await gql(env, OPS.getDeployPackageOptions, { appId, itemsPerPage: 900 });
      const cfg = data && data.app && data.app.deploymentConfig;
      return (cfg && cfg.nodes) || [];
    },

    async checkRepeatCommitByJira(env, input) {
      const data = await gql(env, OPS.checkRepeatCommitByJira, { input });
      return (data && data.queryRepeatCommitByJira) || [];
    },

    async startDeploy(env, input) {
      const data = await gql(env, OPS.deployAppV2, { input });
      return data && data.deploy2;
    },

    /** 部署历史/发布记录：status 为 done/cancel 之外的值即「发布中」 */
    async getDeployHistory(env, appId, { page = 1, itemsPerPage = 20 } = {}) {
      const data = await gql(env, OPS.getDeployHistory, { id: appId, page, itemsPerPage });
      const rows = (data && data.app && data.app.deployHistory) || [];
      return Array.isArray(rows) ? rows : [];
    },

    /** 发布任务当前阶段状态（完成发布会把 taskStatus 推出 running） */
    async getDeploySection(env, taskId) {
      const data = await gql(env, OPS.queryDeploySection, { taskID: Number(taskId) });
      return (data && data.queryDeploySection) || {};
    },

    /** 触发任务某一段的部署动作：deployType=fast 即任务页上的「快速部署」，flow 是「灰度部署」 */
    async updateDeploySection(env, variables) {
      const data = await gql(env, OPS.updateDeploySection, variables);
      return (data && data.updateDeploySection) || {};
    },
  };
}

/** 审批结果判定：FinishStatus + SuccessResult 才算通过（与平台审批详情页一致） */
export function normalizeApproval(record) {
  if (!record) return 'missing';
  if (record.status === 'FinishStatus' && record.result === 'SuccessResult') return 'passed';
  if (record.status === 'FinishStatus') return 'rejected';
  return 'ongoing';
}

/** 平台对镜像的审批状态枚举（部署弹窗里的 dealApproval 分支） */
export const PLATFORM_APPROVAL_LABEL = {
  NotNeedApproval: '无需审批',
  SuccessResult: '审批通过',
  ApprovalGoingOn: '审批中',
  GoingOnStatus: '审批中',
  ApprovalReject: '已驳回',
  FailureResult: '已驳回',
  NeedApproval: '待发起审批',
  NeedApprovalButApproversIsEmpty: '未配置审批人',
  UndefinedStatus: '审批状态未知',
};

/** 该审批状态是否可以直接部署 */
export function isDeployableStatus(value) {
  return value === 'NotNeedApproval' || value === 'SuccessResult';
}

/** 任务号 -> tag 前缀边界匹配，避免 OS-4355 命中 OS-43558 */
export function matchesJiraTag(tag, jiraId) {
  const tagUpper = String(tag || '').toUpperCase();
  const jiraUpper = String(jiraId || '').toUpperCase();
  if (!jiraUpper) return true;
  if (!tagUpper.startsWith(jiraUpper)) return false;
  const boundary = tagUpper.charAt(jiraUpper.length);
  return boundary === '' || '-._/ '.includes(boundary);
}
