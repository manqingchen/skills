# run 状态与目录布局

每次并行执行是一个 **run**。run 的状态全部落盘，进程重启、会话中断都不丢。**`run.json` 只由 `po.py` 写**，不要手工改；需要改状态就重跑对应子命令。

## 目录布局

```
~/.prd-to-breakdown/
├── prd.db                                  # 唯一数据源（ptb）
├── prd-<ts>-manual.db                      # init / 手工 backup 生成
├── runs/<JIRA>/<YYYYMMDD-HHMMSS>/          # 一次 run
│   ├── run.json                            # 运行状态（po.py 原子写）
│   ├── plan.json                           # 最近一次计划快照
│   ├── specs/<T>.<arm>.md                  # 每任务每臂的自包含 spec
│   ├── mail/start-<T>-<arm>.json|.err      # worker-start 的原始 receipt
│   ├── compare/<T>/                        # 三维比对产物
│   │   ├── <arm>.patch                     # 未提交改动的 patch
│   │   ├── <arm>-verify-<n>.log            # 协调者重跑验证的日志
│   │   ├── compare.json                    # 结构化比对
│   │   └── report.md                       # 人读比对报告
│   ├── logs/integration.log|.patch         # finalize 导出
│   └── integration/                        # 集成分支的独立 worktree
└── reports/<JIRA>/
    ├── latest.html                         # 最新报告（固定路径）
    ├── <ts>.html
    ├── latest.json                         # 报告快照数据
    └── <ts>.json
```

- `PO_RUNS_ROOT` / `PO_REPORTS_ROOT` / `PTB_DB` 可覆盖默认路径（测试用）。
- `<JIRA>/current.json`（在 `runs/<JIRA>/` 下）是「当前活跃 run」指针：`{jira, run_dir, run_id, status, updated_at}`。有未完成 run 时不要新开，先 `status` / `resume`。
- `integration/` 只用于应用选定臂的 patch，**永远不派 worker 进去**。

## run.json schema（schema = 2）

```jsonc
{
  "schema": 2,
  "jira": "OS-43643",
  "title": "…",
  "root": "/abs/business/repo",            // 业务仓库根（协调者运行处）
  "db": "/abs/prd.db",
  "plugin_root": "/abs/plugins/prd-workflow",
  "orca_cmd": "orca",                       // 解析后的 Orca 可执行文件
  "repo_id": "…",                           // orca worktree current 的 repoId
  "run_id": "…",                            // orchestration run-create 返回
  "objective": "prd-orchestrate: OS-43643 …",
  "run_dir": "/abs/runs/OS-43643/…",
  "status": "initialized|running|waiting|waiting_choice|complete|blocked|stalled",
  "created_at": "…", "updated_at": "…",
  "base_ref": "manqingchen/…",
  "base_sha": "…",
  "integration": {
    "mode": "git|orca",
    "branch": "prd/OS-43643/run-20261103-120000",
    "path": "/abs/runs/…/integration",
    "head_sha": "…"                         // 只随 choose 前进
  },
  "options": {"max_parallel": 3, "arms": ["codex", "claude"],
              "setup": "run", "mode": "git"},
  "batches": [{"index": 1, "created_at": "…", "tasks": ["T001"],
               "arms": ["codex", "claude"], "reason": "依赖已满足"}],
  "tasks": {"T001": { /* 见下 */ }},
  "wakes": [{"at": "…", "messages": 2}],
  "questions": [{"message_id": "…", "task": "T001", "arm": "codex",
                 "question": "…", "answer": "…"}],
  "preflight": { /* preflight --json 结果 */ },
  "report": {"html": "…/latest.html", "snapshot": "…/latest.json"}
}
```

### `tasks.<T>` 条目

```jsonc
{
  "title": "…",
  "dispatched_at": "…",
  "arms": {
    "codex": {
      "agent": "codex",
      "dispatched_at": "…",
      "dispatch": {
        "state": "starting|started|running|succeeded|failed|settled",
        "dispatch_id": "…", "orca_task_id": "…",
        "workspace": "<repoId>::<path>",
        "worktree_path": "/abs/arm/worktree",
        "port": 3001,
        "base_sha": "…",                 // 派发时集成分支 HEAD
        "outcome": "succeeded|failed|…",  // Orca 任务结果，不等于验收通过
        "started_at": "…", "done_at": "…", "error": "…"
      },
      "report": { /* normalize 后的 PO_ARM_REPORT.json */ },
      "git": {
        "changed_files": ["src/a.ts"], "extra_files": [],
        "stat": "…", "insertions": 12, "deletions": 3,
        "head_sha": "…", "base_sha": "…", "committed": false,
        "problems": [], "effect_screenshots": 1, "effect_total": 1
      },
      "verification": [{"cmd": "pnpm lint", "rc": 0, "log": "…", "summary": "…"}],
      "collected_at": "…",
      "notes": []
    },
    "claude": { /* 同结构 */ }
  },
  "compare": {
    "state": "pending|ready|chosen|failed",
    "ready_at": "…",
    "patches": {"codex": "/abs/compare/T001/codex.patch", "claude": "…"},
    "report_path": "/abs/compare/T001/compare.json",
    "markdown_path": "/abs/compare/T001/report.md",
    "recommendation": {
      "winner": "codex|null|tie",
      "verdict": "recommend|tie|none",
      "reasons": ["…"],
      "gates": {"codex": [], "claude": ["验收未通过 id=[7]"]}
    }
  },
  "merge": {
    "state": "pending|merged|conflict|failed",
    "arm": "codex",
    "merge_sha": "…", "merged_at": "…",
    "conflicts": [], "error": null
  },
  "notes": ["2026-11-03T12:00:00 双 agent 比对就绪（codex, claude），建议 codex"]
}
```

## 状态机

```
run:  initialized ──dispatch──> running ──finalize──> complete
                                  │   └─ 有待选择比对 → waiting_choice
                                  └─ advance 检测到无 running、无待选、未完成 → stalled

task: (未派发)
  └ dispatch ─> 两条臂各自 running（codex / claude，互不通信）
        ├ 两臂都结束（succeeded / failed） ─> compare ─> compare.ready
        │     ├ 用户 choose <arm> ─> git apply --3way ─> 集成分支提交 ─> 回写 ptb ─> merge.merged
        │     ├ apply 冲突        ─> 回滚集成分支 + ptb blocked + merge.conflict
        │     └ 用户不选          ─> 停留 compare.ready（阻塞后续依赖派发）
        └ 某臂失败 ─> 记录 note；另一臂仍可比对；两臂都失败 → 任务 blocked / 可 --retry
```

**合并状态是派发的硬门槛**：任务 `ptb done` 且本 run `merge.state = merged` 才满足依赖。`compare.ready` 未选择时，`dispatch` 与 `advance` 都不会派发下一批，保证依赖顺序。

## 并发写

- `run.json` 写入：`flock` 排他锁 + 临时文件 `os.replace` 原子替换（`RunStore.update()` 读-改-写在同一把锁内）。
- 状态库：**worker 不写结果**；只有协调者在 `choose` 时写 `acc set` / `complete`，因此不再有双臂竞争写同一条 task。
- 不要并行跑两个 `advance` / `compare` / `choose`——事件消费与集成分支写都不是并发安全的。

## 中断恢复

1. `status --run-dir <RUN_DIR>`：看每任务两臂状态、`compare` 状态、待选择项、冲突、失败与停滞原因。
2. `resume --run-dir <RUN_DIR>`：
   - `orchestration run-use <run_id>` 切回原 run；
   - `worker-list --run <run_id>` 对账，逐臂补写 `dispatch.outcome`；
   - 对每个已记录 `merge.merge_sha` 的任务，用 `git merge-base --is-ancestor <sha> HEAD` 判断是否已在集成分支上，补写 `merge.state=merged`；
   - **不重新派发**已派发过的任务。
3. 再跑一次 `advance`，由它消费遗留消息、对两臂完成的任务做比对并继续派发。

`resume` 只补事实，不猜：worker 是否完成以 Orca 记录为准，是否达标以 `PO_ARM_REPORT.json` + 协调者重跑验证为准。

## 手工介入的边界

| 场景 | 做法 |
|---|---|
| 想放弃某条任务 | `ptb task set <T> --jira <J> --status blocked` + notes，重跑 `advance` |
| 想让某条任务重派 | `dispatch --retry <T>`（清空两臂与比对，重置为 `todo` 后重派；已合并的拒绝） |
| 想选某臂但验收有瑕疵 | `choose --task <T> --arm <arm> --force`，并在 `--deviations` 里说明（由 choose 写入） |
| 想放弃整个 run | 停掉 worker → 先 `finalize` 报告；集成分支与 worktree 由用户决定是否清理 |
| 想改 agent / setup | 只能新开 run；`options.arms` 在 init 时固化 |
