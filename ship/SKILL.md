---
name: ship
description: 命令式发布流程（唯一方向 FEATURE -> DEVELOP）：方向断言 -> 提交 -> 推送 feature -> 在 develop worktree 合并 feature -> 推送 develop -> 监听流水线。用户明确说「ship」「发布」「合到 develop」「提交推送并合到 develop」时执行；只说「提交」「提交推送」时只做提交与推送，不合并 develop。
---

# ship — 命令式发布（唯一方向：FEATURE → DEVELOP）

> 本文件是命令清单，不是建议。严格按 `RUN` 执行；`ASSERT` 失败或出现 `FORBIDDEN` 命令立即 `STOP`。
> 不做语义推断，不交换合并方向：**只能把 FEATURE 合进 DEVELOP，禁止把 DEVELOP 合进 FEATURE。**

## 0. 方向规则（先读，违反即 STOP）

唯一允许的合并命令：

```bash
git -C "$DEVELOP_WT" merge --no-ff --no-edit "$FEATURE"
```

执行前必须通过 §5.2 的方向断言。

FORBIDDEN（出现任意一条立即 STOP）：

```bash
git merge develop                 # 在 FEATURE 分支执行 = 事故
git merge origin/develop          # 同上
git rebase develop                # 同上
git pull origin develop           # 同上
git checkout -- .                 # 丢弃改动
git reset --hard                  # 除非用户明确要求
git push --force                  # 禁止
git push --force-with-lease       # 禁止
git commit --no-verify            # 禁止绕过钩子
git add -A                        # 禁止
git add .                         # 禁止
git commit -- <paths>             # 禁止（会与 lint-staged 冲突）
```

合并方向自检（每次 merge 前打印）：

```text
FEATURE = <当前分支>
DEVELOP_WT = <develop worktree 路径>
DEVELOP_BRANCH = <develop worktree 当前分支，必须是 develop>
MERGE_CMD = git -C $DEVELOP_WT merge --no-ff --no-edit $FEATURE
```

## 1. 变量与预检（RUN）

```bash
git fetch origin --prune
FEATURE=$(git branch --show-current)
git status --short --branch
git worktree list
git ls-remote --heads origin develop
```

ASSERT：

```bash
[ -n "$FEATURE" ] || { echo "STOP: detached HEAD"; exit 1; }
[ "$FEATURE" != "develop" ] || echo "NOTE: 当前就是 develop，只做提交+推送，跳过 §5/§6"
git ls-remote --heads origin develop | grep -q "refs/heads/develop" || { echo "STOP: 远端无 develop"; exit 1; }
```

无内容可发布时直接结束：

```bash
DIRTY_COUNT=$(git status --short | wc -l | tr -d ' ')
AHEAD_COUNT=$(git rev-list --count "origin/$FEATURE..$FEATURE" 2>/dev/null || echo 0)
if [ "$DIRTY_COUNT" -eq 0 ] && [ "$AHEAD_COUNT" -eq 0 ] && git merge-base --is-ancestor "$FEATURE" origin/develop; then
  echo "NOTHING_TO_SHIP"
  exit 0
fi
```

任务号从 `$FEATURE` 提取（`OS-43401`、`feature/ABC-123` -> `ABC-123`）；提取不到用 `git log --oneline -10` 的既有风格。

## 2. 提交（RUN）

若项目已安装 smart-commit / gz-smart-commit，调用它完成本阶段（只提交，不推送）；否则：

```bash
git status --short
git diff
git diff --cached
```

按「一个提交只做一件事」分组；同一文件不同功能用精确 patch 拆分。提交前留安全网：

```bash
git diff > /tmp/ship-backup.patch
```

提交命令（只用显式路径）：

```bash
git add <file1> <file2>
git commit -m "type(任务号): 标题" -m "- 改动说明 (模块路径)"
```

ASSERT：

```bash
git status --short          # 干净，或只剩下一组未提交文件
git log --oneline -1
```

全部提交完成后，如项目有 lint 脚本，RUN 一次全量检查（如 `pnpm lint` / `pnpm tsc`），只作为最终校验。

记录推送前提交列表（必须保存，推送后 origin 引用会变）：

```bash
if git show-ref --verify --quiet "refs/remotes/origin/$FEATURE"; then
  PUSH_BASE="origin/$FEATURE"
else
  PUSH_BASE=$(git merge-base HEAD origin/develop)
fi
PUSH_BASE_SHA=$(git rev-parse "$PUSH_BASE")
COMMITS_BEFORE_PUSH=$(git log --oneline "$PUSH_BASE_SHA"..HEAD)
printf '%s\n' "$COMMITS_BEFORE_PUSH"
```

## 3. 推送 FEATURE（RUN）

```bash
git push origin "$FEATURE"
```

ASSERT：推送输出包含 `$FEATURE -> $FEATURE`，没有 `rejected`。

## 4. 定位 DEVELOP worktree（RUN）

```bash
DEVELOP_WT=$(git worktree list | awk '$3=="[develop]"{print $1; exit}')
TEMP_WT=0
if [ -z "$DEVELOP_WT" ]; then
  [ ! -e /tmp/ship-develop ] || { echo "STOP: /tmp/ship-develop 已存在"; exit 1; }
  git show-ref --verify --quiet refs/heads/develop || git fetch origin develop:develop
  git worktree add /tmp/ship-develop develop
  DEVELOP_WT=/tmp/ship-develop
  TEMP_WT=1
fi
```

ASSERT：

```bash
[ -d "$DEVELOP_WT" ] || { echo "STOP: develop worktree 不存在"; exit 1; }
```

## 5. 合并 FEATURE -> DEVELOP（RUN）

### 5.1 同步 develop

```bash
git -C "$DEVELOP_WT" status --short
read LEFT RIGHT <<< "$(git rev-list --left-right --count origin/develop...develop)"

if [ "$RIGHT" -gt 0 ]; then
  echo "STOP: 本地 develop 领先/分叉"
  exit 1
fi

if [ "$LEFT" -gt 0 ]; then
  [ -z "$(git -C "$DEVELOP_WT" status --short)" ] || { echo "STOP: develop worktree 脏，禁止 ff 同步"; exit 1; }
  git -C "$DEVELOP_WT" merge --ff-only origin/develop
fi
```

### 5.2 方向断言（必须全部通过）

```bash
[ "$FEATURE" != "develop" ] || { echo "STOP: 当前分支是 develop，跳过合并"; exit 0; }
[ "$(git -C "$DEVELOP_WT" branch --show-current)" = "develop" ] || { echo "STOP: worktree 不在 develop"; exit 1; }
git -C "$DEVELOP_WT" rev-parse --verify "$FEATURE^{commit}" >/dev/null || { echo "STOP: feature 不存在"; exit 1; }
```

打印方向自检：

```bash
echo "FEATURE=$FEATURE"
echo "DEVELOP_WT=$DEVELOP_WT"
echo "DEVELOP_BRANCH=$(git -C "$DEVELOP_WT" branch --show-current)"
echo "MERGE_CMD=git -C $DEVELOP_WT merge --no-ff --no-edit $FEATURE"
```

### 5.3 脏文件交集检查

```bash
CHANGED=$(git diff --name-only develop..."$FEATURE")
DIRTY=$(git -C "$DEVELOP_WT" status --short | awk '{print $2}')
comm -12 <(printf '%s\n' "$CHANGED" | sort -u) <(printf '%s\n' "$DIRTY" | sort -u)
```

ASSERT：输出为空；否则 STOP。

若 DIRTY 非空但与 CHANGED 无交集：可继续，完成后在报告里提醒这些脏文件仍在（不会被推送）。

### 5.4 执行唯一允许的 merge

```bash
git -C "$DEVELOP_WT" merge --no-ff --no-edit "$FEATURE"
```

ASSERT：

```bash
git -C "$DEVELOP_WT" rev-parse --short HEAD
```

### 5.5 冲突处理

```bash
CONFLICTS=$(git -C "$DEVELOP_WT" status --short | grep -E '^(UU|AA|DD|AU|UA|DU|UD)' || true)
[ -z "$CONFLICTS" ] || { echo "$CONFLICTS"; echo "STOP: merge conflict"; exit 1; }
```

有输出 -> STOP，报告冲突文件。不要执行：

```bash
git merge develop
git rebase develop
git checkout <feature> && git merge develop
```

只有用户明确说「解决冲突并重新 ship」时，才在 `$DEVELOP_WT` 内解决冲突；解决后从 §5.4 继续，禁止把 develop 合回 feature。

## 6. 推送 DEVELOP（RUN）

```bash
git -C "$DEVELOP_WT" push origin develop
```

ASSERT：输出包含 `develop -> develop`，没有 `rejected`。

记录本次 develop HEAD（清理 worktree 后还要用它匹配流水线）：

```bash
DEVELOP_HEAD=$(git -C "$DEVELOP_WT" rev-parse HEAD)
```

清理临时 worktree（仅 `TEMP_WT=1`）：

```bash
[ "$TEMP_WT" = "1" ] && git worktree remove "$DEVELOP_WT"
```

## 7. 监听流水线（RUN，后台）

优先用 `glab api` 轮询（`glab ci status --branch` 在部分项目会忽略参数）。必须匹配本次 develop HEAD 的 SHA，避免读到上一条流水线：

```bash
PIPELINE_SHA=$DEVELOP_HEAD
glab api "projects/:id/pipelines?ref=develop&per_page=10"
```

轮询到 `pipeline.sha == $PIPELINE_SHA` 且 `status` 为 `success` / `failed` / `canceled`。失败时：

```bash
glab api "projects/:id/pipelines/<PIPELINE_ID>/jobs"
glab api "projects/:id/jobs/<JOB_ID>/trace" | tail -100
```

通过时确认 `release-test` job 为 `success`（测试环境已发布）。

## 8. 完成报告（RUN 取证）

```bash
printf '%s\n' "$COMMITS_BEFORE_PUSH"
git rev-parse --short "$FEATURE"
git rev-parse --short "$DEVELOP_HEAD"
```

报告必须写清：

- FEATURE 推送区间（如 `db068ce9c..8f5ff6ff3`）；
- DEVELOP merge commit 短哈希；
- 合并方向：`FEATURE -> DEVELOP`（不是反过来）；
- 冲突/脏文件/临时 worktree 清理情况；
- 流水线状态（监听中 / success / failed + 失败 job）。
