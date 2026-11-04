---
name: prd-orchestrate
description: 用 Orca 的多 Agent / 多 worktree 把 prd-to-breakdown 的任务卡并行做掉——按依赖与文件重叠分批，把同一任务同时派给 codex 与 claude 两臂，各自在隔离 worktree 中实现且都不提交；两臂结束后从「代码 diff / 结果 / 效果」三维度比对，给出机器建议，由用户人工选定一臂后才合入集成分支并回写状态库，最后产出可离线打开的单文件 HTML 汇总。只要用户说「并行做任务」「多个任务一起做」「用 orca 跑任务」「开几个 worker」「把 OS-xxxx 的任务批量做掉」「批量执行任务卡」「跑一轮并行开发」「双 agent 对比着做」，就必须使用本 skill。只做一条、按顺序做用 task-runner；只比较两个 agent 的手工方案用 dual-agent-compare，不要用本 skill。
---

# prd-orchestrate：双 Agent 并行 → 三维比对 → 人工选择 → HTML 汇总

协调者只做五件事：**排期、双路派发、比对、等待用户选择、汇总**。实现由 codex / claude 两臂分别在隔离 worktree 内完成；**worker 一律不提交，协调者也不自动提交或合并**——只有用户执行 `choose` 后，才把选定臂的 patch 应用到集成分支并写回 ptb。

```bash
SKILL_DIR="$(cd "$(dirname "<本技能目录>/SKILL.md")" && pwd -P)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd -P)"
PTB="$PLUGIN_ROOT/scripts/ptb.py"
PO="$SKILL_DIR/scripts/po.py"
DB="${PTB_DB:-$HOME/.prd-to-breakdown/prd.db}"
```

数据源只有 `~/.prd-to-breakdown/prd.db`；`po.py` 与 worker 都只通过 `ptb` 读写，**禁止直接 SQL**。运行状态在 `~/.prd-to-breakdown/runs/<JIRA>/<ts>/`（`run.json` + `plan.json` + `specs/` + `mail/` + `compare/`）。

排障或需要细节时按需读：`references/run-state.md`（run.json schema、状态机、中断恢复）、`references/worker-spec.md`（spec 组成与 `PO_ARM_REPORT.json` 协议）、`references/report.md`（报告数据模型与版块）。

## 铁律

1. **只在业务仓库根目录运行**。协调者当前 worktree 保持不动；集成在独立 worktree `prd/<JIRA>/run-<ts>` 上完成。
2. **同一任务双路派发**：`codex` 与 `claude` 各一个 worker、各一个 worktree、各一份 spec（`specs/<T>.<arm>.md`），互不通信。
3. **worker 绝不写提交、也绝不回写 ptb**：不 `commit` / `push` / `merge` / `rebase`，不 `acc set` / `complete` / `block`；所有结论写进 worktree 根目录的 `PO_ARM_REPORT.json`，截图放 `.po-artifacts/<T>/<arm>/`。两臂共享同一条任务记录，写库会互相覆盖。
4. **可派发 ≠ `ptb ready`**。任务可派发必须同时满足：`todo`、依赖 `done` 且有 completion、依赖在本 run 中 `merge=merged`、与运行中任务无文件重叠、未派发过。`ready` 只作输入。
5. **`done` 但未合并 ≠ 依赖已满足**；且只有 `choose` 后任务才算进入集成分支，依赖任务才能派发。
6. **两臂都结束后必须做三维比对**：代码 diff（改动文件与增删行）、结果（验收/验证命令）、效果（`effect.summary` / demo / 截图）。缺 `PO_ARM_REPORT.json` 的臂不能入选。
7. **绝不自动选择、提交或合并**：比对只产出建议；必须由用户明确执行 `choose --task <T> --arm <codex|claude>`，协调者才应用 patch、提交集成分支并回写 ptb。
8. **一次唤醒只 `check --run <id>` 一次**；不轮询、不写 watch 脚本。
9. **不自动 push、不自动建 MR、不自动并回用户分支、不自动删 worktree**——都要用户明确确认。
10. **不静默结束**：无运行中臂、无可派发、也未全部合并时判 `stalled`/`waiting_choice`，列出原因并报告。

## 0. 预检（只读，任一不过就停）

```bash
python3 "$PO" --db "$DB" preflight --jira <JIRA> --root "$(git rev-parse --show-toplevel)" --json
```

| 检查 | 通过条件 | 不通过 |
|---|---|---|
| git 状态 | `git status --porcelain` 为空 | 让用户先提交/清理；协调者不接管未提交改动 |
| 进行中操作 | 无 `MERGE_HEAD` / rebase / cherry-pick / bisect | 停下，交用户处理 |
| 库一致性 | `validate --jira` 0 error（**不带 `--root`**） | 记录问题先修记录，不带着错误开跑 |
| 库 vs 仓库 | `doctor` 只作信号 | 有 `doing / residual_work / inconsistent` → 先问用户对账 |
| repoId | `orca worktree current` 与仓库根一致 | 不一致或未解析到 → 停下确认 |
| 未完成 run | `<JIRA>/current.json` 无活跃 run | 有 → `status` + `resume`，不新开 run |

预检通过后向用户确认：JIRA、两臂（默认 `codex,claude`）、`max_parallel`（默认 3）、`--setup run|skip`、base ref、是否只出计划。

## 1. 计划

```bash
python3 "$PO" --db "$DB" plan --jira <JIRA> --max-parallel 3 [--conservative] [--run-dir <RUN_DIR>]
```

- 只把 `todo` 纳入候选；`doing / blocked / done` 只报告。
- 按依赖拓扑分 wave，同 wave 内按 `files` 规范化路径做**前缀重叠**检查后拆 batch；与运行中任务重叠的不派发。
- `files` 为空 → `file_unknown`：默认单独成批；`--conservative` 全部串行。
- `max_parallel` 数的是**任务**，每个任务会占用 2 个 worker。

## 2. 初始化

```bash
python3 "$PO" --db "$DB" init --jira <JIRA> --root "$ROOT" \
  --arms codex,claude --setup run --max-parallel 3 [--base-ref <ref>] [--integration-mode git|orca]
```

做四件事：`ptb backup`、`orchestration run-create`、建集成分支与独立 worktree、写 `run.json` 与 `<JIRA>/current.json`（schema 2，`options.arms`）。`--dry-run` 只打印命令。

## 3. 双路派发

```bash
python3 "$PO" --db "$DB" dispatch --run-dir <RUN_DIR> [--max N] [--conservative] [--retry T001] [--dry-run]
```

对每个选中任务、每条臂生成 spec（`specs/<T>.<arm>.md`），随后并行 `worker-start --agent <arm>`，receipt 写入 `mail/start-<T>-<arm>.json` 与 `run.json` 的 `tasks.<T>.arms.<arm>.dispatch`。

- worktree 由 Orca 新建（`--worktree new-top-level`），base 为集成分支；两臂端口不相交（`3000 + task_index*arms + arm_index + 1`）。
- **两臂都是本机 CLI**：Orca 用 `--agent codex` / `--agent claude` 直接启动本机的两个 TUI agent；命令不传 `--environment` / `--on`，worker 固定落在当前本机 Orca runtime。两个 `worker-start` 在同一轮并行拉起，再统一等待 receipt。
- **权限沿用 Orca 的 new-agent-tab 设置**（`po.py` 不注入权限参数）。无人值守时请在 Orca 里配好：claude 自动接受编辑与常用命令，codex 使用 `workspace-write` 沙箱；否则 worker 可能停在权限确认上，只能靠 `status` 的停滞检测发现。
- spec 明确要求：**不提交、不 push/merge/rebase、不写 ptb、必写 `PO_ARM_REPORT.json`**。
- 启动失败**绝不自动重跑**；记录 `dispatch.state=failed`，本轮继续其他任务。用户确认后 `--retry <T>` 清空该任务两臂并回到 `todo` 重派。
- 存在待选择的比对（`compare.state=ready` 未合并）时，`dispatch` 拒绝派发下一批——先选择，保证依赖顺序。

## 4. 唤醒与推进

```bash
python3 "$PO" --db "$DB" advance --run-dir <RUN_DIR>
```

`advance` 一次完成：`check` → 归一化消息到对应臂 → 对两臂都结束的任务 `compare` → 无运行臂且无待选择时派发下一批 → 刷新报告 → 停滞检测。也可拆开：

```bash
python3 "$PO" --db "$DB" check   --run-dir <RUN_DIR>   # 只消费一次 Run 级消息
python3 "$PO" --db "$DB" compare --run-dir <RUN_DIR> [--task T001] [--no-verify]
python3 "$PO" --db "$DB" status  --run-dir <RUN_DIR>   # 含每任务两臂状态与待选择项
```

消息处理：

| 消息 | 处理 |
|---|---|
| `question` | 能从 spec/任务卡推出的直接 `answer`；推不出问用户（记录 `arm`） |
| `escalation` / `stopReason` | `worker-show` 查看现场；必要时 `send` 引导，不算成功 |
| `worker_done` | 只是 Orca 任务结束，不写 ptb；更新对应 `arms.<arm>.dispatch` |
| `outcome=failed` | 该臂失败，记录 note；另一臂仍可参与比对 |
| `succeeded` | 等另一臂结束，然后比对 |

## 5. 三维比对（只读，不合并）

`compare` 对每个两臂都已结束的任务：

1. 读取两臂 worktree（`git add -A -N` 后对 base 做 diff，排除 `PO_ARM_REPORT.json` / `.md` / `.po-artifacts/**`），生成 `compare/<T>/<arm>.patch`；
2. **代码 diff**：改动文件数、增删行、stat、超出登记文件清单；
3. **结果**：解析 `PO_ARM_REPORT.json` 的 `status` 与 `acceptance[]`，协调者再串行重跑任务验证命令（`compare/<T>/<arm>-verify-<n>.log`）；
4. **效果**：`effect.summary` / `effect.demo` / 截图存在数；
5. 写 `compare/<T>/compare.json` 与 `compare/<T>/report.md`，并给机器建议：只有报告 `status=done`、验收全覆盖且全 pass、验证 rc=0、确有改动的臂才合格；两臂都合格时推荐改动更小的一臂；两臂都不合格则 `winner=null`。

比对阶段**不提交、不合并、不回写 ptb**。

## 6. 人工选择（唯一的合并入口）

```bash
python3 "$PO" --db "$DB" choose --run-dir <RUN_DIR> --task <T> --arm <codex|claude> [--force] [--dry-run]
```

1. 校验比对已就绪、臂存在、门禁达标（未达标需用户显式 `--force`）；
2. 集成工作区必须干净；`git apply --3way <patch>`；失败 → `reset --hard` + `clean -fd`、`merge.state=conflict`、任务 blocked；
3. 成功 → `git add -A && git commit -m "feat(<JIRA>): <T> <title> [<arm>]"`，`merge.state=merged`、`compare.state=chosen`、`integration.head_sha` 更新；
4. 按选定臂报告逐条 `ptb acc set --result`，再 `ptb complete --evidence "arm=..., commit=..., acc=n/m, files=..."`；验收未全过时才 `--force`；
5. 释放该任务两臂终端（不删 worktree）。

**没有其他路径会写 ptb 完成状态或提交集成分支。**

## 7. 报告

```bash
python3 "$PO" --db "$DB" report --run-dir <RUN_DIR> [--full]
```

产出 `~/.prd-to-breakdown/reports/<JIRA>/latest.html`（及时间戳副本与 snapshot JSON）。单文件、零外部依赖、亮/暗主题、可打印。

报告版块：Hero → KPI → 状态条 → **双 Agent 比对**（每任务一表：臂 / 报告状态 / 代码 diff / 验收 / 验证 / 效果 / 截图 / patch，附机器建议与 `choose` 命令）→ 数据一致性 → 项目上下文 → 批次时间线 → 任务看板（含两臂详情）→ 需求覆盖 → 契约/决策/风险 → 合并与下一步。

## 8. 收尾与恢复

```bash
python3 "$PO" --db "$DB" finalize --run-dir <RUN_DIR> [--release]
```

- `validate --root <集成分支 path>` 仅作信号；导出 `git log/diff --stat` 与 patch；生成最终报告。
- 仍有 `waiting_choice` 时状态保持，不会误报完成；并回用户分支、push、建 MR、删 worktree 都必须用户明确确认。

中断后：

```bash
python3 "$PO" --db "$DB" status --run-dir <RUN_DIR>
python3 "$PO" --db "$DB" resume --run-dir <RUN_DIR>
```

`resume` 用 `run-use` + `worker-list` / `worker-show` 对账每条臂，重建 merge 状态；不重派已完成 worker。

## 常见错误

- **让 worker 提交或回写 ptb**：两臂会互相覆盖任务记录，且破坏「先比后选」；一律只在 `PO_ARM_REPORT.json` 留结论。
- 看到一臂 `worker_done` 就合并：必须等两臂都结束、完成三维比对、用户 `choose`。
- 拿 `ptb ready` 当派发依据：ready 不看本 run 的合并状态。
- 比对阶段顺手提交：`compare` 只读，唯一写入口是 `choose`。
- 验收未过也直接选：`choose` 默认拒绝，确需强行采纳由用户加 `--force`。
- 冲突后手工解掉继续跑：停链交用户。
- 一次唤醒连跑多次 `check`：会抢走后续批次的消息，永远一次唤醒一次 check。
