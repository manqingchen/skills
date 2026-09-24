---
name: gton
description: 线上发布：校验 MR 已合并进 master 后，调用本地 gton 命令（pnpm tsc + 打 <分支>-<时间戳>-online 标签并推送）触发 online 生产流水线，后台监听流水线并在结束时用 macOS 系统通知提醒。当用户说「gton」「打 tag」「打 tag 发布」「上线」「发布线上」时使用。MR 未合并或分支落后 master 时停下阻止发布。
---

# gton：MR 合并校验 → 打 tag → 监听 online 流水线

依赖本地命令 `gton`（`pnpm tsc` 校验 → `git tag-online` 打 `<branch>-<时间戳>-online` 标签并推送、打开流水线页面）。本 skill 负责串联完整链路：**合并校验 → 执行 gton → 监听 tag 流水线 → 系统通知**。

## 第 0 步：预检

```bash
glab auth status            # 未认证停下报告
command -v gton             # 不存在则报告；用户同意后退化为等价命令：pnpm tsc && git tag + push
git branch --show-current   # detached HEAD 报告结束
git status --short          # 脏工作区不阻塞，但报告中提醒
```

## 第 1 步：合并校验（防止把未进 master 的内容发线上）

```bash
glab mr list --source-branch <当前分支> --all
```

- 无 MR → 停下：提示先用 create-mr 建 MR 并完成评审合并
- MR 存在但 `state != merged` → 停下：**不发布**，报告当前状态（opened / locked 等）

MR 已 merged 后再校验分支基线：

```bash
git fetch origin master
git merge-base --is-ancestor origin/master HEAD
```

不满足（当前分支落后 master，常见于合并产生了 merge commit）→ 停下：提示用户先把 master 合入当前分支（或按仓库习惯 rebase/ff 同步）后再发布。

## 第 2 步：执行 gton

```bash
gton
```

- `pnpm tsc` 失败或 tag 推送失败 → 如实报告，**不得绕过 tsc 直接打 tag**
- 成功后解析刚打的 tag 备用：

```bash
git tag --points-at HEAD | grep -E '\-online$' | head -1
```

## 第 3 步：监听 tag 流水线（后台任务挂起）

```bash
# 取 tag 对应流水线 id
glab api "projects/:id/pipelines?ref=<tag>&per_page=1"
# 每 20s 轮询状态（注意：zsh 中 status 是只读变量，换变量名如 pipe_state）
```

结束后用 macOS 系统通知（成功 / 失败都弹）：

```bash
osascript -e "display notification \"online 流水线 <状态> · <tag>\" with title \"ZCode · 线上发布\" sound name \"Glass\""
```

失败时归因：`glab api projects/:id/pipelines/<id>/jobs` 定位失败 job → `glab api projects/:id/jobs/<job-id>/trace` 拉日志尾部，只报结论与关键报错。

## 硬性约束

- 不自动合并 MR、不自动重试流水线、不删 tag；这些操作一律让用户决定
- tag 一律经 `gton` 打（自带 tsc 门禁），禁止手写 `git tag` 绕过校验
- 监听必须走 `glab api` 轮询：`glab ci status --branch` 对非当前检出分支 / tag 不可靠
- MR 校验不通过时即使 gton 能跑也必须停下，这是本 skill 存在的核心价值
