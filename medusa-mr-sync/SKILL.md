---
name: medusa-mr-sync
description: 核对一次发布用的分支有没有合进主干；没合就在 GitLab 建 `<分支> → master` 的 MR 并合并。只有 `status=done` 的发布会自动合并；`running` / `cancel` 只查发布段并上报。载荷区间 `target..source` 内任一 merge 的非第一父节点不在目标历史里、或源分支存在 opened MR 时，不自动合并、只上报。单次查发布用 `--jira/--tag/--branch`；批量巡查用 `--patrol`，覆盖 guazi-flow inbox 的所有项目。每次核对都会强制判定发布段（production / preview-only / unknown / not-applicable）。当用户说「这次发布的分支合 master 了吗」「把发版分支合进 master」「巡查一下所有项目有没有漏合」「这是预发还是生产发布」时使用。只管合并与发布段判定；打 tag、审批、部署用 online-release，日常提交用 smart-commit。
---

# medusa-mr-sync：发布分支有没有进 master

本技能只做两件事：**合并检查** 和 **发布段判定**。不改业务代码、不打 tag、不碰审批和部署。

## 固定流程（没有跳过开关）

单次和批量按同一顺序执行，命中 `3` 或 `4` 直接结束并上报，不建 MR、不合并：

| 顺序 | 检查 | 不通过时 |
| --- | --- | --- |
| 1 | 取发布记录并判定发布段 | 接口失败 → `MEDUSA_FAILED`（终止）；判定为 unknown 不阻断合并，但结论不能写「已上线生产」 |
| 2 | 发布状态必须是 `done` | `running` → `RELEASE_NOT_FINISHED`；`cancel` → `RELEASE_CANCELED`（只上报） |
| 3 | 载荷区间 `target..source` 内的 merge 提交 | 任一非第一父节点不在目标历史里/无法确认 → `PAYLOAD_MERGE_COMMIT` |
| 4 | 源分支上是否有 opened MR | 有 → `OPENED_MR_EXISTS` |
| 5 | 全部通过 | 才允许建 MR / 合并 |

### 管線实现（强制串行）

两个脚本共用 `scripts/pipeline.mjs`，顺序写死为：

1. `deploy-stage` — 拉取任务事件并判定发布段（production / preview-only / unknown / not-applicable）
2. `release-state` — 只允许 done 继续
3. `refresh-refs` — 单次模式 `git fetch`（批量模式无操作）
4. `resolve-commit` — 确定提交 / tag / Jira
5. `already-merged` — 提交已在目标分支则结束
6. `source-branch` — 选源分支
7. `payload-merge` — 载荷区间 merge 检查
8. `opened-mr` — 源分支 opened MR 检查
9. `merged-mr` — 已 merged MR 视为已合
10. `squash-check` — 区间无提交视为已合（批量模式）
11. `write` — 唯一允许产生副作用的门禁（建 MR / 合并）

`deploy-stage` 是一个整体：拉事件和判定预发/生产在同一门禁内完成，单次和批量都走这里；
批量模式不再在候选收集阶段提前判定。`release-state` 紧接着判断 done/cancel/running。

`runGates` 逐条执行：任一 gate 返回 `bail` 立即停止，后续 gate（包括 `write`）不会执行；
缺少任何一个 gate 实现会直接抛错，没有「跳过某个检查」的写法。自测：

```bash
node scripts/selftest.mjs
```

自测断言：门禁顺序固定、`bail` 不会触达 `write`、`done` 会提前结束、缺 gate 报错，
以及发布段 / merge 判定 / 排序规则的单元用例。

## 发布状态：只有三种

`status` 按以下规则归一化（大小写不敏感）：

| 归一化 | 原始值 | 处理 |
| --- | --- | --- |
| `done` | `done` | 继续第 3、4 步 |
| `canceled` | `cancel` | 只上报 `RELEASE_CANCELED`，查发布段，不建 MR |
| `running` | 除 done、cancel 之外的任意值（running / create / …） | 只上报 `RELEASE_NOT_FINISHED`，查发布段，不建 MR |

`running` 不代表「没上生产」：任务可能已经部署生产并切了流量，只差有人点「完成发布」。
所以 running 也会查发布段并在报告里给出结论（见下面例子）。

**选哪条记录（确定性）**：单次模式固定只拉最近 50 条，批量模式为 `--medusa-history`（默认 50）。记录先按 `createdAt` 倒序、再按任务 `id` 倒序排序：

| 参数 | 选择规则 |
| --- | --- |
| `--tag` | `gitlabCommit.branch` 与 tag 完全相等的第一条 |
| `--jira` | 该 Jira 的第一条（即最新一条；不因为最新是 running 去回退旧的 done） |
| `--branch` | tag 匹配 `^<branch>(-\d{14})?-online$` 的第一条；没有匹配才用远端分支 HEAD |

注意 `gitlabCommit.branch` 装的是 tag 名，`gitlabCommit.tag` 是空的。

## 发布段判定：production / preview-only / unknown / not-applicable

唯一依据是任务的 K8s 事件流（`queryDeployEvent`）。以下字段**不能**作为证据：

- `status=done`：只代表任务被结单，生产段点「跳过」再完成发布同样是 done；
- `deployHistory.cluster`：部署包配置的目标集群，每条记录都一样；
- 镜像 `lastDeployHistory`：任务结单时写入（时间戳沿用任务创建时间），仅预发也会写。

### 判定规则

设 `<app>` 是应用名（`overseas/global-station` → `global-station`）：

| 事件 | 含义 |
| --- | --- |
| `VirtualService: <app>-*`（不以 `-preview` 结尾）+ `请求创建资源:部署新实例` | 发起生产部署 |
| 同上 + `资源创建成功` | 生产部署成功 |
| `VirtualService: <app>-*-preview` + `请求创建资源:部署新实例` | 发起预览部署 |
| `Deployment: <部署名>:请求变更实例流量配置:权重100%` 且部署名属于本次生产 | 出现过生产流量 100% 事件 |

判定优先级（从上到下，命中即停）：

| 条件 | verdict | reason |
| --- | --- | --- |
| 有生产部署成功事件 | `production` | 空 |
| 有生产部署发起事件、没有成功事件 | `unknown` | `PRODUCTION_REQUESTED_ONLY` |
| 有预览发起或成功事件 | `preview-only` | 空 |
| 以上都没有 | `unknown` | `NO_DEPLOY_EVENTS` |
| 任务没有任何事件 | `unknown` | `NO_EVENTS` |
| 没有部署任务（单次 `--branch` 未匹配到发布记录） | `unknown` | `NO_TASK` |
| 非美杜莎仓库（没有部署任务可查） | `not-applicable` | `NO_TASK` |

补充定义：

- `productionDeployedAt` = 最早一次生产「资源创建成功」的时间；
- `previewDeployedAt` = 最早一次预览「资源创建成功」的时间，没有则取最早一次预览发起时间；
- `productionRequestedAt` = 最早一次生产发起时间；
- `trafficSwitchedAt` = 最早一次「生产部署名 + 权重100%」事件时间。它是**历史事件**，不代表当前流量仍是 100%；
- 部署名归属：只在该秒恰好只有一个 VirtualService 创建事件时，才把 Deployment 归到生产或预览；归属不了的一律不计入生产切流。

### 实测例子

仅预发（生产段没部署）：

```bash
$ node sync-mr.mjs --tag OS-43595-online --dry-run
发布段：仅预发（生产段未部署）
"deployStage":{"taskId":2093956,"verdict":"preview-only","reason":"","previewDeployedAt":"2026-10-30T17:51:11+08:00",
 "productionDeployedAt":"","productionRequestedAt":"","trafficSwitchedAt":"","detail":"只有预览实例事件，生产段没有部署动作"}
```

生产部署并出现过 100% 事件：

```bash
$ node sync-mr.mjs --tag OS-43621.202610302034-online --dry-run
发布段：生产已部署（出现过流量 100% 事件）
"deployStage":{"verdict":"production","previewDeployedAt":"2026-10-30T20:54:02+08:00",
 "productionDeployedAt":"2026-10-30T20:54:43+08:00","productionRequestedAt":"2026-10-30T20:54:34+08:00",
 "trafficSwitchedAt":"2026-10-30T21:06:47+08:00"}
```

`running` 也会查发布段并上报（2026-11-02 全量巡查实测）：

```json
{"status":"RELEASE_NOT_FINISHED",
 "detail":"发布状态 running；发布段：生产已部署（出现过流量 100% 事件）；按规则只自动合并 status=done 的发布"}
```

## 强制检查 3：载荷区间的 merge 内容

检查范围是**将要合并的内容** `target..source` 里的全部 merge 提交（不是只看 tag 指向的提交）。
每个 merge 提交：

- 第一父节点是合并时所在分支的延续，不检查；
- 其余父节点必须全部在目标分支历史里；
- 任一不在、或无法确认 → `PAYLOAD_MERGE_COMMIT`；
- 区间枚举失败、或批量模式 `compare` 被 GitLab 截断（`compare_timeout`）→ `PAYLOAD_MERGE_COMMIT`。

「父节点在目标历史里」= 单次模式 `git merge-base --is-ancestor <parent> origin/<target>`；
批量模式 `<parent>` 的 refs 包含 `<target>`。

实测（2026-11-02，global-station）：

```bash
# 放行：merge 合进来的是 master 已有的内容
$ node sync-mr.mjs --branch OS-40009 --dry-run
载荷区间 master..OS-40009 有 1 个 merge 提交，非第一父节点都已在 master 历史里，放行

# 拦截：merge 合进来的父提交不在 master 历史里
$ node sync-mr.mjs --branch OS-40940 --dry-run
{"ok":false,"code":"PAYLOAD_MERGE_COMMIT",
 "message":"按要求不自动合并：载荷区间 master..OS-40940 的 merge 内容未通过检查：合入的父提交 04c106d5 不在 master 历史里（来自 merge fe29f1ba Merge branch 'feat/home-ssg' into 'OS-40940'）；请人工确认 OS-40940 的内容"}
```

## 强制检查 4：源分支上的 opened MR

- 查询对象是**最终选中的源分支**（不是所有包含该提交的分支）；
- 不区分目标分支，`state=opened` 即可；GitLab 的 Draft MR 也是 `opened`，同样拦截；
- 多条 opened MR 按 `iid` 升序取第一条上报；
- 不复用、不合并、不关闭任何已有 MR。

实测（2026-11-02）：csp 项目 `SCC-42193` 分支存在 MR !1689，巡查当时输出：

```json
{"status":"OPENED_MR_EXISTS",
 "detail":"按要求不自动合并：源分支 SCC-42193 存在 opened MR !1689（https://git.guazi-corp.com/c2b-fe/csp/merge_requests/1689，目标 master）"}
```

## 源分支选择（确定性）

候选 = 远端包含发布提交、且不是目标分支、不是集成分支的分支，按名称升序。

集成分支的判定是固定名单 + 固定正则：`master / main / develop / dev / development / online / online1-3 /
pre / pre-online / prerelease / release / released / prod / production / staging / stage / uat / test /
testing / sit / preview / gray / grey / hotfix`，或以 `v` / `release` / `release-` / `release_` /
`release/` 开头且包含 6 位以上连续数字。

选择顺序：

1. 前 8 个候选里，分支尖端等于发布提交的（按名称升序取第一个命中）；
2. tag 名去掉 `-<14位时间戳>-online` 后与某个候选完全相等；
3. 只有一个候选时用它；
4. 其余报 `NO_SOURCE_BRANCH`，不猜。

## 确定性约束

同一份平台数据、同一份远端 refs，重复运行的输出必须完全一致。为此固定了：

- 发布记录：`createdAt` 倒序 → 任务 `id` 倒序；
- 同一 commit 的多条发布记录 / 多个 tag：只保留排序后的第一条；
- 远端分支候选：名称升序；MR 列表：`iid` 升序；批量 compare 的提交：提交时间倒序 → `id` 升序；
- 批量项目顺序：`repository_path` 升序；
- 事件时间相同：按 reason 字符串升序取最早一条；
- 批量 tag 列表：提交时间倒序 → tag 名升序；
- 计数单位：`deployStages`、`deployStageIssues`、`manualReview` 都按**去重后的候选（每个 commit 一条）**计数；
  两条硬规则同时命中时只算 1 条，`manualReview` 取事件状态为 `PAYLOAD_MERGE_COMMIT` 或 `OPENED_MR_EXISTS` 的候选数；
- 批量写入模式（`--enable-writes`）并发固定为 1，按 `repository_path` 升序执行，
  保证 `--max-merges` 截断时每次跳过的项目一致。

## 用法

脚本路径：`~/.zcode/skills/medusa-mr-sync/scripts/sync-mr.mjs`（也可用 `~/.agents/skills/...`）。

```bash
# 单次：查发布记录 → 发布段 → 状态 → 载荷 merge → opened MR → 建 MR / 合并
node sync-mr.mjs --jira OS-43538
node sync-mr.mjs --tag OS-43538-20261029195356-online
node sync-mr.mjs --branch OS-43538

# 只读：不建 MR、不合并；命中硬规则时返回 ok:false + 失败码
node sync-mr.mjs --tag OS-43538-20261029195356-online --dry-run

# 只建 MR，不合并
node sync-mr.mjs --jira OS-43538 --no-merge

# 批量巡查：默认只读，必须显式 --enable-writes 才写入
node sync-mr.mjs --patrol
node sync-mr.mjs --patrol --project global-station
node sync-mr.mjs --patrol --enable-writes
```

`--dry-run` 也是完整执行全部检查，只是在「建 MR / 合并」前停下。命中硬规则时与正式运行一样返回
`ok:false` + 对应失败码，不会伪造一个 `ok:true`。

## 批量巡查

1. 项目清单来自 guazi-flow：`GET /api/projects/repositories?workspace_slug=guazi`，
   取 `project_title / repository_path / default_ref`。
2. 发布候选：
   - 美杜莎应用（`apps.gitlabPid` 与 `repository_path` 小写后完全相等）→ 最近 `--medusa-history` 条发布记录；
   - 其他仓库 → 仓库最近 `--since-hours` 内打的 `*-online` tag（排除 `-pre-online`）。
3. `--since-hours`：美杜莎按任务 `createdAt`，其他仓库按 tag 提交时间；**`<= 0` 表示不过滤**。
4. 合并目标：美杜莎应用 `master` → `main` → `default_ref`；其他仓库 `default_ref` → `master` → `main` → `develop`。
   `--target` 强制覆盖。目标分支允许就是集成分支（例如平台登记的 `default_ref=online`），这是既定行为。
5. 发布状态：done 继续；running / cancel 在 `events` 里分别记 `RELEASE_NOT_FINISHED` / `RELEASE_CANCELED`，
   同时带上 `deployStage`，不建 MR、不合并。
6. 每个项目独立执行，单个失败不中断整轮。

批量输出（每个项目）：

```json
{"candidates":7,
 "deployStages":{"production":6,"previewOnly":1,"unknown":0,"notApplicable":0},
 "deployStageIssues":[{"jira":"OS-43595","tag":"OS-43595-online","taskId":2093956,"sha":"aa3ad8ca",
   "verdict":"preview-only","detail":"只有预览实例事件，生产段没有部署动作"}],
 "events":[]}
```

`deployStageIssues` 只包含 `preview-only` 和 `unknown`；`not-applicable`（非美杜莎仓库）不算问题。

## 合并的是分支尖端

MR 的源是**源分支当前尖端**，不是 tag 指向的提交。tag 之后如果还有提交，会一起进入目标分支，
这是既定行为：

- 单次输出 `extraCommits` = `<tag提交>..<源分支尖端>` 的提交数；
- 批量事件 `commits` = `target..source` 的提交数（MR 描述里同样列出这些提交）。

## MR 标题与描述

- 标题：`--title` > 唯一 `feat` 提交原文 > `合并 <source> 到 <target>（N 个提交）`；
- 描述：Jira / tag / 部署任务 / **发布段** / 分支与提交数 / `git diff --shortstat` / 只列 `feat`（最多 8 项）；
- 不复用、不修改任何已有 MR 的标题和描述。

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--jira` | 无 | 该 Jira 最新一条发布记录 |
| `--tag` | 无 | `gitlabCommit.branch` 与 tag 完全相等 |
| `--branch` | 无 | 先按 `^<branch>(-\d{14})?-online$` 找发布记录，找不到才用远端分支 HEAD |
| `--app` | `oversea/global-station` | Medusa 应用 id |
| `--env` | `online` | Medusa 环境 |
| `--target` | `master` | 目标分支（批量模式按项目类型自动选，见上文） |
| `--title` | 自动 | 显式 MR 标题 |
| `--dry-run` | 关 | 只读；检查流程完整执行，命中硬规则返回 `ok:false` |
| `--no-merge` | 关 | 只建 MR，不合并；命中硬规则时连 MR 也不建 |
| `--wait-pipeline` | 关 | 使用 GitLab「流水线通过后自动合并」 |
| `--timeout-minutes` | 30 | 等 MR 变可合并的最长时间 |
| `--poll-ms` | 10000 | 轮询间隔 |

批量（`--patrol`）额外参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--project` | 全部 | 精确匹配 `project_title` 或 `repository_path`（忽略大小写），可重复 |
| `--workspace` | `guazi` | guazi-flow 工作区 slug |
| `--since-hours` | 72 | 时间窗；`<= 0` 表示不过滤 |
| `--medusa-history` | 50 | 每个应用最多回看多少条发布记录；更旧的发布不会被发现 |
| `--max-merges` | 20 | 单轮最多合并多少个 MR |
| `--concurrency` | 4 | 同时巡查几个项目；`--enable-writes` 时固定为 1（保证截断顺序一致） |
| `--include` / `--exclude` | 无 | 精确匹配 `project_title` 或 `repository_path`（忽略大小写），逗号分隔，无通配符 |
| `--target` | 自动 | 强制所有项目合并到同一分支 |
| `--enable-writes` | 关 | 批量写入总开关：不加就只读 |

## 结果与失败码

成功时 stdout 一行 JSON。单次模式关键字段：`deployStage`、`mergeStateUnknown`、`extraCommits`、
`source`、`target`；批量模式关键字段：`summary`、每个项目的 `deployStages` / `deployStageIssues` / `events`。

| code | 含义 | 处置 |
| --- | --- | --- |
| `AUTH_REQUIRED` | 没有有效 GUAZISSO | 跑一次 guazi-sso-login，或设 `MEDUSA_COOKIE` |
| `MEDUSA_FAILED` | Medusa 接口失败（含发布段事件流查询失败） | 看原始报错；发布段无法判定时不继续 |
| `RELEASE_NOT_FOUND` | 单次模式：最近 50 条记录里没有该 Jira/tag | 用 `--tag` 精确指定；更旧的发布不在单次模式的回看范围内 |
| `RELEASE_NOT_FINISHED` | 发布状态属于 running | 等结单后再跑；报告里可带发布段 |
| `RELEASE_CANCELED` | 发布状态是 cancel | 该发布不处理 |
| `BRANCH_NOT_FOUND` | `--branch` 指定的远端分支不存在 | 检查分支名或先推送 |
| `NO_SHA` | 发布记录里没有 commit | 用 `--tag` 重新指定，或改用 `--branch` |
| `NO_SOURCE_BRANCH` | 没有非集成分支包含该提交，或候选无法唯一确定 | 用 `--branch` 指定 |
| `NO_TARGET_BRANCH` | 批量：`default_ref / master / main / develop` 都不存在 | 用 `--target` 指定 |
| `PROJECT_NOT_FOUND` | `--project` 没匹配到 inbox 项目 | 用精确项目名或仓库 path |
| `PAYLOAD_MERGE_COMMIT` | 载荷区间的 merge 内容未通过检查（外部分支 / 无法确认 / compare 截断） | 人工确认后处理 |
| `OPENED_MR_EXISTS` | 源分支存在 opened MR（含 Draft） | 人工评审/关闭后重跑 |
| `MR_CONFLICT` | MR 有冲突 | 人工解冲突后重跑 |
| `MR_NOT_MERGEABLE` | `need_rebase / not_approved / blocked_status / ci_must_pass / not_open` | 按状态处理，或加 `--wait-pipeline` |
| `MR_TIMEOUT` | 等 MR 可合并超时 | 去 MR 页面看卡点 |
| `GIT_FAILED` | git 命令失败 | 看原始报错 |
| `GITLAB_FAILED` | GitLab 接口失败 | 看原始报错 |

批量模式 `summary` 固定字段：
`merged / created / wouldCreate / alreadyMerged / noSource / conflicts / notMergeable / manualReview /
releaseNotFinished / releaseCanceled / failed / skipped`。

## 前置与凭证

- 单次模式：在目标仓库目录下运行（脚本用当前目录的 git），`glab` 已登录且有该仓库的 Maintainer 权限；
- 批量模式：不需要本地仓库和 glab，只要有 GitLab token 和 GUAZISSO；
- 凭证链：
  - 美杜莎 / guazi-flow：`MEDUSA_COOKIE` / `GUAZISSO` → `guazi-sso-login` → `POST /auth/sso/identity` 换 `multica_auth`（或 `FLOW_COOKIE`）；
  - GitLab：`GITLAB_TOKEN` / `PRIVATE_TOKEN` → `~/.git-credentials` → `glab config get token --host git.guazi-corp.com`。
