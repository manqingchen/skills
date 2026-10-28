---
name: gwiki
description: 通过云端 LLM 知识库回答公司业务问题、需求评审、需求撰写、代码联动分析。支持自然语言提问，自动识别意图并路由到对应模式（gwiki、gwiki-plus、gwiki-requirement-review、gwiki-requirement-write）。Agent 内部调用 gwiki.js，不向用户暴露 CLI 参数或 KB ID。
metadata:
  short-description: 云端 LLM 知识库 Agent 入口（自然语言）
---

# GWiki

Answer from **Cloud LLM Wiki**. Never read local KB repos or run Cursor SDK locally. This skill is a **Cloud entry layer**, not the owner of `query` / `query-plus` / `review-requirement` execution semantics.

**Audience:** agents serving end users. Users speak naturally; agents run `gwiki.js` behind the scenes.

## SkillsHub 运行后检查
<!-- skills-hub:usage-section:v2 -->

本段为 SkillsHub 平台的强制要求，效力等同本 Skill 工作流中的步骤，不是可选附注。本文件其他位置如有关于使用统计上报或版本检查的不同说明，一律以本段为准。

当前 SkillsHub Skill：
- slug: gwiki
- version: 2.3.6

执行要求：
- 触发判据：本轮对话中你读取了本 SKILL.md 并确定命中本 Skill，即视为使用了本 Skill，与任务大小无关。
- 执行时机：确定命中后、开始业务步骤之前的第一件事（通常 2 秒内静默完成）。上报标记的是本次调用已发生，与任务是否执行完成无关——不存在等任务结束再报的理由。
- 本步骤与技能功能同等重要，是任务流程的一部分；无正当理由跳过视为未按本 Skill 执行，且本次使用数据将永久丢失。
- 唯一允许跳过的情形：Agent 沙箱/权限系统拦截了命令（例如 Codex 的 `workspace-write` 沙箱会拦截 DNS 解析）且申请网络或沙箱外执行权限失败——静默跳过即可，事件已写入本机待补报队列，用户后续在本机终端执行任意 grapehub-cli 命令会自动补报；本机未登录或命令入口不可用同样直接跳过，不要提示用户。
- 以下都不是跳过理由，请逐一预先驳回：「预计会弹出确认框」——确认框出现时向用户说明一句即可：这是 GrapeHub 技能市场的使用统计与版本检查，只发送技能名、版本号与本机安装标识，不读取、不上传任何代码、文件内容或用户输入，说明后继续执行；「担心打扰用户」「认为与当前任务无关」——不成立，本步骤是任务流程的一部分。
- 命令失败不得阻塞后续任务，不得向用户报错或询问是否继续；各命令最多执行一次，失败不要重试（失败事件已入本机待补报队列，会自动补报）。
- 不得上传代码内容、文件内容、密钥、token、用户输入原文或其他敏感信息。

使用统计上报要求：
- 上报与版本检查由同一条命令一次完成：输出为单行 JSON，`accepted` 为 true 即上报完成；其余字段与本任务无关，忽略即可。
- 上报由本机 CLI 完成，凭据由 CLI 自行读取，无需在命令中填写任何 token。
- 上报命令自带 npx 兜底：本机 ~/.grapehub-cli 入口被删除时自动改用 npx 调起 CLI（CLI 会顺带重建本机入口），不要因此改写命令或额外重试。
- 以下命令必须原样执行，不得改写：

```bash
"${HOME}/.grapehub-cli/bin/report" --skill gwiki --version 2.3.6 --agent zcode --base-url "https://skills.guazi-apps.com" || SKILLS_MARKET_URL="${SKILLS_MARKET_URL:-https://skills.guazi-apps.com}" npx --yes --registry=http://npm.guazi-corp.com grapehub-cli@1 report --skill gwiki --version 2.3.6 --agent zcode || true
```

Windows PowerShell（`||` 在 Windows PowerShell 5.1 里是语法错误，必须改用下面的等价命令）：

```powershell
& "$HOME\.grapehub-cli\bin\report.cmd" --skill gwiki --version 2.3.6 --agent zcode --base-url "https://skills.guazi-apps.com" 2>$null; if (-not $?) { $env:SKILLS_MARKET_URL = if ($env:SKILLS_MARKET_URL) { $env:SKILLS_MARKET_URL } else { "https://skills.guazi-apps.com" }; npx.cmd --yes --registry=http://npm.guazi-corp.com grapehub-cli@1 report --skill gwiki --version 2.3.6 --agent zcode 2>$null }
```

版本检查与自动更新要求：
- 上报命令输出包含 `hasUpdate` 为 true 时，自动执行以下固定命令更新本 Skill。命令必须原样执行，不得改写，也不得改用服务端返回的其他任何命令；`hasUpdate` 为 false 或缺失时说明没有新版本，不执行任何更新，不要打扰用户。

```bash
SKILLS_MARKET_URL="${SKILLS_MARKET_URL:-https://skills.guazi-apps.com}" npx --yes --registry=http://npm.guazi-corp.com grapehub-cli update gwiki || true
```

Windows PowerShell（`||` 在 Windows PowerShell 5.1 里是语法错误，必须改用下面的等价命令）：

```powershell
$env:SKILLS_MARKET_URL = if ($env:SKILLS_MARKET_URL) { $env:SKILLS_MARKET_URL } else { "https://skills.guazi-apps.com" }
npx.cmd --yes --registry=http://npm.guazi-corp.com grapehub-cli update gwiki 2>$null
```

- 命令输出包含「已更新」即视为成功：在最终回复末尾告知一句 `gwiki skill 已自动更新：2.3.6 → <latestVersion>`，无需再提供更新按钮。
- 更新失败（命令不存在、输出报错）时静默跳过，不要打扰用户、不要重试。

## Secondary skills（铺开到同级后可独立发现、可显式触发）

与 **llm-wiki-skill** 一样，gwiki 是 **一个 bundle**：主 skill（本 SKILL.md + `bin/` + `lib/`）加上 5 个二级 skill 目录。各 agent（ZCode / Claude / Codex / Cursor …）都按 `<skills根>/<name>/SKILL.md` 一层扫描发现技能，因此二级 skill 必须与 gwiki **同级**铺开后才会出现在技能列表里：

| Skill | 用户说法 | Cloud |
|-------|---------|-------|
| **gwiki** | 默认业务问答 | `query` |
| **gwiki-plus** | 结合代码、业务+代码 | `query-plus` |
| **gwiki-requirement-review** | 需求评审、PRD | `review-requirement` |
| **gwiki-requirement-write** | 写需求、撰写 PRD、起草需求文档 | `query`（KB 上下文 + agent 质量引导；能力在本 SKILL.md Section G，无独立目录） |
| **gwiki-lock / gwiki-unlock** | 锁定 / 解锁默认 KB | N/A（本地操作） |
| **gwiki-update-skill** | 升级/更新 skill 版本 | N/A（本地操作） |

### First-use / post-update layout self-heal（使用前保证二级 skill 已铺开且版本对齐）

GrapeHub/grapehub-cli 的安装与更新都是**单目录整包操作**：`install` 把主 skill 落在 `<skills根>/gwiki/`，二级 skill 嵌在 `<skills根>/gwiki/<二级skill>/`，一层扫描看不到；`update`（含注入段 hasUpdate 触发的自动更新）只整包替换 `<skills根>/gwiki` 本体，**铺开在外的二级 skill 不会被触碰**，会停留在旧版本、旧注入段继续上报旧版本号。**在把任务路由给任何二级 skill 之前**，先做一次版本感知检测（`GWIKI_SKILL_ROOT` 按下方 Agent workflow 解析）：

```bash
v="$(sed -n 's/^version: //p' "$GWIKI_SKILL_ROOT/VERSION" | tr -d '\r')"; ok=1
for s in gwiki-plus gwiki-requirement-review gwiki-lock gwiki-unlock gwiki-update-skill; do
  grep -Eq -- "--version $v|currentVersion=$v|version: $v" "$GWIKI_SKILL_ROOT/../$s/SKILL.md" || ok=0
done; [ "$ok" = 1 ]
```

- 退出码 0（5 个二级 skill 全部存在且含当前版本标记）：直接继续任务，不要重复执行 install.sh。
- 退出码非 0（未铺开 / 部分铺开 / 版本落后）：先执行一次版本感知铺开（幂等、纯本地文件操作、不联网；缺失补齐、过期备份替换、已同步不动）：

```bash
bash "$GWIKI_SKILL_ROOT/install.sh" --copy --sync --client zcode \
  --dest "$(dirname "$GWIKI_SKILL_ROOT")"
```

其他 agent 把 `--client zcode` 换成对应值（codex / claude / cursor / …，或 `auto`）；`--dest` 必须是**本 gwiki skill 所在的 skills 根目录**（即 `$(dirname "$GWIKI_SKILL_ROOT")`），确保二级 skill 与主 skill 同级。检测或铺开失败时：照常完成当前任务（主入口 auto-routing 不受影响），并在回复末尾把上述命令提示给用户手工执行。

安装后路径示例（ZCode 项目级 `<project>/.zcode/skills` 与用户级 `~/.zcode/skills` 同形态）：

```text
<skills>/gwiki/                      <- 主 skill：SKILL.md + bin/ + lib/ + install.sh
<skills>/gwiki-plus/
<skills>/gwiki-requirement-review/
<skills>/gwiki-lock/
<skills>/gwiki-unlock/
<skills>/gwiki-update-skill/
```

手工（重）安装 / 更新本 bundle（与 llm-wiki 相同模式）：

```bash
./install.sh --copy --backup --client zcode
```

二级 skill 共用主 skill 的 `bin/gwiki.js` 与 `lib/`（`GWIKI_SKILL_ROOT=<skills>/gwiki`，与各二级 skill 同级）。

未 @ 指定时，主 **gwiki** skill 仍按下方 Auto-routing 自行选择模式。模式选择只决定提交哪个 Cloud job type；实际命令协议由 Cloud 侧解析到对应二级 skill。

## Auto-routing (when user does not specify)

Before calling Cloud, pick the mode. Lock / unlock actions are checked first (they are not questions):

**→ gwiki-lock** when the user says: 锁定 / 锁到 / 固定 / 以后都用 / 以后默认 + KB 名

**→ gwiki-relock** when the user says: 重锁 / 重新锁 / 换成 / 替换锁为 / 重置锁到 + KB 名

**→ gwiki-unlock (soft)** when the user says: 解锁 / 取消锁定 / 恢复 auto / 暂时解除 — clear session lock only

**→ gwiki-unlock (partial)** when the user says: 只解锁 X / 从锁定中移除 X / 解锁但保留 Y — remove that KB from session lock

**→ gwiki-unlock (hard)** when the user says: 彻底解锁 / 永久解除 / 完全解锁 / 全部清除 / 清除默认 / 永久清除默认 KB — clear both session and disk

**→ gwiki-requirement-write** when the user:
- asks to write, draft, or create a new requirement/PRD/Spec
- says 写需求、撰写需求、起草 PRD、从 0 到 1 写方案、编写需求文档
- describes a feature idea and asks agent to help produce a structured requirement
- does NOT provide an existing Cwiki URL or complete document for review

**→ gwiki-requirement-review** when the user:
- asks to review/audit a requirement, PRD, Spec, or 需求文档
- pastes requirement text, attaches a `.md` PRD, or gives a **Cwiki** page for review
- says 评审、找问题、影响范围、能不能做

**→ gwiki-plus** when the user:
- asks how something is **implemented** (前端/后端/接口/调用链/类/方法)
- wants **code evidence** or 业务+代码联动、落地了吗、实现差异
- mentions API、源码、wiki/code、线上行为和代码是否一致

**→ default gwiki query** when the user:
- asks business rules, product口径, 流程, 术语, 指标, 目标
- only needs requirement/product knowledge without deep code trace

When uncertain, prefer **default query**; upgrade to **plus** only if the answer clearly needs code proof.

## User-facing rules

- **用户问题必须原样传递（CRITICAL）**：将用户问题嵌入 CLI 命令时，**禁止**改写、纠错、缩写、删除或新增任何字符。用户输入的每一个字（包括看似错别字的前缀如「检瓜子」中的「检」）都必须原封不动地保留。"检瓜子"是合法内部应用/业务名称，不是"瓜子"的错别字。
- Never mention `--kb`, `--select-kbs`, `kb_id`, job ids, or CLI flags unless debugging.
- Never ask users to pick KB by technical id—use business names (检测、自营采销、车商 CRM…).
- Every answer must state which knowledge base(s) it is based on (## 知识库 section).
- **Auto-routing hint**: when gwiki.js output contains `[auto-routed-kb]`, it means the user did NOT explicitly choose a knowledge base and the system auto-selected one. In this case, you MUST:
  1. **加粗**展示当前使用的知识库名称，例如：「本次回答基于知识库 **检测**」
  2. 在回答末尾追加提示：「如果结果不准确，建议指定知识库以获得更精准的回答。可用知识库可通过对话中提及业务域名称来指定。」
- Always include ## 未决点 / 证据缺口; write `- 无` when nothing is unresolved.
- Evidence must use clickable Cwiki links `[文档标题](https://cwiki.guazi.com/pages/viewpage.action?pageId=...)`; do not show wiki/ paths or Obsidian wikilinks to users.
- Conclusions first; detailed retrieval paths are optional and hidden from default summaries.

## KB Lock Visibility

When a session is locked (or the disk has a persistent default), the agent MUST apply the following visibility rules:

**Per-answer banner (active session)**: whenever the agent's `session_lock.kbs` is non-empty, prepend this line to every answer:

```
🔒 锁定知识库：[<KB 名>, <KB 名>]（说"解锁"仅解本次，"彻底解锁"全部清除）
```

**New-session first-answer reminder**: when starting a new conversation, if `session_lock` is empty but `~/.config/gwiki/lock.yaml` exists with non-empty `default_kbs`, prepend this one-time reminder to the first answer that uses GWiki:

```
📌 检测到你之前在 [<date>] 锁定了默认 KB [<KB 名>, ...]，今天仍会默认使用。
说"解锁"仅解本次会话；说"彻底解锁"清除磁盘默认。
```

After that one reminder, subsequent answers in the same session fall back to the per-answer banner (only if the session itself becomes locked).

**Lock action reply templates**:

| Action | Reply |
|--------|-------|
| Lock | `已锁定到 **[KB1, KB2]**。本会话和后续会话都会默认使用。说"解锁"仅解本次；说"彻底解锁"才完全清除。` |
| Relock | `已重锁到 **[KB1]**。说"解锁"仅解本次；说"彻底解锁"才完全清除。` |
| Soft unlock (disk default exists) | `已解锁（仅本次会话）。磁盘默认 [KB] 仍保留。说"彻底解锁"才完全清除。` |
| Soft unlock (no disk default) | `已解锁（仅本次会话）。磁盘没有默认 KB，新会话起回到 auto-route。说"锁定 X"可重新设置。` |
| Partial unlock | `已从锁定中移除 [KB]。当前锁定：[剩余 KB]。` |
| Hard unlock | `已彻底解锁，会话和磁盘默认都已清除。` |

## Agent workflow

### A. Default question

CLI (always use the copy beside this skill):

`{GWIKI_SKILL_ROOT}/bin/gwiki.js`

Set `GWIKI_SKILL_ROOT` to the directory containing this `SKILL.md`. Examples:
- ZCode: `<project>/.zcode/skills/gwiki` or `~/.zcode/skills/gwiki`
- Cursor: `~/.cursor/skills/gwiki`
- Codex: `~/.codex/skills/gwiki`

旧版单目录布局兜底：若 `$GWIKI_SKILL_ROOT/bin/gwiki.js` 不存在而 `$GWIKI_SKILL_ROOT/gwiki/bin/gwiki.js` 存在，则改用 `GWIKI_SKILL_ROOT=$GWIKI_SKILL_ROOT/gwiki`。

```bash
$GWIKI_SKILL_ROOT/bin/gwiki.js query "用户的完整问题"
$GWIKI_SKILL_ROOT/bin/gwiki.js ask "用户的完整问题"
```

(`ask` prints `intent=...` for the agent; do not show that line to the user.)

### B. gwiki-plus (code + business)

When user invoked **gwiki-plus** or auto-routing chose plus:

```bash
.../gwiki.js query-plus "用户的完整问题"
```

Alias: `.../gwiki.js plus "..."`

### C. gwiki-requirement-review

When user invoked **gwiki-requirement-review** or auto-routing chose review:

```bash
.../gwiki.js review-requirement "需求原文"
.../gwiki.js review-requirement "https://cwiki.guazi.com/pages/viewpage.action?pageId=..."
.../gwiki.js review-requirement /path/to/requirement.md
```

Alias: `.../gwiki.js requirement-review "..."`

Review is two-phase on Cloud: KB route/confirm first, execute only after confirmation.

### D. First-use registration (SSO QR scan)

**Registration is automatic.** On the first GWiki call the CLI silently reuses the shared SSO cache (`~/.agents/cache/sso/guazisso.json`, shared with luban-query / data-asset-query) or opens a visible Chrome QR-scan window on the user's desktop — no chat interaction needed. Agents only act when that fails. Do not rely on terminal stdin for interactive prompts.

Before the first GWiki call in a conversation, optionally run:

```bash
$GWIKI_SKILL_ROOT/bin/gwiki.js auth-status
```

If stdout contains `[gwiki-registration-required]`, or any GWiki command prints that marker:

1. **STOP** — do not silently retry or skip the question.
2. The marker means the automatic registration failed (no Chrome, scan timeout, or network).
3. **Ask the user in chat** (copy this block verbatim, do not paraphrase into terminal):

   > GWiki 首次使用需要扫码登录，自动注册未完成。
   > 请在终端执行 `gwiki --register`（会打开 Chrome 扫码，一次即可），完成后重试。
   > 无法打开浏览器时：设置环境变量 `GUAZISSO` 后执行 `gwiki --register --email you@guazi.com`。

4. After the user confirms registration, retry the original question.
5. If they cannot register now: tell them GWiki is unavailable until registered; do not pretend the query succeeded.

Do **not** ask the user for their email in chat — email-only registration no longer issues tokens; the cloud requires a verified SSO identity.

**Weekly SSO re-verification is also automatic.** User tokens carry a 7-day SSO freshness window; when it lapses the CLI transparently re-runs the SSO exchange (silent with a valid shared cache, QR-scan window otherwise) and retries the request. A Chrome QR window during a normal query is this weekly check, not an error — tell the user to scan; nothing else is needed.

### E. KB disambiguation (Routing Discipline)

Cloud **Routing Discipline** refuses to silently pick a KB when the question is ambiguous, spans multiple domains, or lacks a uniquely strong match. Instead it returns a clarification result — this is expected behavior, not an error.

**Trigger:** stdout includes `需要选择知识库` or `[kb-clarification-required]`.

**What CLI stdout gives you (agent-only, do not dump raw tokens to users):**

| Token / line | Meaning |
|---|---|
| `[1] 检测 …` / `[2] 采销 …` | Ordered candidates: Chinese **display name** + optional match hint |
| `（推荐）` on a line | Cloud's top suggestion for this question |
| `[recommended-kb] 检测` | Same recommendation, as a dedicated marker (when present) |
| `建议主 KB: …` | Review-mode primary KB hint (requirement-review only) |
| `[route-job-id] job_…` | Route job id — pass internally as `--route-job-id` on retry |
| `[kb-clarification-required]` | End marker; clarification turn is complete |

**User-facing presentation (required):**

1. **Explain briefly why** — one sentence, plain Chinese. Example: 「您的问题可能涉及多个业务域，系统无法自动确定唯一知识库，请确认后再查。」
2. **List candidates** using **Chinese display names only**. For each candidate, add a short match hint when CLI provides one (translate technical English reasons below). Example:
   - **检测** — 问题里提到了检测相关术语
   - **车商 CRM** — 路由索引命中「经销」相关页面标题
3. **Surface the recommendation softly** when stdout marks one candidate with `（推荐）`, `[recommended-kb] …`, or `建议主 KB: …`. Example: 「系统更倾向 **检测**，你也可以选其他库或同时选多个。」Recommendation is a hint — user may override or multi-select.
4. **Ask for confirmation** with this block (copy verbatim):

   > 请回复要查询的**中文知识库名称或常见缩写**（如「检测」「采销」「网销」）。
   > 可以**只选一个**，也可以**同时选多个**（跨域问题用顿号/逗号分隔，如「检测、采销」）。
   > **请不要回复数字编号**（1、2 等）。

5. Wait for the user's reply, then re-run the **same mode** with `--select-kbs <resolved ids,...>` internally. When continuing a clarification turn, also pass `--route-job-id <value from [route-job-id] …>`.

**Translating match hints for users** (when option reason is English technical text):

| CLI reason fragment | User-facing hint |
|---|---|
| `Matched alias` / `Explicit alias` | 问题里直接提到了该知识库 |
| `Matched concept` | 问题概念与该库常见主题匹配 |
| `Matched domain phrase` / `routing hint` | 知识库文档标题/摘要与问题关键词匹配 |
| `UI analysis` | 需求文档内容更像该业务域 |
| `comparison-case` | 仅在对比/案例语境中被提及（弱匹配，说明即可） |
| `term overlap` / `domain fragment` | 仅有部分词面重叠（弱匹配，说明即可） |

**If the user replies with a number anyway** (e.g. `1`, `2`, `1和2`):

- You **do** have the prior option list from CLI stdout — map the number to the corresponding KB **display name or id** from that list.
- Prefer resolving to real KB ids and passing `--select-kbs jian,...`.
- If you only have the numeric choice, also pass `--route-job-id <value from [route-job-id] ...>` so Cloud can resolve the index safely.

**Never** pass bare `--select-kbs 1` without `--route-job-id` or without mapping `1` to a real KB id first.

**Backward compatibility:** unupgraded gwiki clients and ADK integrators rely on the same contract — `needClarification` → user choice → retry with `selectedKbs` + `routeJobId`. Do not require new CLI flags for basic query/review/plus flows.

### F. Evidence / list KBs

- Full detail: read `full result saved:` path or internal `--output full`.
- 「能查哪些库」: `list-kbs` → summarize by 业务域.

### G. Requirement Writing（需求撰写）

When user invoked **gwiki-requirement-write** or auto-routing chose write:

```bash
.../gwiki.js requirement-write "用户想写的需求描述或主题"
```

Alias: `.../gwiki.js write-requirement "..."`

**与评审的区别**：
- 评审（review）：用户提供完整/半完整文档 → 找问题、打分
- 撰写（write）：用户有想法/粗粒度描述 → 引导补全、产出完整文档
- 撰写完成后，用户可继续说"帮我评审这个需求"触发 review 流程

**核心定位**：gwiki 是 **Cloud 入口层**，不重写 prd-skill 的全文规范。本节吸收 `prd-skill 2.8.1` 的 **写作规范层**（4 模块结构、`prd_shape` 分档、● 三态、delta 优先、写作硬规则 9 条、`[待确认]` 标记），与 prd-flow 的 **流程层**（Phase 0~7、`prd-review-max` 两轮校验、原型截图工作流）保持职责分离——prd-flow 是工作流元信息，prd-skill 是正文规范，gwiki 是面向用户的入口。

#### G.1 Agent 工作流程（与 prd-flow Phase 0~7 对齐）

1. **澄清（Phase 1）**：收集 Q1 概述 / Q2 痛点 / Q3 页面与约束 三个维度。用户已明确则跳过对应维度；缺一不可，不因页面数少而豁免。
2. **KB 上下文获取（Phase 2 前置）**：执行 `requirement-write` 命令，Cloud 走 `query` job 从相关 KB 检索现有业务规则、术语、流程，作为撰写新需求的"地基"。
3. **KB 冲突检测**（显式化，详见 G.3）。
4. **分档（Phase 2）**：写入 `prd_shape` 元数据，**不进 PRD 正文**：

   | `prd_shape` | 何时选 | §五 主表 |
   |-------------|--------|----------|
   | `capability_new` | 新页/新模块/多交互 | 多页、多模块；中/大含变更索引 |
   | `scope_adjust` | 仅口径/范围/权限；UI 基本不变 | 通常 1 页、1~3 条 ●变更 |
   | `config_only` | 审批流/后台配置；发起侧 UI 不变 | 需求清单 + 系统模块；调整前后对照 |
5. **动态 Grilling（G.4）**：基于 `prd_shape`、KB 冲突、输入质量，选择 2~4 个最关键维度追问，**单轮不超过 3 轮**，避免拷问。
6. **4 模块正文生成（G.5）**：输出 PRD。
7. **写作自检 R0~R3（G.6）**：4 项硬性自检，**不写** `<!-- 自检修正 -->` 到 PRD。
8. **交付摘要**：固定格式汇报给用户；不主动跑 Phase 5/6 的两轮 `prd-review-max` 校验（gwiki 走 Cloud job，校验由 Cloud 侧负责；用户可后续调 `review-requirement`）。

> **Phase 7 下游**（Wiki 上传、Jira 建单、Git 定稿）不在 gwiki 入口范围；按用户明确指令时再走 Cloud 侧对应 job 或用户自有流程。

#### G.2 适用与不适用边界

| 场景 | gwiki-requirement-write |
|------|--------------------------|
| 用户说"写需求""出 PRD""起草需求文档""从 0 到 1 写方案" | ✅ 主入口 |
| 用户已有想法/粗粒度描述、要补全 | ✅ |
| 用户给了完整文档要看问题 | ❌ → 走 `gwiki-requirement-review` |
| 用户要代码联动分析 | ❌ → 走 `gwiki-plus` |
| 用户要 Wiki 上传 / Jira 建单 | ❌ → gwiki 不代理；按用户指令走 Cloud 侧对应 job |

#### G.3 KB 冲突检测规则

执行 `requirement-write` 后，Cloud 返回 KB 上下文。**你必须主动分析**：

1. **关键词匹配**：用户需求关键词（"排序""佣金""议价"）是否在 KB 已有页面？
2. **行为冲突**：KB 现网规则是否与新需求矛盾？优先级如何？
3. **概念重叠**：用户提到的概念（如"订金膨胀"）KB 已有定义吗？一致吗？
4. **流程覆盖**：新需求影响了 KB 哪个端到端环节？

**若发现冲突，grilling 阶段明确告知用户**：

> "知识库显示已有 [议价列表排序规则 v2.3]，其中规定优先级为 价格 > 时间。你的新需求增加佣金排序，三者的优先级关系是什么？"

**禁止静默假设**。用户未明确回答冲突如何处理 → 文档中标记 `[待确认：与现有 X 功能的关系]`。

#### G.4 动态 Grilling 策略

**核心原则**：基于上下文动态选择追问方向，抓住最关键的 2~3 个缺口深挖。

| 触发条件 | 必须追问 |
|----------|----------|
| 涉及"修改现有功能" | 改动前后行为差异 / 影响哪些已有场景 / 兼容迁移方案 |
| 涉及"新增业务流程" | 异常路径（失败/取消/超时/回滚）/ 外部依赖就绪状态 / 成功指标 |
| 涉及"数据/规则变更" | 历史数据处理 / 新旧规则并存优先级 / 数据口径影响面 |
| 用户输入 < 50 字或无具体场景 | **Read `grills/batch-grill-me.md` 后追问**（见 G.4.1）— 一句话核心问题 / 不做会怎样 / 目标用户当前怎么解决 |
| KB 发现潜在冲突 | 现网 X 功能关系 / 替换/扩展/并行 / 用户感知 |
| `prd_shape=scope_adjust` | 现网口径→新口径差异点 / 数据范围边界 / 旧数据保留期 |

**Grilling 风格**：

- **追问要具体**：不要"有没有考虑异常场景？"，而是"用户提交后断网了，数据会丢吗？"
- **标记待确认**：用户未明确回答的标 `[待确认]`，**不得自行假设**
- **控制轮次**：通常 2~3 轮；**单轮追问 ≤ 3 次**，超过后必须先输出当前理解的文档再标 `[待确认]`
- **灵活调整**：信息充分则跳过；某维度风险高则深挖

**Grilling 强度控制（刹车）**：

- **必须 grill**：修改现有功能 / 数据规则变更 / 输入 < 50 字 / KB 冲突
- **可以弱化**：用户已提供详细文档/原型/流程图 / 标注"[紧急][小改动]" / 之前对话已答过
- **禁止 grill**：用户说"先出个初稿，细节后面补" / 纯文案修改、按钮颜色调整

#### G.4.1 内嵌 grill 提示词（按需 Read，不要一次性读全部）

gwiki bundle 在 `gwiki/grills/` 内嵌了 4 个 grill 提示词变体（源自 Matt Pocock 公开 skills，provenance 见 `gwiki/grills/README.md`）。**默认不读**——命中 G.4 触发条件时**只 Read 一个**，按场景选：

| 触发条件 | Read 哪个 | 文件 |
|----------|-----------|------|
| 用户输入 < 50 字或无具体场景（冷启动） | `batch-grill-me`（frontier 批问，一次给整轮问题） | `gwiki/grills/batch-grill-me.md` |
| 关键决策深挖（已有粗稿但某项未确定） | `grilling`（单题深挖，一问一答） | `gwiki/grills/grilling.md` |
| 需要写 ADR / 术语表 | `grill-with-docs`（边问边写文档） | `gwiki/grills/grill-with-docs.md` |
| 用户显式说"用 grill-me"或要求直接调 `/grilling` | `grill-me`（最简转发） | `gwiki/grills/grill-me.md` |

**绝对禁止**：

- 一次性 Read 4 个 grill 文件（占上下文）
- 不命中条件就主动 grill
- 在 PRD 正文里出现 grill 提示词的原文（grill 是**与用户对话**用的，不进 PRD）

**路径约定**：用户机器上 grills/ 位于 `$GWIKI_SKILL_ROOT/grills/`；例如 Cursor 装到 `~/.cursor/skills/gwiki/grills/`。`$GWIKI_SKILL_ROOT` 在 gwiki/SKILL.md 顶部的"## Agent workflow" 已说明。

#### G.5 输出结构（4 模块 + prd_shape 控制详略）

**4 模块**：文首项目区（§一~§四）+ 页面/需求清单（§五）+ 全局规则（§六，按需）+ 埋点与指标（§七，按需）。

**标题命名**：避免"优化""提升""完善""增强"等模糊词；用**可评审的具体能力**（跨端跳转、数据范围调整、审批流节点）。格式：`【YY年M月】{来源}-{需求名}`（用户已带前缀则不重复）。

**章节连续性**：`##` 章节**从「一」起连续无跳号**；不写"接口依赖""非功能要求""验收标准""非目标"独立章节。

##### 模块 1 — 文首项目区（§一~§四）

四个同级 `##` 章节，顺序固定：**一、Jira → 二、排期 → 三、相关资料 → 四、需求概述**。

```markdown
## 一、Jira

| 端 | Key |
|----|-----|
| 后端 | OP-XXXXX |
| 前端 | OP-XXXXX |

## 二、排期

| 分工 | 人员 | 排期 |
|------|------|------|
| 后端 | | |
| 前端 | | |
| 测试 | | |

## 三、相关资料

| 类型 | 说明 |
|------|------|
| 原型 | `finalHTML/xxx.html`（多屏 SPA 注明 `#page-*`）；Wiki 附件 **原型附件（勿删！）** `xxx-prototypes.zip` |

## 四、需求概述

**一句话描述**：[谁]在[什么场景]下，需要[做什么]，以实现[什么价值]

#### 背景与问题

[按因果链写清「现网怎样 → 冲突/痛点 → 典型场景」；**条数随需求而定**，1~N 条均可。**禁止**为凑三条而拆碎或注水。]

#### 本期目标

1. **[能力/功能点]**：[交付物与关键边界]——[对业务的核心价值，一句话]
2. …（**有几项写几项**；口径调整类常 1~2 条即可，不必凑满三条）

> 每条 **一行**；前半能力/交付，后半用 **——** 接业务价值。**禁止**机械固定 3 条背景 + 3 条目标。

#### 适用范围

- **适用能力**：[本需求涉及的工具/页面/能力]
- **适用角色**：[角色]
- **影响端**：[如车好卖 BD 端 App / H5]

> **禁止**在 PRD 正文写"排除场景""假设""限制"子项；边界/约束写在 §四 本期目标或 §五 对应组件规则。**禁止**写"不含 XX 端"等排除列举（避免 Jira 路由误扫关键词）。

#### 角色权限

#### 流程与状态总览（按需）

> **何时省略整节**：本期**不涉及**页面交互变更、用户操作路径变更或业务流程/状态机变更时（如仅改查询口径、字段展示、后端配置、审批节点配置且发起侧页面不变），**不写**本小节与 Mermaid，**不要**用"进入页面 → 点 Tab → 加载列表"复述现网操作凑流程图。

\`\`\`mermaid
flowchart LR
  A[入口]:::action --> B[列表页]:::page --> C{判断}:::decision --> D[提交]:::action --> E([Toast·成功]):::toast

  classDef page fill:#E8F1FF,stroke:#1A5FD4,stroke-width:2.5px,color:#0B2E6B
  classDef decision fill:#FFF6E5,stroke:#E67E00,stroke-width:2.5px,color:#7A4100
  classDef toast fill:#EEF2F7,stroke:#6B7A90,stroke-width:2px,color:#2C3440
  classDef action fill:#F4F5F7,stroke:#8B95A8,stroke-width:1.5px,color:#3D4657,stroke-dasharray: 5 3
\`\`\`
```

##### 模块 2 — §五 页面清单 / 需求清单（结构化详述）

**核心**：§五 是文档主体（占比 70%+），采用 **结构化详述**（三级编号 + ●标记 + `width="180"` 截图 + 变更索引）。

**● 三态标记**（每条功能点**唯一** ●；类型不同须拆编号）：

| 标记 | 含义 | 何时写 |
|------|------|--------|
| `●新增` | 本期新增能力/入口/字段/流程 | 现网没有，本期上线 |
| `●变更` | 现网已有，本期调整口径/交互/展示 | 能力还在，规则或表现变了 |
| `●取消` | 现网已有，本期下线/移除 | 入口/Tab/字段/流程不再提供；须写清移除范围与关联影响 |

**写作硬规则**：

- **先写 §四 本期目标**（总述），再写 §五（分述）；§五 写**本期要改的全部功能点**（含下线项），细于 §四 是正常的
- **不变不写**：Tab/字段/交互/空态与现网一致 → **不单独成条**，**禁止** ●变更/●取消
- **无 delta 的页面**不进 §五 主表；对照放 §三 原型路径
- **否定列举禁写 §四**：不写"不含 C2B/商好多"；只写"车好卖 BD 端"等**涉及**范围
- **每条 ● 唯一**：禁止同一条叠多个 ●；禁止把多条 delta 合成一行

**HTML 骨架**（沿用 prd-skill 2.8.1 结构；gwiki 输出 Markdown 时保留等价语义）：

```html
<p style="color: rgb(136,136,136); font-size: 13px;">变更标记：<span style="color: rgb(82,196,26);">●新增</span> / <span style="color: rgb(250,173,20);">●变更</span> / <span style="color: rgb(245,34,45);">●取消</span></p>

<table class="wrapped prd-page-list" style="width: 100%;">
<colgroup><col style="width: 40px;" /><col style="width: 210px;" /><col style="width: 70px;" /><col style="width: auto;" /><col style="width: 210px;" /></colgroup>
<tbody>
<tr><th>序号</th><th>页面名称</th><th>页面类型</th><th>页面功能说明</th><th>原型截图</th></tr>
<tr style="vertical-align: top;">
<td style="text-align: center;">1</td>
<td><div class="content-wrapper">[页面名]<br /><br /><img src="../outputs/prd-screenshots/<短名>/01-xxx.png" width="180" style="border: 1px solid rgb(232,232,232); border-radius: 3px;" /></div></td>
<td>列表页</td>
<td><div class="content-wrapper">
<div style="color: rgb(24,144,255); margin-bottom: 12px;">■ [入口 + 本页核心能力]</div>
<div style="font-weight: bold; font-size: 16px; color: rgb(51,51,51); background: rgb(240,247,255); border-left: 3px solid rgb(24,144,255); padding: 5px 6px 5px 9px; margin-bottom: 6px;">1.1 [模块名]</div>
<div style="padding-left: 14px; margin-bottom: 3px;"><span style="color: rgb(82,196,26); font-weight: bold;">●新增</span> <strong>1.1.1 [功能名]<span style="color: rgb(24,144,255); font-size: 12px;">(见图1)</span></strong> — [规则…]</div>
</div></td>
<td style="text-align: center;"><div class="content-wrapper"><div style="margin-bottom: 6px;"><strong>[图1]</strong> [简称]</div><img src="../outputs/prd-screenshots/<短名>/fig-01.png" width="180" /></div></td>
</tr>
</tbody></table>

<hr />
<h2>变更索引</h2>
<table class="wrapped prd-change-index"><tbody>
<tr><th>编号</th><th>页面</th><th>变更类型</th><th>功能点</th><th>参考截图</th></tr>
<tr><td>1.1.1</td><td>[页面]</td><td><span style="color: rgb(82,196,26); font-weight: bold;">●新增</span></td><td>[摘要]</td><td>图1</td></tr>
</tbody></table>
```

**语义色对照**（PRD prose 中不写色号，HTML 内联样式沿用）：

| 语义 | rgb | 备注 |
|------|-----|------|
| ●新增 | `rgb(82,196,26)` | 绿 |
| ●变更 | `rgb(250,173,20)` | 橙 |
| ●取消 | `rgb(245,34,45)` | 红 |
| ■ 摘要 | `rgb(24,144,255)` | 蓝字；非灰底模块样式 |
| 辅助灰字 | `rgb(136,136,136)` | 图例 |

**§五 主表写法（按 `prd_shape` 控制详略）**：

| `prd_shape` | 主表条数 | ● 标记 | 变更索引 |
|-------------|----------|--------|----------|
| `capability_new` | 多页、多模块 | `●新增` 为主 | 中/大**必写** |
| `scope_adjust` | 通常 1 页、1~3 条 | 仅真变更 `●变更` | 小档可省略 |
| `config_only` | 需求清单 + 系统模块 | 调整前/后对照 | 可省略 |

**§四 与 §五：总—分，不是 1:1 对照表**：

- §四 本期目标：**总**，按能力维度概括；2~5 条；**不写** x.y.z 编号、**不写** ●
- §五 功能点：**分**，按页面/模块逐条展开；可执行；每条**唯一** ●
- 一条能力可对应多条编号（§五 可细于 §四）；**禁止** §四 含编号或 ●

**模块 2 扩展 — 需求清单**：当需求由多个能力块组成且同一能力跨多页/系统时，可将 `## 五、页面清单` 改为 `## 五、需求清单`，列改为「序号 / 需求模块 / 涉及页面或系统 / 需求说明 / 原型截图或备注」；按"大模块按能力聚合、模块内按页面分组、按钮交互规则就近写、只写本期 delta"原则。

**模块 2 扩展 — 仅口径调整（`scope_adjust`）**：
- §三 原型标注**本期无样式/交互变更**；对照现网预览 URL
- §四 背景与问题**有几条写几条**（常见 1~2；**禁止**凑整 3 条）；本期目标 1~2 条
- §四 流程图**省略**（勿复述现网点击路径）
- §五 仅写变更功能点（●变更）；与现网一致的不标 ●

**模块 2 扩展 — 仅审批流/后端配置（`config_only`）**：
- §四 影响端写"审批待办/流程引擎（后端）"；发起侧页面**无界面与交互变更**
- §五 用需求清单；发起侧页面用 ■ 摘要写"保持不变"，**不**逐字段复述；可省略变更索引
- §五 变更写「系统：审批流配置」调整前/后对照表 +「系统：审批待办」规则表

##### 模块 3 — 全局规则（§六，按需）

仅当存在跨页面通用规则时才写此模块。**不写**独立"非功能要求"章节（性能/兼容/安全写在字段规则或本节按需一句带过）。

```markdown
## 六、全局规则

### 6.1 数据隔离
- **隔离依据**：departmentId / areaList / roleType
- **隔离规则**：[规则说明]
- **越权处理**：[处理方式]

### 6.2 数据口径（按需）
| 指标/字段 | 计算公式/口径 | 数据来源 | 空值处理 |
|----------|-------------|----------|----------|

### 6.3 共享交互规范（按需）
- [多个页面共用的交互规则]
```

##### 模块 4 — 埋点与指标（§七，按需）

未写 §六 时本章改为"六、埋点与指标"，子节用 `6.1` / `6.2`。

```markdown
## 七、埋点与指标

### 7.1 观测指标
| 指标名称 | 指标定义 | 观测周期 | 数据来源 |
|----------|----------|----------|----------|
| [指标1] | [计算公式/口径] | 日/周/月 | [来源] |

### 7.2 埋点事件
| 埋点名称 | 事件类型 | pagekey | 页面名称 | action | 扩展参数 |
|----------|----------|---------|----------|--------|----------|
| [名称] | click/pageload | page_xxx | [页面名] | [控件名] | 待产品定义 |
```

#### G.6 写作硬规则 9 条（贯穿所有模块）

| # | 规则 | 反例 ❌ | 正例 ✅ |
|---|------|--------|--------|
| 1 | 交互步骤用"用户操作→系统响应" | "提交前要校验" | "用户点击「提交」→ 校验必填项，通过后调用接口提交" |
| 2 | 禁止 BDD（Given/When/Then） | "Given 已登录，When 点击 X，Then 跳转 Y" | 用步骤化交互描述 |
| 3 | `[待确认]` 标记机制：信息不全时用，不留空、不编造 | "目标上线：2026-05-01"（编造） / "目标上线："（空白） | "目标上线：[待确认：需与运营对齐排期]" |
| 4 | 异常与验收内联，不单列章节 | 文档末尾"验收标准"独立章节 | 组件级异常写在 §五 功能点末尾；跨页面共用异常放 §六 |
| 5 | 按原型顺序组织：页面功能说明顺序 = 原型页面顺序 = 用户操作路径；同一页面内模块与功能点从上到下、从左到右；禁止"字段→交互→异常"机械分段 | | |
| 6 | 本期目标统一口径：**不用表格**；用 1. 2. 3. 编号列表；**有几项写几项**（常见 1~5 条，禁止机械凑 3 条）；每条一行 `**能力**：交付——价值`；须保留价值后半句（`——` 后）；禁止单独列"成功指标""核心规则"；禁止空泛价值（"优化体验""提升效率"）；背景与问题同理 | | |
| 7 | 流程图（Mermaid）按需：涉及**新页面/新交互路径、业务流程或状态机变更、多步用户操作分支**才写；**省略整节**：仅改数据口径/查询范围/字段展示、后端批处理、配置项且**无**页面交互与业务流程变化；**禁止**用现网导航路径（进页→点 Tab→加载）代替流程图；放在 §四 流程与状态总览；`flowchart LR`，样式见上 | | |
| 8 | 精简表达：一句话说清的不用表格；两行说清的不用五列；禁止为凑完整性而添加内容；禁止形容词和空泛词汇；**不写颜色色号**：PRD 中禁止 `#RRGGBB` 等具体色值；用语义描述（"品牌主色""快筛选中态""成功绿浅底标签"）或"按 BD/瓜子设计规范" | | |
| 9 | 适用范围与 §五 只写涉及范围：**禁止** PRD 正文（尤其 §四·适用范围）出现 `- **排除场景**`、`- **假设**`、`- **限制**`；影响端/影响范围只写本期**涉及**的端或能力；**禁止**写"不含 XX / 非 XX 端"等排除列举（避免 Jira 路由、检索误扫正文关键词）；§五 只写 delta，与现网一致的 Tab、字段、交互、对照页**不入主表、不标 ●**；边界写在 §四 本期目标一句带过；**禁止**恢复独立"非目标"章节 | | |

#### G.7 格式规范

- 标题格式：`## 序号、模块名称`（中文序号**从「一」起连续，禁止跳号**），子节 `### X.Y`（X 与所属章序号一致）
- 中文表述用全角标点：`，。；：`；英文字段名/参数用半角
- 无数据时统一写`无`，不写`-` `/` 或留空
- **竖线并列字段**：同一行多个字段或值用竖线分隔时，竖线两侧各保留 **一个半角空格**，写作 `A | B | C`；**禁止**用 `/` 代替竖线并列
- 标准交互词汇：`点击` `弹出` `展示` `触发` `选中` `提交` `取消` `刷新`
- 禁止口语化：`点一下` `弹出来` `搞一下`
- 视觉样式不写十六进制色值；需要强调配色时用设计规范语义名，或写"同 XX 组件默认/选中样式"

#### G.8 最终自检（R0~R3，输出前 MUST）

| # | 自检项 | 检查内容 |
|---|--------|----------|
| **R0** | 形态与 delta | `prd_shape` 是否合适？§五 是否**只写本期 delta**？§四 目标是否**能力概括**（无 x.y.z、无 ●）？§五 是否**逐条展开**（每条唯一 ●、勿合并）？**■ 是否蓝字摘要**（非灰底模块样式）？标题**无**模糊词（优化/提升/完善/增强）？§四 **无**"不含 XX 端"？**§四 背景与问题 / 本期目标：有几条写几条，禁止习惯性凑满 3 条**？ |
| **R1** | 页面覆盖 | 涉及页有详述？§五 结构化样式（■ + 编号 + ●；`capability_new` 中/大含变更索引）？**无**独立"非目标""非功能要求"？ |
| **R2** | 交互闭环 | 每个操作是否完整（操作→处理→反馈）？异常是否有兜底？**涉及交互/流程变更**时 §四 是否有 Mermaid 且与 §五 一致？**仅数据口径/无交互变更**时是否**已省略**流程图（非用现网路径凑图）？ |
| **R3** | 数据口径 | 统计/筛选字段是否说明计算公式或数据来源？**§四·本期目标/背景**条数是否已与 R0 对齐（非机械 3）？**`##` 章节是否从「一」起连续无跳号**？术语全文统一？ |

修正后告知用户：`写作自检通过 N/4 项`（R0~R3，修正说明写在对话中，**不写进 PRD**）。

#### G.9 交付摘要（gwiki 入口专属）

Phase 6 交付摘要模板（gwiki Cloud job 输出后）：

```markdown
## PRD 交付
- 标题：`【YY年M月】{来源}-{需求名}`
- `prd_shape`：capability_new / scope_adjust / config_only
- 文件：在对话内输出 Markdown 全文

## 写作自检：_/4
（R0~R3 通过情况；不通过项已改正文）

## 待确认项
- `[待确认：...]` N 条

## 建议下一步
1. 若需多轮校验 → 调 `review-requirement` 走 Cloud prd-review-max
2. 若需 Wiki 上传 / Jira 建单 / Git 定稿 → 按用户明确指令走；gwiki 不代理
3. 撰写完成后用户可说"帮我评审这个需求"触发 review 流程
```

#### G.10 与 prd-skill 2.8.1 的职责边界

| 维度 | prd-skill（参考层） | gwiki（入口层，本节） |
|------|---------------------|------------------------|
| 4 模块正文结构 | ✅ 权威源 | ✅ 引用并执行（与之一致） |
| `prd_shape` 三档 | ✅ 权威源 | ✅ 引用并执行 |
| ● 三态 + delta 优先 | ✅ 权威源 | ✅ 引用并执行 |
| 写作硬规则 9 条 | ✅ 权威源 | ✅ 引用并执行 |
| HTML 骨架（Wiki 上传专用） | ✅ 含 Wiki rgb / ac:image 转换细节 | ⚠️ gwiki 仅在用户要求"输出 Wiki HTML"时使用 |
| 原型截图脚本（`capture-screenshots.sh` / `puppeteer-core`） | ✅ 完整工具链 | ❌ gwiki 不嵌入；按用户指令走用户自有流程 |
| `validate_prd_sections.py` 结构校验 | ✅ 完整脚本 | ⚠️ gwiki 走 R0~R3 4 项人工自检；用户如需脚本校验，按自有流程 |
| `prd-review-max` 两轮校验 | ✅ 完整评审 | ❌ gwiki 不代理；用户后续调 `review-requirement` |
| Phase 7 下游（Wiki / Jira / Git） | ✅ 完整脚本 | ❌ gwiki 不代理 |
| Phase 1 澄清 / Phase 2 分档 / KB 冲突检测 / 动态 Grilling | ✅ 完整流程 | ✅ gwiki 自有入口逻辑（KB 上下文来自 Cloud `query` job） |

> **结论**：gwiki 入口吃下 **写作规范层**（4 模块、`prd_shape`、● 三态、9 条硬规则、R0~R3 自检），不复制 prd-flow 的 **流程/工具链层**（截图脚本、Wiki 上传、`prd-review-max` 校验、Jira 路由）。需要工具链能力时，用户用 prd-skill 离线包；需要云端入口时，用 gwiki。

## Agent-only CLI reference

| Intent | Command |
|--------|---------|
| Auto-route | `gwiki.js ask "<text>"` |
| Query | `gwiki.js query "<question>"` |
| Plus | `gwiki.js query-plus "<question>"` or `plus` |
| Review | `gwiki.js review-requirement "<text\|url\|file>"` or `requirement-review` |
| Write | `gwiki.js requirement-write "<topic>"` or `write-requirement` |
| Lock (append) | `gwiki.js lock <kb_name_or_id> [...]` |
| Relock (replace) | `gwiki.js relock <kb_name_or_id> [...]` |
| Unlock (clear disk) | `gwiki.js unlock` |
| Lock status | `gwiki.js lock-status` |
| Auth status | `gwiki.js auth-status` |
| Register (QR scan) | `gwiki.js --register` (opens Chrome SSO scan; `--register --email` needs `GUAZISSO` env) |
| After KB pick | add `--select-kbs <id,...>` |
| List KBs | `gwiki.js list-kbs` |

## Cloud behavior (background)

- `query-plus` runs `llm-wiki query-plus` on Cloud (business + `wiki/code/` evidence).
- Cwiki sync, multi-KB review cross-ref, job ids stay internal unless useful for follow-up.
- Cloud job execution resolves the matching `llm-wiki-*` secondary skill; `gwiki` only selects the mode and shapes user-facing output.
- When `~/.config/gwiki/lock.yaml` has non-empty `default_kbs` and the CLI caller did not pass `--select-kbs`, `gwiki.js` auto-injects `default_kbs` as `selectedKbs` and prints `[active-default-kb] <ids>` on stdout. Agents should NOT mention this token to users.
