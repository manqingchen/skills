---
name: ship
description: 一键发布流程：智能分批提交当前改动并推送当前分支，然后把当前分支合并到 develop（自动定位或创建 develop 的 worktree）并推送 develop，最后挂起监听 develop 流水线结果并在结束后汇报。当用户明确说「提交推送然后把当前分支合并到 develop 再推送」「合到 develop」「发布」「ship」等包含 develop 合并意图的组合指令时使用。用户只说「提交」「提交推送」而不提 develop 时不要自动合并——只做提交与推送，完成后询问是否继续合并 develop。
---

# ship：提交 → 推送 → 合并 develop → 推送 → 盯流水线

四个阶段严格按顺序执行，每个阶段成功后再进入下一个。任何阶段失败（冲突、推送被拒、分叉、脏工作区风险）都停下来向用户报告，不要自行绕过。

**合并 develop 阶段仅在用户明确提到 develop / 发布 / ship 时执行**；用户只说了提交推送时，完成前两个阶段后停下来询问。

## 第 0 步：预检（先 fetch，再判断）

```bash
git fetch origin --prune                # 先更新远端引用，后续所有判断才准确
git status --short --branch
git branch --show-current               # 输出为空 = detached HEAD，报告后结束
git worktree list                       # develop 检出在哪个 worktree
git ls-remote --heads origin develop    # 远端是否存在 develop
```

develop 存在性三级判定：

1. 本地有 develop：记录它是否在某个 worktree（`git worktree list`）。不在 → 第 3 步需临时 worktree。
2. 本地没有、远端有：`git fetch origin develop:develop` 建立本地分支（develop 未被任何 worktree 检出时可用）。
3. 本地和远端都没有：报告后结束。

其他预检结论：

- 当前分支就是 develop：只做提交 + 推送，跳过合并阶段。
- 无改动、且无未推送提交（`git rev-list --count origin/<当前分支>..<当前分支>` 为 0；无远端分支则视为全部未推送）、且本地 develop 已包含当前分支（`git merge-base --is-ancestor <当前分支> develop`）→ 告诉用户没有要发布的内容，结束。
- 任务号从分支名提取（如 `OS-43401`、`feature/ABC-123` 取 `ABC-123`），用于 commit 前缀 `feat(任务号):`；提取不到则沿用 `git log --oneline -10` 的既有风格，或问用户。

## 第 1 步：提交（smart-commit 分组规范）

若项目已安装 smart-commit / gz-smart-commit 类 skill，优先调用它完成本阶段——**它只负责提交分组，不包含推送**，完成后跳到第 2 步。否则按以下精简规则执行：

1. `git status --short` + `git diff`（含 untracked 文件逐个 Read）收集全部改动。
2. 按「一个提交只做一件事」分组：同一功能跨文件归一组；样式与逻辑分开；修复与功能分开。**同一文件里不同功能的改动要拆开提交**：优先用精确 patch（`git apply --cached`）暂存目标 hunk；Edit 临时回退法作为备选。
3. commit message：`type(任务号): 标题` + body 列表（`- 改动说明 (模块路径)`，多文件时才标路径）。
4. 校验分层：优先依赖 pre-commit 钩子的 lint-staged（提交时自动跑）；需要手动校验时只 lint 暂存文件；**全量 lint 只作为全部提交完成后的最终检查**（避免被不属于本提交的未暂存改动干扰）。
5. 推送前先记录本次提交列表：`git log --oneline origin/<当前分支>..<当前分支>`（推送后 origin 引用更新，就查不到了），供完成报告用。

硬性约束（来自实际踩坑）：

- **只用显式路径 `git add <files>`**，绝不 `git add -A` / `git add .`，避免把根目录遗留的 untracked 杂物带进去。
- **禁止 `git commit -- <paths>`**：与 lint-staged 组合会把格式化前的内容恢复回暂存区。正确顺序是 `git add` 后直接 `git commit -m ... -m ...`。
- **禁止 `git commit --no-verify`**（绕过钩子）、**禁止 `git push --force` / `--force-with-lease`**、**禁止自行 `rebase` / `reset --hard` / `checkout -- .` 丢弃改动**。
- 动手做同文件拆分前先留安全网：`git diff > /tmp/ship-backup.patch`（untracked 单独备份）。
- 每次提交后确认 `git status --short` 干净（或只剩下一组的文件）。

## 第 2 步：推送当前分支

```bash
git push origin <当前分支>
```

（当前分支没有 upstream 也没关系，显式指定 origin + 分支名即可。）如实报告推送区间。推送会把本地所有未推送提交带上（包括用户自己此前的提交），属正常，报告时说明。

## 第 3 步：合并到 develop（以**本地 develop** 为基准）

```bash
git -C <develop-worktree> status --short                 # 脏文件？
git rev-list --left-right --count origin/develop...develop
```

先判分叉，**顺序不能反**（left = origin/develop 独有，right = develop 独有）：

- `right > 0`：本地 develop 有远端没有的提交（领先或分叉，两种情况都**一律停下报告**，不要试图 ff-only，分叉时会失败）。
- `left > 0 && right == 0`：本地落后远端，且 develop worktree 干净 → `git -C <develop-worktree> merge --ff-only origin/develop` 快进同步；worktree 脏则停下。
- `left == 0 && right == 0`：已同步。

脏文件与合并范围判定（**用本地 develop，不是 origin/develop**）：

```bash
git diff --name-only develop...<当前分支>    # refs 跨 worktree 共享，当前目录直接跑即可
```

- develop worktree 有未提交改动：与上述文件列表无交集 → 可合并，结束后提醒用户脏文件仍在（未提交，不会被推出去）；有交集 → 停下报告。

执行合并（`--no-ff` 保证产生 merge commit，与仓库 develop 历史的 merge 惯例一致，报告也有据可引）：

- develop 已在 worktree：`git -C <develop-worktree> merge <当前分支> --no-ff --no-edit`
- 本地 develop 无 worktree：
  ```bash
  git worktree add <临时目录> develop
  git -C <临时目录> merge <当前分支> --no-ff --no-edit
  # 第 4 步推送成功后：git worktree remove <临时目录>
  ```
- 冲突：**停下来报告冲突文件**，不要自行解决。

## 第 4 步：推送 develop

```bash
git -C <develop-worktree或临时目录> push origin develop
```

推送被拒（远端又有新提交）：停下报告，不要 force。

## 第 5 步：监听 develop 流水线（推送成功后执行）

第 4 步推送成功后，用**后台任务**（run_in_background）挂起等待流水线结果，随后照常输出完成报告（注明「流水线监听中」），出结果后再补一条汇报：

```bash
# --wait 会挂住直到最新流水线 success/failed/canceled，退出码非 0 即失败
glab ci status --branch develop --wait --compact
```

- 本地 CLI 的「监听」本质是 glab 封装好的轮询（`--wait` 挂住到结束）；真推送（Pipeline Events webhook）需要 GitLab 可达的接收端，本地终端场景不适用。
- 本项目分支推送/MR 不触发流水线，只有 develop 与 `-online` 分支有，等待对象固定是 develop。
- 失败时归因：`glab api "projects/:id/pipelines?per_page=5"` 取最新流水线 ID → `glab api projects/:id/pipelines/<id>/jobs` 定位失败 job → `glab api projects/:id/jobs/<job-id>/trace` 拉日志尾部（约最后 100 行）；只报告结论与关键报错，不贴全量日志。
- 通过时顺带确认 `release-test` job 已执行（即已发布测试环境），提醒用户可去测试环境验证本次改动。

## 完成报告

一段简洁的中文总结，包含：

- 本次提交列表（用第 1 步记录的 `origin/<分支>..<分支>` 范围，不要裸跑 `git log` 列全量历史）；
- 两个分支的推送区间（如 `db068ce9c..8f5ff6ff3`）；
- develop 合并结果：`git -C <worktree> rev-parse --short HEAD` 的短哈希（`--no-ff` 下即 merge commit）；
- 值得注意的事项：develop worktree 里的脏文件、远端新提交被同步进来、用户自己的中间提交被一并推送、临时 worktree 是否已清理等；
- 流水线监听状态：第 5 步已挂起则注明「监听中」，结束后补报最终结果（成功 / 失败原因与失败 job）。
