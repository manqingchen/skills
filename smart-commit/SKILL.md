---
name: smart-commit
description: 智能分批提交当前改动：敏感文件预检（凭证/env/密钥绝不入库）→ 收集全部改动 → 按「一个提交只做一件事」逻辑分组 → conventional 中文提交 → 核对并输出分组清单。只提交，不推送、不合并。当用户说「提交」「commit」「分批提交」「smart-commit」「把改动提交了」等只要提交意图（不含 develop/发布/推送）时使用；也是 ship 技能第 1 步（提交阶段）的委托执行者——ship 调用它时完成提交即返回，不附带收尾建议。
---

# smart-commit：敏感预检 → 逻辑分组 → 分批提交 → 核对

四个阶段严格按顺序执行。任何阶段发现风险（敏感文件、detached HEAD、分组拿不准）都停下来向用户报告，不要自行绕过。**本技能只负责提交，不推送、不合并 develop**；用户需要推送/发布时提示改用 `/ship`。

## 第 0 步：预检（敏感文件是红线）

```bash
git status --short --branch
git branch --show-current        # 输出为空 = detached HEAD，报告后结束
```

- 无任何改动 → 告诉用户没有要提交的内容，结束。
- **敏感文件检查**（命中任何一条就停下报告，绝不 add）：
  1. untracked 里是否混入：`.env` / `.env.*`（除 `.example`）、`*secret*`、`*token*`、`*credential*`、`*.key`、`*.pem`、`id_rsa*`、`.auth/` 等凭证与登录态文件；
  2. 这些文件若已被 .gitignore 盖住（`git status --ignored --short` 里出现）则安全；**没盖住又像敏感文件 → 先建议补 .gitignore，等用户确认后再继续**；
  3. `git diff --cached` 里是否已有暂存的敏感内容，有则报告。

## 第 1 步：收集全部改动

```bash
git status --short        # untracked 逐个判断归属
git diff                  # 未暂存改动
git diff --cached         # 已暂存改动（保留原暂存意图，归入对应分组）
```

untracked 文件不要只看文件名猜用途，关键文件 Read 一眼再定组。参照 `git log --oneline -10` 摸清本仓库既有提交风格（type 用词、中英文、scope 习惯），后续 message 与之对齐。

## 第 2 步：分组（一个提交只做一件事）

常用逻辑单元，按仓库实际情况组合：

- 基础配置与文档（构建配置 / gitignore / README / 规约文档）
- 功能实现（同一功能跨文件归一组）
- 测试（单测、e2e 基建、e2e 用例——基建与用例分开）
- 样式调整、修复、重构各自独立成组

规则：

- 样式与逻辑分开、修复与功能分开、配置与业务代码分开；
- **同一文件里不同功能的改动要拆开提交**：优先精确 patch（生成 diff → `git apply --cached` 暂存目标 hunk），Edit 临时回退法作为备选；
- 全新仓库首次提交：配置 + 文档一组先行，再按模块分批；
- 拿不准某文件归属哪组时，向用户确认，不要为了凑分组数硬拆。

## 第 3 步：commit message

格式：`type(scope): 标题` + body 列表。

- type ∈ `feat / fix / test / chore / docs / refactor / perf`；
- scope：优先从分支名提取任务号（如 `OS-43401`、`feature/ABC-123` 取 `ABC-123`）；提取不到用模块名（如 `used-cars`、`e2e`）；再没有就省略 scope；
- 标题与 body 用中文（除非仓库既有提交是英文，则以既有风格为准）；
- body：`- 改动说明 (模块路径)`，多文件时才标路径；说明「为什么/覆盖了什么」，不复述 diff。

## 第 4 步：执行与核对

逐组执行，每组一条命令链（`--quiet` 减少噪音）：

```bash
git add <该组文件...> && git commit -m "<message>" --quiet
```

- 某组 add / commit 失败 → 停下报告当前进度（已完成几组、剩余几组），不要强行继续；
- 全部完成后核对：`git log --oneline`（提交数 = 分组数）+ `git status --short`（工作树应干净或仅剩声明不提交的文件）；
- 向用户输出分组清单（每组的提交哈希 + 标题 + 涉及文件数）。

## 与 ship 的接口

- 被 ship 调用时（ship 第 1 步委托）：完成提交即返回，**不要**提示「用 /ship 推送」，交由 ship 继续第 2 步推送；
- 被用户直接调用时：收尾可提示「需要推送/合并 develop 的话用 /ship」。
