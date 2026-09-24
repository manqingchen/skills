---
name: create-mr
description: 在 GitLab 仓库把当前任务分支创建为到 master 的 Merge Request 并返回链接。当用户说「建 MR」「创建 MR」「搞个 MR 链接」「提 MR」「MR 到 master」等要求产出 Merge Request 时使用。内置查重（已存在直接回链接）、从分支名与未合并提交自动生成标题、按「变更摘要 / 关联任务 / 影响环境 / 验证方式」四段模板生成描述、glab mr create 一条龙。
---

# create-mr：当前分支 → master 的 Merge Request

把当前任务分支建成 MR 并返回链接。目标分支默认 `master`，用户显式指定其他目标时覆盖。

## 第 0 步：预检

```bash
glab auth status          # 未认证则停下报告，不要自行处理凭据
git branch --show-current # 输出为空 = detached HEAD，报告后结束
git status --short        # 有未提交改动不阻塞建 MR，但需在报告中提醒
```

## 第 1 步：查重（必做，避免重复创建）

```bash
glab mr list --source-branch <当前分支> --all
```

已存在任何状态的 MR（open / merged / closed）→ 直接把链接返回给用户，**结束，不重复创建**。只有 open 状态的旧 MR 但内容明显过期时，提醒用户自行决定关旧建新，不擅自操作。

## 第 2 步：取材

- **任务号**：从分支名提取（如 `OS-43401`、`feature/ABC-123` 取 `ABC-123`）；提取不到则从提交历史找，再没有就问用户。
- **标题**：沿用该分支最新一条 commit message（`type(任务号): 标题` 格式），不要自拟新标题。
- **提交列表**：`git log master..<当前分支> --oneline`（目标分支按用户指定替换），作为变更摘要素材；同时跑 `git diff master...<当前分支> --stat` 把握改动面，摘要按主题归纳而不是逐条罗列 commit。
- **验证素材**：若本会话刚走过 ship / develop 发布，带上 develop merge commit 短哈希与流水线编号；没有就留待用户补充。

## 第 3 步：描述（四段模板，缺一不可）

```markdown
## 变更摘要

- 按主题归纳的改动点（文件/模块可括号标注）

## 关联任务

<任务号>

## 影响环境

<全端 / 仅 PC / 仅 H5 / 指定页面；行为变化点>

## 验证方式

<测试环境验证步骤与预期结果；无则写明待验证>
```

## 第 4 步：创建

描述写进临时文件（避免 shell 转义问题），然后：

```bash
glab mr create --push \
  --source-branch <当前分支> \
  --target-branch master \
  --title "<取材得到的标题>" \
  --description "$(cat /tmp/mr-desc.md)"
```

- `--push` 必带：兜底远端分支不存在的情况。
- 创建成功后把 MR 链接以 Markdown 形式返回（标题 + 链接），并附一句摘要（分支、提交数）。

## 硬性约束

- 禁止任何编辑器交互 flag（`--description "-"` 等），描述一律显式传值。
- 只创建，不合并、不关闭、不删分支；需要这些操作时让用户确认。
- 查重命中已有 open MR 时绝不重复创建。
- 标题不自拟，以分支最新 commit message 为准；描述不改写用户已验证过的事实（如流水线编号）。
