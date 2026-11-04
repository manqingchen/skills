---
name: create-mr
description: 在 GitLab 仓库把当前任务分支创建为到 master 的 Merge Request 并返回链接。当用户说「建 MR」「创建 MR」「搞个 MR 链接」「提 MR」「MR 到 master」等要求产出 Merge Request 时使用。内置查重（已存在直接回链接）、从分支名与未合并提交自动生成标题、按「变更摘要 / 关联任务 / 影响环境 / 验证方式」四段模板生成描述（关联任务必须给 Jira 与 cwiki 链接）、glab mr create 一条龙。
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

**不要用 `glab mr list --source-branch`**：当前 glab 版本该 flag 会直接 SIGSEGV（`DisplayAllMRs` nil pointer），拿不到结果。改用 REST API：

```bash
glab api "projects/:id/merge_requests?source_branch=<当前分支>&state=all" | python3 -c "
import sys,json
data=json.load(sys.stdin)
if not data: print('NO_MR_FOUND')
for m in data: print(m['iid'], m['state'], m['web_url'], '|', m['title'])
"
```

已存在任何状态的 MR（open / merged / closed）→ 直接把链接返回给用户，**结束，不重复创建**。只有 open 状态的旧 MR 但内容明显过期时，提醒用户自行决定关旧建新，不擅自操作。

## 第 2 步：取材

- **任务号**：从分支名提取（如 `OS-43401`、`feature/ABC-123` 取 `ABC-123`）；提取不到则从提交历史找，再没有就问用户。
- **Jira 链接**：固定为 `https://cjira.guazi-corp.com/browse/<任务号>`。curl 直连会 302 跳 SSO 登录页，这是未登录的正常现象，链接本身有效，不要据此判断任务不存在。
- **cwiki 链接**：取该需求在 cwiki 的**父页**，用 CQL 按任务号搜：
  ```bash
  CK="$(bash ~/.zcode/skills/guazi-sso-login/run.sh wiki --validate --plain)"
  curl -s -H "Cookie: $CK" \
    "https://cwiki.guazi.com/rest/api/content/search?cql=title%20~%20%22<任务号>%22&limit=20" \
    | python3 -c "
  import sys,json
  for r in json.load(sys.stdin).get('results',[]):
      print(r['id'],'|',r['title'])
  "
  ```
  判定父页：标题以任务号开头（如 `OS-43643 独立站日期与时间展示统一`），子页不重复任务号。搜不到就写「Wiki：待补充」并提醒用户，**禁止编造 pageId**。
- **标题**：取能代表整条分支的提交，通常是主功能 `feat(任务号): …`，不要盲取最新一条提交——多提交分支的最新提交往往是收尾 `fix(...)`，代表不了整体。只有一条提交时直接用该条；拿不准就取 `feat` 主提交，并在报告里说明取值理由。
- **提交列表**：`git log master..<当前分支> --oneline`（目标分支按用户指定替换），作为变更摘要素材；同时跑 `git diff master...<当前分支> --stat` 把握改动面，摘要按主题归纳而不是逐条罗列 commit。
- **验证素材**：若同批改动已合入 develop，用 `git branch -r --contains <HEAD>` 确认后写进验证方式（这是「已验证过」的实证）；有 develop merge commit 短哈希与流水线编号就一并带上，没有就留待用户补充。

## 第 3 步：描述（四段模板，缺一不可）

```markdown
## 变更摘要

- 按主题归纳的改动点（文件/模块可括号标注）

## 关联任务

- Jira：<https://cjira.guazi-corp.com/browse/OS-xxxxx>
- Wiki：<https://cwiki.guazi.com/pages/viewpage.action?pageId=xxxxxxx>（父页标题）

## 影响环境

<全端 / 仅 PC / 仅 H5 / 指定页面；行为变化点>

## 验证方式

<测试环境验证步骤与预期结果；无则写明待验证>
```

「关联任务」必须是上面两条可点击链接，**不能只写裸任务号**：评审人要能一键跳到需求单和设计文档。

## 第 4 步：创建

描述写进临时文件（避免 shell 转义问题；每次覆盖写，别复用上一轮残留内容），然后：

```bash
glab mr create --push \
  --source-branch <当前分支> \
  --target-branch master \
  --title "<取材得到的标题>" \
  --description "$(cat /tmp/mr-desc.md)"
```

- `--push` 必带：兜底远端分支不存在的情况。
- 创建成功后把 MR 链接以 Markdown 形式返回（标题 + 链接），并附一句摘要（分支、提交数）。
- 完成报告末尾固定提示衔接：**「MR 评审合并后，可用 gton（打 tag 发布线上）完成上线」**，形成 create-mr → 合并 → gton 的完整发布链。

## 硬性约束

- 禁止任何编辑器交互 flag（`--description "-"` 等），描述一律显式传值。
- 关联任务段必须是 Jira 链接 + cwiki 父页链接；pageId 只能来自 CQL 查询结果，禁止推测或套用其他需求的 ID。
- 查重一律走 `glab api`，不要用会崩溃的 `glab mr list --source-branch`。
- 只创建，不合并、不关闭、不删分支；需要这些操作时让用户确认。
- 查重命中已有 open MR 时绝不重复创建。
- 标题不自拟，以分支提交历史中代表整体改动的那条为准（通常为 `feat` 主提交）；描述不改写用户已验证过的事实（如流水线编号）。
