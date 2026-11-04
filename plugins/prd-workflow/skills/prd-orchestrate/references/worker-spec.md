# worker spec 与比对报告协议

`po.py dispatch` 会为**每条臂**生成 `specs/<T>.<arm>.md` 并交给 `orca orchestration worker-start --spec-file`。spec 自包含：worker 不读本技能文档，也不依赖协调者的会话上下文。本文件说明 spec 的组成与它要求 worker 遵守的协议，便于排障和改模板。

## spec 的组成（顺序固定）

1. **运行上下文表**：JIRA、任务、臂（codex / claude）、集成分支、集成分支工作树路径（只读）、状态库绝对路径、`ptb` 绝对命令、独占端口、比对产物路径。
2. **工作区铁律**：只做本任务；只改登记文件；**不 commit / push / merge / rebase / 切分支**；**不执行 `acc set` / `complete` / `block`**；不把全局 `validate/doctor/ready` 当 Gate。
3. **任务卡原文**：`ptb task show <T> --jira <J> --md`。
4. **涉及文件**：任务 `files` 清单。
5. **验收表（只记录）**：`seq | acceptance.id | kind | 验收内容 | 当前库状态`。
   **必须使用真实 `acceptance.id`**（全局自增），不是任务内 `seq`。worker 把它抄进 `PO_ARM_REPORT.json`，不直接写库。
6. **验证命令**：任务 `verifications`，worker 执行后把 `cmd` / `rc` / `summary` 写进报告。
7. **执行协议**：Gate → `start` 留痕（消息带 `[<arm>]` 前缀）→ 实现 → 验证 → 效果留痕 → 写 `PO_ARM_REPORT.json` → 自查。
8. **完成回报模板**（outcome / task / arm / status / files / verify / acceptance / effect / blockers）。
9. **项目上下文**：`export --jira` 的 JIRA 行、契约、设计稿、决策、风险、i18n；`project_brief` 的 `## 结构设计` 原样带入。
10. **结构设计优先级**：`## 结构设计` 高于单卡 `implementation`。

## worker 的写回协议（按时间顺序）

```bash
# 0) 自查（只保留本任务的 validate 错误，忽略其他 worktree 的噪声）
python3 po.py --db <DB> check-task --jira <J> --task <T> --root .

# 1) Gate 通过后开工留痕（只 start/progress，不写结果）
python3 ptb.py --db <DB> start --jira <J> --task <T> --msg "[<arm>] Gate 通过，开始实现"

# 2) 实现中的关键节点
python3 ptb.py --db <DB> progress --jira <J> --task <T> --msg "[<arm>] …"
python3 ptb.py --db <DB> progress --jira <J> --task <T> --kind verify --msg "[<arm>] A<id> pass: <证据>"

# 3) 效果留痕（截图放 worktree 内，不提交）
mkdir -p .po-artifacts/<T>/<arm>
# …截图写入该目录…

# 4) 写比对报告（不 commit、不写 ptb）
$EDITOR PO_ARM_REPORT.json

# 5) 确认改动都在登记范围内
git status --porcelain
```

`PO_ARM_REPORT.json` 示例：

```json
{
  "task": "T001",
  "arm": "codex",
  "status": "done",
  "acceptance": [
    {"id": 7, "result": "pass", "evidence": "src/a.ts:42；pnpm test 输出见 compare 日志"}
  ],
  "verifications": [
    {"cmd": "pnpm lint", "rc": 0, "summary": "0 problems"}
  ],
  "effect": {
    "summary": "列表页新增筛选栏，默认展示全部",
    "demo": "打开 /list → 点击筛选 → 选择状态",
    "screenshots": [".po-artifacts/T001/codex/list.png"]
  },
  "files": ["src/a.ts"],
  "blockers": "",
  "risks": "",
  "notes": ""
}
```

- `status`：`done | blocked | failed`；半成品不允许写 `done`。
- `acceptance` 必须覆盖 spec 验收表全部 `acceptance.id`；缺失项协调者按 `blocked` 处理。
- `files` 填实际改动文件（相对路径），不要包含 `PO_ARM_REPORT.json` / `PO_ARM_REPORT.md` / `.po-artifacts/`。
- 阻塞时只写进报告（`status=blocked` + `blockers`），**不要调用 `ptb block`**。

## 为什么这些约束不能省

| 约束 | 原因 |
|---|---|
| 不 commit | 两臂在同一任务上竞争；未提交改动由协调者用 `git diff <base>` 生成 patch，选定臂后才由协调者提交一次；worker 提交会让 diff 归属混乱 |
| 不写 ptb 结果 | 两臂共享同一条 task 记录，`acc set` / `complete` 会互相覆盖；验收结论进 JSON，`choose` 时统一写库 |
| 不 push / merge / rebase | worker 之间共享远端和分支会互相踩踏；合并只由协调者在集成分支上做 |
| 不跑全局 `validate/doctor/ready` | 其他任务在其他 worktree，全局校验必然报缺文件/residual；worker 用 `check-task` |
| 真实 `acceptance.id` | `id` 是全局自增，`seq` 只在任务内有序；写错 id 会覆盖别人的验收 |
| 独占 `WORKER_PORT` | 两臂 × 多任务并行时不撞端口；验收结束必须结束进程 |
| `database is locked` 退避重试 | 多个 worker 同时写 SQLite；`ptb.py` 已加 busy_timeout，长事务仍可能短暂锁住 |

## 协调者侧的对账（compare）

`worker_done` 只是 **Orca 任务结束**，不是验收通过。两臂都结束后 `compare` 独立核对：

```bash
git -C <worker_path> add -A -N
git -C <worker_path> status --porcelain
git -C <worker_path> diff --name-only <base> -- . ':(exclude)PO_ARM_REPORT.json' \
  ':(exclude)PO_ARM_REPORT.md' ':(exclude).po-artifacts/**'
# 读取 <worker_path>/PO_ARM_REPORT.json
# 在 <worker_path> 串行重跑任务 verifications
```

产出 `compare/<T>/<arm>.patch`、`<arm>-verify-<n>.log`、`compare.json` 与 `report.md`；只有 `choose` 才会应用 patch、提交并回写 ptb。

## 改 spec 模板

模板在 `po.py` 的 `build_spec()`。修改时保持：

- 所有变量用绝对路径（worker 的 cwd 是 worktree，不是协调者目录）；
- 不改「不 commit」「不写 ptb」「真实 id」「必须写 PO_ARM_REPORT.json」四条；
- 新增要求先想清楚协调者能否自动核对——核对不了的要求等于没写。
