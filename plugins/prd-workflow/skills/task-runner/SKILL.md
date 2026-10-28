---
name: task-runner
description: 按 prd-to-breakdown 产出的任务记录驱动开发——从状态库取一条依赖已就绪的任务，开工前核对文件现状/契约/设计锚点/基线约束，按记录的落点实现并逐条执行验收，最后回写完成记录与状态；视觉还原委托 ui-restore，提交委托 smart-commit。只要用户说「开始做 T003」「执行任务卡」「跑一下这个任务」「接着做下一条」「按任务开发」「把 OS-xxxx 的任务做掉」，就必须使用本 skill，不要直接开始写代码。
---

# task-runner：按任务记录驱动开发

数据源只有一处：`~/.prd-to-breakdown/prd.db`。所有读写通过 `ptb` 命令，不手写 SQL、不生成任务卡文件。

```bash
SKILL_DIR="$(cd "$(dirname "<本技能目录>/SKILL.md")" && pwd -P)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd -P)"
PTB="$PLUGIN_ROOT/scripts/ptb.py"
```

`<本技能目录>` = 本 SKILL.md 所在目录（软链会被 `pwd -P` 解析到插件真实路径）；`<项目根>` = 当前业务仓库根。

## 铁律

1. **一次一条任务**，除非用户明确要求「连续做 / 全部做完」。
2. **开工 Gate 全过才动手**；任何一项不通过就停下来处理，不带着问题开始。
3. **记录与仓库冲突时，改记录或问用户**，不在实现里就地偏离后不记录。
4. **不扩范围**：只改任务 `files` 里列出的路径；清单外的改动先说明理由并登记。
5. **验收逐条执行**，不许口头通过；`complete` 会被 CLI 拦住未通过的验收。
6. **UI 改动必须走 ui-restore**，禁止凭记忆或组件默认样式还原。
7. **不回写视为没做完**：完成记录、验收结果、状态三者缺一不可。
8. 只通过 `ptb` 读写状态库。
9. **开工即留痕**：Gate 全过后 `start`，实现与验证的关键节点 `progress`，中断也能接上。
10. **恢复先对账**：任务已是 `doing` 或库说未开始但仓库有改动时，先 `doctor`，再决定继续/补做/重做。

## 0. 定位任务

用户给定 `JIRA + 任务ID` 时直接读；只给任务 ID 时用 `python3 "$PTB" jira list --json` 确认归属，多个需求有同名任务就问用户。

用户说「接着做 / 下一条」时：

```bash
python3 "$PTB" ready --jira <JIRA> --json
```

按 `seq` 取第一条，先用一句话复述「任务标题 + 依赖 + 设计锚点 + 验收条数」再开工——不必等确认，Gate 发现冲突时才停。

读取上下文：

```bash
python3 "$PTB" jira show --jira <JIRA> --md          # 范围、基线约束、设计稿、进度
python3 "$PTB" task show <TASK> --jira <JIRA> --json # 任务全量字段
python3 "$PTB" validate --jira <JIRA>                # 当前库的一致性
python3 "$PTB" doctor --jira <JIRA> --root <项目根>  # 库内进度 vs 仓库实际
```

中断后回来、或不确定上次做到哪，**第一步永远是 `doctor` + `log`**，不要凭印象接着写。

任务里的 `implementation` 是**输入不是答案**，开工前必须重新读真实代码确认现状。

同一需求 brief 里若有 `## 结构设计` 小节（Epic 级：模块边界与依赖方向 / 状态归属 / 数据流向 / 抽象判定 / 分层核对），它比单卡 `implementation` 更上位：**状态归谁、抽象落在哪一层，以它为准**。执行中发现结构设计与代码现状冲突——例如状态其实已被某个 Provider 持有、或项目里已有同类实现可改造而卡上写「新建」——按铁律 3 回写记录（`task text --field implementation` / `decision add`），不要就地按卡片字面实现。

## 1. 开工 Gate（逐项过，任一不过就停）

| 检查 | 通过条件 | 不通过怎么办 |
|---|---|---|
| 状态 | `todo` 或 `doing` | `done` → 报告已完成，不重做；`blocked` → 先问阻塞是否解除；`doing` → 上次中断，先 `log` + `doctor` 恢复现场 |
| 依赖 | 所有 `depends_on` 均 `done` | 报告被谁卡住，不硬上 |
| 库校验 | `validate` 无错误 | 错误属于记录问题 → 先修记录（`task text`/`file add`/`acc add`）；属于他人任务 → 报告 |
| 文件现状 | `add` 的路径不存在；`modify/delete` 的路径存在；`files` 清单跑过 `codegraph impact` | 记录已过期：先更新任务的文件清单并说明，再开工 |
| 契约 | 契约 `status` 不是 `fixme`，或确可 mock | 未冻结 → mock 先行，禁止自造字段名；需要真契约 → 先跑 contract-align |
| 设计锚点 | 有 `design_ref` | 任务含 UI 改动但无锚点 → 停下来问用户要设计稿或确认「沿用现有组件」 |
| 基线约束 | `jiras.baseline` 逐条仍适用 | 与当前实现冲突 → 问用户，不自行解除 |

Gate 全过后**立即留痕**（中断后靠它恢复）：

```bash
python3 "$PTB" start --jira <JIRA> --task <TASK> --msg "Gate 通过（依赖/契约/设计锚点/文件现状已核对）"
```

**`impact` 只作范围输入，不作断言**：改共享 Provider / hook / service 时，对符号跑 `codegraph impact <英文符号名>`，列出的直接消费方必须进回归验收——例如改 `PriceDropBadgeProvider` → 回归 `CollectIcon` 与收藏页 `CollectNewPage`；结果连同 `symbol@file#Lx-Ly` 写进 `acc add --kind regression` 或 `progress`。四条约束同拆解阶段：先英文化、只信 `--json`、退出码恒 0、**字符串事件总线（`emitter.emit('x')` → `.on('x')`）静态图看不到，须 grep 补查**。仓库无 `.codegraph/` 索引时用 grep 核对消费方，并在任务 notes 标注降级。

**约定核对用索引**：开工前先 `python3 "$PTB" index ensure --root <项目根>` 保鲜（没建过就 init、建过就增量 sync），再 `index show --root <项目根> --module <目录>` 看该模块已有的 Provider / hook / 请求收口情况，确认不是「已有实现却按新建做」；结构设计里的 `rule:<规则名>@file#Lx-Ly` 依据可以先这样复跑一遍再动手。

## 2. 执行

- 按 `implementation` 的步骤实现，遵守项目 `AGENTS.md` 的目录、命名、组件、样式约定；
- **复用优先**：能通过 props/slot/className/主题适配的既有组件一律复用，不重复造；
- 只改 `files` 列出的路径。确实需要动其他文件：先 `file add` 登记并说明，再改；
- 视觉部分：调用 **ui-restore** 完整流程（解析稿 → 组件映射 → 缺口确认 → 实现 → 双宽度截图）；
  一次还原覆盖 `design_drafts.covers` 里的多条任务时，按「一稿一轮」组织，不重复解析；
- 发现技术债或记录外的风险：写进 `task notes`（`task text --field notes`）或 `risk add`，**不动手修**；
- **关键节点记一条进度**（`progress`），粒度到「能据此判断下一步做什么」即可：

```bash
python3 "$PTB" progress --jira <JIRA> --task <TASK> --msg "已完成路由骨架，接下来接列表数据"
python3 "$PTB" progress --jira <JIRA> --task <TASK> --kind verify --msg "ui-restore 双宽度截图通过"
```

## 3. 验证

按顺序：

1. 跑 `verifications` 里的命令（如 `pnpm lint` / `pnpm tsc` / `pnpm build`）；
2. 逐条执行 `acceptance`，结果用 `acc set` 回写（每批完成后补一条 `progress --kind verify`）：
   - `kind=functional` → 打开页面/调用接口验证现象；
   - `kind=visual` → 由 ui-restore 截图比对，偏差回查样式对照表；
   - `kind=regression` → 实际回归相邻页面，不能只看代码；回归范围取 Gate 里 `impact` 的结果 ∪ 任务卡列明的受影响方；
   - `kind=contract` → 契约未冻结前标 `blocked`，不标 `pass`；
3. 任何一条失败：先修；修不动（缺契约、缺依赖、记录本身错）→ 任务置 `blocked` 并留事件：
   ```bash
   python3 "$PTB" progress --jira <JIRA> --task <TASK> --kind block \
     --msg "A2 阻塞：契约 C2 的 displayPrice 未冻结，等后端确认"
   ```
   `notes` 同步写清缺什么、谁提供。

## 4. 回写（不可省略）

```bash
python3 "$PTB" acc set <TASK> --jira <JIRA> --id 2 --result pass
python3 "$PTB" complete --jira <JIRA> --task <TASK> \
  --evidence "lint 0; tsc 0; 页面 /shop/... 渲染 12 张车卡" \
  --deviations "无"
```

- `evidence` 写**可核对的证据**（命令 + 结果摘要、页面路径 + 可见现象），不写「已完成」；`complete` 会自动记一条 complete 事件；
- 实现与记录不符（方案不成立、加了文件、改了验收）→ 用 `task text` / `task set` / `file add` / `acc add` **更新记录**，再完成；
- 用户裁决或新决策 → `decision add` / `risk add`；
- **禁止**用 `--force` 掩盖未通过的验收；确需强行完成，`deviations` 必须写明原因。

## 5. 收尾

汇报四件事：完成了什么、验收结果、偏差与风险、下一步可执行的任务（`ready` 列表）。

- 提交：用户要求时才提交，一条任务一个提交，调用 **smart-commit**；
- 建 MR 用 **create-mr**，发布用 **gton**；
- 用户验收后提出的「与设计稿/需求不一致」的修改，交给 **task-retro** 记录，不要只改代码不记录。

## 中断恢复

会话被中断、机器重启、或换了人接手时，按这个顺序恢复，不要直接接着写：

```bash
python3 "$PTB" doctor --jira <JIRA> --root <项目根> --json
python3 "$PTB" log --jira <JIRA> --task <TASK>
```

`doctor` 会给出每个任务的 `claimed_status`（库里的状态）和 `suspected_state`（结合 git 改动/事件/验收推断的实际状态）：

| suspected_state | 含义 | 处理 |
|---|---|---|
| `not_started` | 库和仓库都干净 | 正常开工 |
| `started_no_change` | 已 start 但无改动 | 按 implementation 第一步继续 |
| `partially_implemented` | 有改动/事件但未完成 | 读最近事件 + `git diff` 对齐到具体验收项，从断点继续 |
| `residual_work` | 库说未开始但仓库有改动 | **上次中断未回写**：先核对改动范围，再决定补做或重做 |
| `blocked` | 阻塞中 | 确认阻塞是否解除，解除后 `progress --kind resume` |
| `inconsistent` | 标了 done 却没有完成记录 | 回退为 `doing` 后复核 |

对 `partially_implemented` / `residual_work`，**必须看实际 diff 判断做到哪**——`doctor` 只能告诉你哪些文件动过，不能替你判断代码是否完整。

## 批量模式

用户明确说「连着做」「按顺序全部做完」时：

- 按 `ready` 顺序逐条执行，每条完成即回写并简报一次；
- 遇到 Gate 不通过、验收失败且无法修复、或需要用户拍板的歧义 → **暂停并报告**，不跳过继续；
- 多条任务改同一文件时严格按序，改完一条先验证再下一条。

## 常见错误

- 跳过 Gate 直接写代码：卡片可能已过期（文件被挪、契约已冻结、设计稿换了），返工成本远高于核对；
- 按记录记忆改文件：记录写的是当时的仓库状态，执行前必须重新读；
- 验收写「功能正常」：不可观测，等于没验收；
- UI 任务跳过 ui-restore：自造按钮尺寸、自创布局，是历史上最大的返工来源；
- 完成后不回写：下一个会话不知道进度，`ready` 会继续推荐已做完的任务。
