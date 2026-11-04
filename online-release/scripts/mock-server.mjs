/**
 * 离线 mock：模拟 Medusa 的 GraphQL 网关，用来在没有登录态/不想碰线上时回归整条流程。
 * 前 ODH_TAG_DELAY 次查询返回空列表（模拟流水线还在构建），审批记录在提交审批后才出现。
 */
import http from 'node:http';

const PORT = Number(process.env.PORT || 4600);
const TAG = process.env.ODH_TAG || 'OS-43558-202610291458-online';
const TAG_DELAY = Number(process.env.ODH_TAG_DELAY || 2);

let imageCalls = 0;
let deployCalls = 0;
let approved = false;
let sectionDeployed = false;
let lastSectionVars = {};
const canceledTasks = new Set();
const calls = [];

const image = {
  id: 2672733,
  createAt: '2026-10-29T11:12:55Z',
  approvalStatus: '',
  gitlabCommit: { id: '999ed2', tag: TAG, committedDate: '2026-10-28T11:39:45Z', message: 'fix(OS-43558)' },
  lastDeployHistory: { cluster: '', createdAt: null },
};

// ODH_IN_PROGRESS_SHA：模拟「有一个发布中任务」，用于验证发布前体检
function reply(query) {
  if (query.includes('commitsWithPage')) {
    imageCalls += 1;
    if (imageCalls <= TAG_DELAY) return { app: { commitsWithPage: { totalCount: 0, nodes: [] } } };
    return { app: { commitsWithPage: { totalCount: 1, nodes: [image] } } };
  }
  if (query.includes('deployHistory')) {
    // ODH_PREVIOUS_TAG：模拟「本分支上次发布还在进行中」，用于验证重发时取消旧任务
    const previousTag = process.env.ODH_PREVIOUS_TAG;
    if (previousTag) {
      return {
        app: {
          deployHistory: [
            {
              id: 777001,
              status: 'running',
              jiraID: 'OS-43558',
              operator: 'me',
              deploymentConfigName: 'deploy',
              gitlabCommit: { id: 'old111', tag: previousTag, branch: previousTag },
            },
          ],
        },
      };
    }
    // ODH_DEPLOY_BUSY：模拟部署包被别人的任务占着，deployHistory 里要能看出是谁
    if (process.env.ODH_DEPLOY_BUSY) {
      return {
        app: {
          deployHistory: [
            {
              id: 888001,
              status: 'running',
              jiraID: 'OS-HOLDER',
              operator: 'someone',
              deploymentConfigName: 'deploy',
              gitlabCommit: { id: '', branch: 'holder-online' },
            },
          ],
        },
      };
    }
    const sha = process.env.ODH_IN_PROGRESS_SHA;
    return {
      app: {
        deployHistory: sha
          ? [{ id: 999001, status: 'running', jiraID: 'OS-TEST', taskType: 'flow', gitlabCommit: { id: sha, branch: 'test-branch' } }]
          : [],
      },
    };
  }
  if (query.includes('queryJiraPMAndName')) {
    return { queryJiraPMAndName: { pmName: '赵东', jiraName: '代理商系统 v1.1' } };
  }
  if (query.includes('jiraIDStatus')) return { jiraIDStatus: { canDeploy: true, apiStatus: true } };
  if (query.includes('checkCanApprovalByJiraID')) {
    return { checkCanApprovalByJiraID: { Reason: 0, JiraIDs: [] } };
  }
  if (query.includes('startReleaseApproval')) {
    approved = true;
    return { startReleaseApproval: 'TDDEPL20261029000099' };
  }
  if (query.includes('releaseApprovals')) {
    return {
      app: {
        releaseApprovals: {
          nodes: approved
            ? [
                {
                  id: 1,
                  approvalID: 'TDDEPL20261029000099',
                  jiraID: 'OS-43558',
                  status: 'FinishStatus',
                  result: 'SuccessResult',
                  appCommit: { id: image.id },
                  gitCommitID: image.gitlabCommit.id,
                },
              ]
            : [],
        },
      },
    };
  }
  if (query.includes('deploymentConfig')) {
    return { app: { deploymentConfig: { nodes: [{ name: 'deploy', id: 185220 }] } } };
  }
  if (query.includes('queryRepeatCommitByJira')) return { queryRepeatCommitByJira: [] };
  // 任务页上「快速部署 / 灰度部署」背后的接口：回一个做完了的 preview 段
  if (query.includes('updateDeploySection')) {
    const v = (calls[calls.length - 1] && calls[calls.length - 1].variables) || {};
    // 取消发布：任务进入 cancel，部署包随之释放
    if (v.action === 'cancel') {
      canceledTasks.add(Number(v.taskID));
      return {
        updateDeploySection: {
          taskId: Number(v.taskID),
          taskStatus: 'cancel',
          section: v.section,
          sectionDeployType: v.deployType,
          eventName: 'cancel',
          deploymentStatus: '',
          trafficStatus: '',
        },
      };
    }
    sectionDeployed = true;
    lastSectionVars = v;
    return {
      updateDeploySection: {
        taskId: 2092183,
        taskStatus: 'running',
        section: 'preview',
        sectionDeployType: v.deployType,
        eventName: 'deploy_new_version',
        deploymentStatus: 'done',
        trafficStatus: 'done',
      },
    };
  }
  if (query.includes('queryDeploySection')) {
    const v = (calls[calls.length - 1] && calls[calls.length - 1].variables) || {};
    const taskId = Number(v.taskID) || 2092183;
    if (canceledTasks.has(taskId)) {
      return {
        queryDeploySection: {
          taskId,
          taskStatus: 'cancel',
          section: 'preview',
          deploymentStatus: '',
          trafficStatus: '',
        },
      };
    }
    return {
      queryDeploySection: {
        taskId: 2092183,
        taskStatus: 'running',
        section: 'preview',
        sectionDeployType: sectionDeployed ? lastSectionVars.deployType : '',
        eventName: sectionDeployed ? 'deploy_new_version' : '',
        deploymentStatus: sectionDeployed ? 'done' : 'doing',
        trafficStatus: sectionDeployed ? 'done' : 'doing',
      },
    };
  }
  if (query.includes('deploy2')) {
    deployCalls += 1;
    if (deployCalls <= Number(process.env.ODH_DEPLOY_BUSY || 0)) {
      return { __errors: ['部署包有正在执行的部署任务'] };
    }
    return { deploy2: { taskId: 2092183, taskType: 'flow' } };
  }
  return {};
}

const server = http.createServer((req, res) => {
  if (req.method !== 'POST' || !req.url.startsWith('/graphql')) {
    res.writeHead(404).end('not found');
    return;
  }
  let raw = '';
  req.on('data', (chunk) => {
    raw += chunk;
  });
  req.on('end', () => {
    const body = JSON.parse(raw || '{}');
    calls.push({ variables: body.variables });
    const data = reply(body.query || '');
    res.writeHead(200, { 'Content-Type': 'application/json' });
    // __errors 用于模拟 GraphQL 业务报错（如部署包被占用）
    res.end(
      JSON.stringify(data && data.__errors ? { errors: data.__errors.map((message) => ({ message })) } : { data }),
    );
  });
});

server.listen(PORT, '127.0.0.1', () => {
  process.stderr.write(`mock medusa on http://127.0.0.1:${PORT}\n`);
});
