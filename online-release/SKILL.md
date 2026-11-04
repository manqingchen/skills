---
name: online-release
description: 上线发布到预发（Online 发布）：按 Jira 号打 `-online` tag 并推送、等流水线构建出可部署镜像、发起部署审批、轮询审批结果、部署到预发环境，全程无界面，直接调 Medusa 发布平台的 GraphQL 接口（凭据取自 guazi-sso-login 的 GUAZISSO token）。push 之后自动建分支到 master 的 MR（只建不合，已存在 open MR 直接复用；标题/描述口径同 create-mr：主功能 feat 提交 + Jira/cwiki 链接）。重发时先判断本分支自上次发布有无新提交（无改动跳过，`--force` 强制），有新提交则在审批通过后自动取消本分支上次遗留的进行中发布任务（只认本分支 tag，不碰别人）。当用户说「上线」「发版到预发」「把 OS-43558 上线」「Online 发布」「重新发一版」「这个 tag 审批过了没 / 部署到哪了」时使用。覆盖「合 master → push → 建 MR → 打 tag → 审批 → 部署预发」这一段；日常提交用 smart-commit / ship。
---

# online-release：打 tag → 等构建 → 审批 → 部署预发

一条命令跑完 Medusa 发布平台上「上线到预发」的全流程，不需要点页面、不需要开浏览器。

## 边界

- **合并方向只有一个**：`<当前分支> → master`。发布时把 master 合进当前分支并推送，push 之后**自动**建 `<当前分支> → master` 的 MR；**MR 只建不合**，合并仍由用户决定。全程不涉及其他集成分支。
- **不替人审批**：审批由审批人在统一审批平台点，本 skill 只负责发起与等待。
- **不到正式**：只到「预览实例部署」这一步（即预发）；生产实例部署、完成发布仍由人在平台操作。
- **四个动作必须用户明确同意才能做**：打 tag 推送、提交审批、提交部署任务、点快速部署。用户只说「看看」就用 `--status`。

## 前置

Medusa 认的凭据是 cookie `GUAZISSO`，由 `guazi-sso-login` skill 用本机缓存的账号密码换取（不启动浏览器）。首次或 token 失效时让用户执行一次：

```bash
/usr/bin/python3 ~/.zcode/skills/guazi-sso-login/scripts/guazi_sso_login.py sso --validate --plain
```

也可以直接给现成 cookie：`MEDUSA_COOKIE="GUAZISSO=xxx"`。

## 命令

脚本路径：`~/.zcode/skills/online-release/scripts/release.mjs`（本项目内也可用 `~/.agents/skills/...`）。

### 建 MR 是自动的（在 push 之后、打 tag 之前）

`release.mjs` 在 `git push` 之后自己建 MR，不需要额外命令或人工介入：

1. 分支相对 `origin/master` 没有未合的非合并提交 → 跳过（没什么可合的）；
2. 已有 **open** MR → 直接复用，打印 `!iid url`，不重复创建；
3. 否则 `glab mr create --source-branch <分支> --target-branch master`，**只建不合**。

标题与描述口径与 create-mr 技能一致：
- 标题取**主功能 `feat(<Jira>):` 提交**（最早那条），没有 feat 才退到带 Jira 号的提交、最早提交；
- 描述是四段模板，`关联任务` 带 Jira 链接 + cwiki 需求父页链接（用 guazi-sso-login 的 wiki cookie 查 CQL，查不到写「待补充」）。

建 MR 失败会报 `MR_CREATE_FAILED` 并停在这一步（tag 还没打），重跑即可（幂等）；
确定这次不想建 MR 时加 `--no-mr`。

### 其他用法

```bash
# 只看看状态（只读，最安全）
node release.mjs --jira OS-43558 --status

# 演练：只打印将执行的 pull/push/tag，不动仓库
node release.mjs --jira OS-43558 --dry-run

# 正式发布：git pull → git pull origin master → git push → 建 MR → pnpm tsc → 打 tag 推送
#          → 等镜像 → 发起审批 → 等审批 → 建部署任务 → 点「快速部署」→ 等到预览实例部署完成
node release.mjs --jira OS-43558 --yes

# 走灰度而不是快速部署（对应任务页的「灰度部署」）
node release.mjs --jira OS-43558 --yes --deploy-mode flow

# 只建部署任务，灰度/快速留在页面上人工点
node release.mjs --jira OS-43558 --yes --deploy-mode manual

# 只到审批通过，部署交给用户点
node release.mjs --jira OS-43558 --yes --stop-before-deploy

# 用已有 tag 发布（仓库动作全部跳过）
node release.mjs --tag OS-43558-20261029181532-online --jira OS-43558 --yes

# 可选：把当前分支合进 master（不在发布主流程里）
node mr-master.mjs
node mr-master.mjs --yes
```

## 失败后续跑（用已推的 tag 接着走）

中间任何一步断掉（等镜像超时、建部署任务报错、网络抖动）都不需要重新打 tag：

```bash
node release.mjs --tag OS-43558-20261029181532-online --jira OS-43558 --yes
```

给了 `--tag` 就跳过全部仓库动作（pull / 合 master / push / 建 MR / tsc / 打 tag）。脚本会重新找镜像、
重新查审批：镜像已经有了就直接用；审批已是 `FinishStatus / SuccessResult` 就跳过「发起 + 等待」，
直接从「建部署任务」接着走；只有 `NeedApproval` 才重新发起审批。2026-10-30 OS-43580 就是这样在
`deploy2` 报错后从「建任务」续跑成功的，已建的镜像和已通过的审批都没浪费。

## 建完任务后自动点「快速部署」（默认行为）

`deploy2` 只是**创建**发布任务。任务建出来会停在第一步「预览实例部署」，页面底下三个按钮
「灰度部署 / 快速部署 / 跳过」等人选——不点它，副本永远不会起来（任务的 k8s 事件是 0 条，
状态一直是 `create`，发布包也一直被占着）。

所以 `release.mjs` 建完任务后默认接着替人点**快速部署**，等这一段部署完成再返回。
这三个按钮背后是同一个 mutation（2026-10-29 在 `deploytask/2092884` 上实测抓包）：

```graphql
mutation($taskID: Int!, $deployType: String!, $cluster: String!, $section: String!, $value: String = "", $action: String = "") {
  updateDeploySection(taskID: $taskID, deployType: $deployType, cluster: $cluster, section: $section, value: $value, action: $action) { ... }
}
```

| 页面按钮 | 变量 |
| --- | --- |
| 快速部署 | `{deployType:"fast", cluster:"online1", section:"preview", action:"deploy_new_version", value:"0"}` |
| 灰度部署 | 同上，`deployType:"flow"` |
| 跳过 | 纯前端切下一段，不调接口 |
| 取消发布 | `action:"cancel"` |
| 完成发布 | `action:"close"`（只在最后一段、流量全切后可用） |

`cluster` 取的是页面里 `preview` 段固定的 `online1`（与任务自身的 `eventCluster=online3` 不是一回事），
可用 `--preview-cluster` 覆盖。`--deploy-mode manual` 则完全不点，退回到「只建任务」。

**注意**：发到预览实例后任务仍然是 `running`，页面上只剩「下一步 / 取消发布」，要等有人在生产段走完
「完成发布」才会变 `done`（不关的话平台提示三天后自动关闭）。这段时间它一直占着 `deploy` 包，
下一次发布会被 `DEPLOY_BUSY` 拦住。

## 重发：有改动才发，旧任务自动取消

再执行一次 `release.mjs` 时：

1. **先判断有没有改动**（`--tag` 续跑除外）。取本分支最近一次发布 tag（`<分支>-<14位时间戳>-online`，
   本地和远端都看），用 `git rev-list --no-merges --first-parent <tag>..HEAD` 判断：
   - 只看第一父链，所以「只是把 master 合进来」不算改动；
   - 没有新提交 → 直接跳过：`{"ok":true,"skipped":true,"reason":"NO_CHANGES","previousTag":"…"}`，不推 tag、不动平台；
   - 有新提交 → 正常重发。确实要拿同一个提交再发一遍，加 `--force`。
2. **取消上次的发布任务**。新 tag 的镜像就绪、审批通过之后、建新部署任务之前，把平台上**本分支**还在
   进行中的旧任务取消掉——不取消的话部署包被它占着，新任务起不来（`DEPLOY_BUSY`）。
   - 只认 tag 形如 `<本分支>-<14位时间戳>-online` 的任务（平台 GraphQL 里 `gitlabCommit.tag`，
     老数据可能只填在 `gitlabCommit.branch`，两个字段都看）；
   - 别的分支、别的 Jira、已完成（`done`）或已取消（`cancel`）的任务**一律不动**，避免关错发布；
   - 已经推进到生产段（`section !== 'preview'`）的旧任务也不自动取消——那时可能已经切了流量，
     只打警告跳过，交给人去任务页处理；
   - 取消后轮询到 `taskStatus` 为 `cancel`/`done` 才继续，轮不到报 `CANCEL_TIMEOUT`；
   - `--stop-before-deploy` 停在部署前时不会取消；取消结果写在最终 JSON 的 `canceled` 里。

## 发布前体检（自动执行）

`release.mjs` 动手之前先查平台的发布记录（`app.deployHistory`）：

1. `status` 不是 `done` / `cancel` 的记录即「发布中」；
2. 对每个发布中任务，取它的 `gitlabCommit.id`，判断是否已在 `origin/master` 上
   （先查本地 `git merge-base --is-ancestor`，本地没有该提交再走 GitLab API `commits/<sha>/refs`）；
3. 只要有一个「发布中但没合 master」，就**停下不发布**，报 `IN_PROGRESS_UNMERGED` 并列出这些任务——
   防止别人发了线上却漏合 master，你在错误基线上再发一版（本分支自己上一次的发布不算，它会在新版本就绪后被取消）；
4. 都合过了就打印一行确认，继续发布。

确认无碍可加 `--ignore-in-progress` 跳过这条体检。

**发完要收尾**：部署任务会一直 `running` 占着部署包（挡住别人的 `DEPLOY_BUSY`），而只要它的 commit 还没进
master，别人跑 `release.mjs` 又会撞上 `IN_PROGRESS_UNMERGED`——两头堵。所以发到预发后尽快
`node mr-master.mjs --yes` 把分支合进 master，并在平台把任务点掉。2026-10-30 OS-43580 发完即处于
这个状态（任务 `2093543`、commit `22781fb3` 未合 master），dry-run 当场被自己的任务拦住。
你自己用同一个分支重发不会被这两条挡住：有新提交时脚本会先把本分支的旧任务取消掉，再部署新版本。

## 打 tag 的规则（按本机既有做法）

在**当前分支**上依次执行五步，然后去 Medusa 等镜像：

1. `git pull` 拉当前分支（远端还没有这条分支时自动跳过，交给第 3 步推送）
2. `git pull origin master` 把 master 合进来
3. `git push`
4. **push 之后自动建 MR**（只建不合，已存在 open MR 复用，分支提交都在 master 时跳过），失败报 `MR_CREATE_FAILED`；不需要也不想建时加 `--no-mr`。
5. `git tag <当前分支名>-<时间戳>-online` 并 `git push origin <tag>`

- tag 名规则与本机 `git tag-online` 完全一致（`<branch>-<yyyyMMddHHmmSS>-online`，打在 HEAD 上）；
- 打 tag 前默认跑 `pnpm tsc`（与 `gton` 一致），失败不打 tag，可用 `--no-tsc` 跳过；
- 工作区不干净会直接拒绝，避免 `pull` 出乱子；
- 不依赖 MR 是否已合并：默认不检查 MR 合并状态；需要额外保险时加 `--require-mr-merged`。
- **MR 只建不合**：合并是用户的决定。要合进 master 用 `mr-master.mjs`（见下文），它会对已存在的 open MR 直接复用。

## 部署包被占用（别人正在发版）

一个部署包（默认 `deploy`）同时只能跑一个部署任务。如果轮到你部署时平台报
`部署包有正在执行的部署任务`，说明**别人的发布还没结束**，而且不确定要等多久——所以脚本不替你决定：

1. 先从发布记录里查出占用方，打印成「占用者、Jira、任务号、分支」，例如
   `（占用者：zhangbohan1，OS-43558 / 任务 2092183 / agent-system-v1.1-20261029145744-online）`；
2. 交互环境（TTY）直接问「等它释放后自动重试吗？(Y/n)」；
3. 非交互环境（agent 跑的）**默认停下**，返回 `DEPLOY_BUSY` + `details` 里的占用方，由调用方拿这个去问用户；
4. 用户说等，就用 `--on-deploy-busy wait` 重跑：镜像和审批都已就绪，会跳过打 tag/等构建/发起审批，
   每 3 分钟重试一次，最多等 `--wait-minutes`（默认 30 分钟），超时报 `DEPLOY_BUSY_TIMEOUT`。

```bash
# 被占用时先看是谁：这条只读，不会提交部署
node release.mjs --tag OS-43538-20261029195356-online --jira OS-43538 --status

# 用户确认等待后
node release.mjs --tag OS-43538-20261029195356-online --jira OS-43538 --yes --on-deploy-busy wait
```

注意：占用任务结束后，还需要有人在平台任务页点一次「完成发布」，它才会真正释放部署包；
一直 `running` 的任务会把后续所有发布都挡在 `部署包有正在执行的部署任务` 这一步。

## 可选：把分支合进 master

发布流程会在 push 之后**自动**建 MR（只建不合），**合并**是另一个动作。确认要合进 master 时用 `mr-master.mjs`：

1. `git fetch origin master`
2. 把 `origin/master` 合进当前分支（工作区不干净会拒绝；冲突则回滚并列文件）
3. 推送当前分支
4. 建 `<当前分支> → master` 的 MR 并合并（幂等：已有 open MR 就直接复用、不再新建）

**默认预演**，加 `--yes` 才执行；`--no-merge` 只建 MR；`--no-sync-master` 跳过第 2 步。
它**不在** `release.mjs` 的主流程里，按需单独跑。

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--jira` | 无 | Jira 号（用于审批）；同时触发「pull → 合 master → push → 打 tag」这套仓库动作 |
| `--tag` | 无 | 直接用已有 tag，不再打 tag |
| `--app` | `oversea/global-station` | 平台应用 id |
| `--env` | `online` | 目标环境 |
| `--package` | `deploy` | 部署包名 |
| `--branch` | 当前分支 | 打 tag 用的分支名，tag = `<branch>-<时间戳>-online` |
| `--require-mr-merged` | 关 | 额外要求该分支有已合入 master 的 MR |
| `--ignore-in-progress` | 关 | 跳过「发布中任务是否已合 master」的体检 |
| `--no-tsc` | 关 | 跳过打 tag 前的 `pnpm tsc` |
| `--smoke` / `--health` / `--db-change` | 是 / 是 / 无 | 审批表单里的三个必填项 |
| `--yes` | 关 | 跳过「确认打 tag」的交互（非 TTY 环境必须加） |
| `--dry-run` | 关 | 全部只读：不提交审批、不部署 |
| `--status` | 关 | 只读查询当前状态 |
| `--stop-before-deploy` | 关 | 审批通过后停在部署前 |
| `--no-mr` | 关 | 跳过 push 之后自动建 MR（默认会建，只建不合） |
| `--force` | 关 | 本分支自上次发布没有新提交也强制重发（默认遇到无改动会跳过） |
| `--wait-minutes` | 30 | 等构建 / 等部署包释放的最长时间 |
| `--approve-minutes` | 180 | 等审批的最长时间 |
| `--on-deploy-busy` | `ask` | 部署包被别人的部署任务占着时怎么办：`ask` 交互环境问人、非交互环境直接停；`wait` 自动等待重试；`exit` 直接停 |
| `--deploy-mode` | `fast` | 建完任务后点什么：`fast` 快速部署（直接全量到预览实例）、`flow` 灰度部署、`manual` 只建任务不点 |
| `--preview-cluster` | `online1` | 预览实例段用的集群，与平台页面一致，一般不用改 |
| `--poll-ms` | 15000 | 轮询间隔 |
| `--base-url` | `https://medusa-new.guazi-apps.com` | 测试时可指向 mock |

## 执行规范（agent 必须遵守）

1. 用户说「上线 / 发布」但没说清 Jira 号或 tag 时，**先确认再动手**。
2. 动手顺序：`--status` →（需要时）`--dry-run` → 把 tag、需求名称、产品负责人、冒烟/健康检查/DB 变动念给用户确认 → 再 `--yes` 正式执行。
   若输出 `skipped:true / NO_CHANGES`，说明该分支自上次发布没有新提交，如实告诉用户，不要自作主张加 `--force`。
3. **MR 由脚本自动创建**（push 之后、打 tag 之前，只建不合）。执行完把 `mr.webUrl` 报给用户即可；不要自己另建一个 MR，也不要合并它。`mr.skipped` / `nothing-to-merge` 时无需建。若报 `MR_CREATE_FAILED`，把原因告诉用户，修好后重跑（幂等）。
4. 打 tag 会触发构建流水线、提交审批会在统一审批平台挂到用户名下、部署会动预发环境——这三件事都属于「用户点头才做」。
5. 默认 `--deploy-mode fast` 会在建完任务后自动点「快速部署」，这是**跳过灰度直接全量到预览实例**的动作。
   用户只想看看流程、或想自己决定灰度还是快速，就用 `--deploy-mode manual`。
6. 审批通过后如果要部署，同样先确认；不想等可以 `--stop-before-deploy` 交回用户。
7. 部署时如果报 `DEPLOY_BUSY`，**不要自己决定等或不等**：把占用者（谁、哪个 Jira、哪个任务）报给用户，
   问清楚是等待还是放弃；等待再用 `--on-deploy-busy wait` 重跑，不要不带参数反复重试。
8. 汇报必须如实：失败就说失败，卡在哪一步、原始报错是什么。

## 结果与失败码

成功时 stdout 一行 JSON：

```json
{"ok":true,"tag":"OS-43558-20261029...-online","appCommitID":2672733,"jiraId":"OS-43558",
 "approval":{"approvalID":"TDDEPL...","status":"FinishStatus","result":"SuccessResult"},
 "submittedApproval":true,
 "mr":{"created":true,"iid":1941,"webUrl":"https://gitlab.../merge_requests/1941"},
 "canceled":[{"taskId":777001,"tag":"OS-43558-20261029150000-online"}],
 "deployed":{"taskId":2092183,"taskType":"flow"},"deploySkipped":false}
```

`canceled` 是本次取消掉的本分支旧发布任务（空数组表示没有）；无改动跳过时输出
`{"ok":true,"skipped":true,"reason":"NO_CHANGES","branch":"…","previousTag":"…"}`。

`mr` 的三种形态：`{"created":true,...}` 本次新建；`{"created":false,"iid":...,"webUrl":...}` 复用已有 open MR；
`{"created":false,"reason":"nothing-to-merge"}` 分支提交都已在 master；`{"skipped":true}` 用了 `--no-mr` 或给了 `--tag`。

| code | 含义 | 处置 |
| --- | --- | --- |
| `AUTH_REQUIRED` | 没有有效 GUAZISSO | 让用户跑一次 guazi-sso-login 换 token |
| `TAG_TIMEOUT` | 等构建超时 | 去 GitLab 看 tag 流水线是否失败；或调大 `--wait-minutes` |
| `NO_JIRA` | tag 没带 Jira 号且未传 `--jira` | 问用户这次审批用哪个 Jira |
| `JIRA_UNAVAILABLE` | 取不到产品负责人 | Jira 类型不对（需故事/线上缺陷）或负责人缺失 |
| `PRECHECK_FAILED` | 平台前置校验不过 | 分支前缀不对 / 已部署过 online / 正在部署中 |
| `MR_NOT_MERGED` | 只有加了 `--require-mr-merged` 才会出现：分支没有已合并的 MR | 去掉该参数，或先 `mr-master.mjs` |
| `IN_PROGRESS_UNMERGED` | 平台上有发布中任务且其代码没进 master | 先确认/补上合并，或 `--ignore-in-progress` |
| `DEPLOY_BUSY` | 部署包被别人的部署任务占着，已按默认（非交互）停下；`details` 里有占用者 | 把占用者告诉用户，问「等还是停」；等就加 `--on-deploy-busy wait` 重跑 |
| `DEPLOY_BUSY_TIMEOUT` | 选了等待，但 `--wait-minutes` 用完仍未释放 | 让用户去平台看占用任务卡在哪，或调大 `--wait-minutes` |
| `SECTION_DEPLOY_FAILED` | 任务建好了，但「快速部署/灰度部署」连续 5 次触发失败 | 把原始报错给用户；可在任务页手工点，或用 `--deploy-mode manual` 重跑到建任务为止 |
| `SECTION_DEPLOY_TIMEOUT` | 预览实例这一段 `--wait-minutes` 内没走完 | 去任务页看事件与副本状态（`副本 0/1` 常见于镜像拉取慢） |
| `MERGE_CONFLICT` | 合 master 时冲突 | 手工解冲突后重跑（脚本不做自动解决） |
| `MR_CREATE_FAILED` | push 后自动建 MR 失败（glab 不可用、远端不是 GitLab、分支已被别的 open MR 占用等） | 按报错修好后重跑（幂等）；这次确实不需要 MR 就加 `--no-mr` |
| `CANCEL_FAILED` | 平台拒绝取消本分支旧发布任务（任务可能已走完生产段、状态不允许取消） | 去任务页看任务状态与报错，必要时人工处理后再重跑 |
| `CANCEL_TIMEOUT` | 取消本分支旧发布任务后 5 分钟内平台没生效，部署包可能仍被占用 | 去任务页确认旧任务状态；等它变 `cancel` 后重跑 |
| `DIRTY_WORKTREE` | 工作区有未提交改动 | 先提交或 stash |
| `APPROVAL_REJECTED` | 审批被驳回 | 把结果告诉用户，等其决定是否重发 |
| `APPROVAL_TIMEOUT` | 等审批超时 | 提示去统一审批平台看卡在谁那里 |

## 接口与判定口径

全部是 `POST {base}/graphql?env=<env>`，10 个操作：

| 步骤 | 操作 |
| --- | --- |
| 找镜像 | `getDeployImages`（`commitsWithPage(deployed:false)`，分页） |
| 带出需求/负责人 | `queryJiraPMAndName` |
| 审批前置校验 | `checkJiraID`、`checkCanApprovalByJiraID` |
| 发起审批 | `startReleaseApproval` |
| 查审批 | `getDeployApprovals` |
| 部署 | `getDeployPackageOptions`、`queryRepeatCommitByJira`、`deploy2` |
| 触发分段部署 | `updateDeploySection`（快速/灰度/取消/完成发布）、`queryDeploySection`（看这一段走到哪） |

- **审批通过** = `status === 'FinishStatus' && result === 'SuccessResult'`。
- **镜像级审批状态** `approvalStatus`：`NotNeedApproval` / `SuccessResult` 可直接部署；`NeedApproval` 待发起；`ApprovalGoingOn` 审批中；`ApprovalReject` 被拒。
- **`lastDeployHistory` 只记线上集群**（`online3` 之类），部署到预发不会写这里，所以「线上未部署」不代表没发过预发。
- **审批详情以 Medusa 为准**：平台自己的审批详情页与部署弹窗都读 `releaseApprovals`，不需要去 todo 平台查。

## 盯发布任务（替代平台通知）

```bash
node watch-release.mjs --task 2092183 --once                  # 只看当前状态
node watch-release.mjs --task 2092183 --merge-master --yes    # 等流程结束→自动合 master
```

`--task` 就是部署任务页 URL 里的数字。它轮询 `queryDeploySection` 的 `taskStatus`，
离开「进行中」集合（running / created / creating / StepPaused / deploying / pending / waiting）即视为结束；
带 `--merge-master --yes` 时结束后自动跑 `mr-master.mjs --yes`。

## 关于「完成发布」与平台通知

发布任务页（`/event/overview`、`deploytask/<id>`）的事件流是**只读**的（`queryDeployEvent`）；
旁边的「监控报警」tab 只能生成报警规则，订阅/发送通知要在 NIER 平台做——**平台不会主动推事件给我们**。
所以要「点完完成发布就自动做下一步」，只能轮询任务状态：`queryDeploySection(taskID)` 的 `taskStatus` 从
`running` 变为终态即视为流程结束，再触发后续动作（如 `mr-master.mjs`）。

## 自测（不碰线上）

```bash
node ~/.zcode/skills/online-release/scripts/selftest.mjs
```

起一个 mock 网关，覆盖「完整流程 / `--stop-before-deploy` / 无登录态 / 部署包被占用（默认停下、等待重试）/ 重发取消上次发布」六个用例。

## 排错

- `token is empty` → 没带 GUAZISSO；`错误信息：参数错误` → token 过期，让用户强刷一次。
- `git push` 被拒 → 远端已有同名 tag，换时间戳重来。
- 首次在某台机器跑 → 确认 `guazi-sso-login` 已 `init`（账号密码缓存在本机）。
- `fatal: couldn't find remote ref <branch>` → 分支还只在本机。脚本现在会先 `git ls-remote` 判断远端有没有，
  没有就跳过第 1 步 pull、由第 3 步 push 上去；旧版会直接失败，遇到时 `git push -u origin <branch>` 再重跑。
- 平台 422 `unknown field … ["variable","input","id"]` → `deploy2` 的入参形状不对。`DeployInput` 只有
  `appID / appCommitID / deploymentConfigID / jiraID` 四个字段，把镜像行（带 `id`、`gitlabCommit`）整块塞进去
  就会报这个。2026-10-30 正是 `main()` 把镜像对象误传给 `submitDeploy(client, args, image, jiraId)` 触发
  （该函数签名是 `(client, args, input)`），已修为调用负责拼入参的 `deploy()`。字段不确定时直接
  introspection 查 `__type(name:"DeployInput")`，别猜。
