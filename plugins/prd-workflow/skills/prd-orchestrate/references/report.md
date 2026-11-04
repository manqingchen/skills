# HTML 报告

## 输出

```bash
python3 po.py --db <DB> report --run-dir <RUN_DIR> [--full] [--out-dir <DIR>]
```

```
~/.prd-to-breakdown/reports/<JIRA>/
├── <YYYYMMDD-HHMMSS>.html     # 本次快照
├── latest.html                # 固定入口（覆盖写）
├── <YYYYMMDD-HHMMSS>.json     # 报告数据快照
└── latest.json
```

- **单文件、零外部依赖**：CSS/JS 全部内联，不引用 CDN、字体、图片；离线可开。
- `--full` 额外跑 `doctor --root`（否则用 preflight 里缓存的 doctor 结果）。`advance` 默认用非 full 版本，`finalize` 用 full。
- 生成后终端只打印 `latest.html` 路径；其余信息在报告里。

## 数据来源

| 来源 | 用途 |
|---|---|
| `ptb export --jira` | jira 全字段（brief/baseline/out_of_scope/design_notes）、需求、契约、契约字段、决策、风险、i18n、设计稿、埋点 |
| `ptb task list --jira --json` | 任务清单与状态 |
| `ptb task show <T> --jira --json` | 每任务全量：依赖、文件、验收、验证命令、completion、覆盖需求 |
| `ptb log --jira --task <T> --json` | 事件时间线 |
| `ptb validate --jira --json` | 库一致性（允许 rc=1） |
| `ptb doctor --jira --root --json` | 仅 `--full` / preflight |
| `run.json` / `plan.json` | 每任务两臂（dispatch/report/git/verification）、compare、merge、批次、wakes |
| `compare/<T>/compare.json` | 比对详情的结构化来源（report 直接使用 run.json 中同构的 `compare`） |
| `git` | 集成分支 log / diff（finalize 导出 patch，报告里指向文件） |

数据模型（`collect()` 返回，也是 snapshot JSON 的结构）：

```
{jira, title, jira_row, run, plan, tasks[], requirements[], contracts[], contract_fields[],
 decisions[], risks[], design_drafts[], i18n_keys[], tracking_points[], validate, doctor,
 stats, integrity, generated_at}
```

`tasks[]` 每项：`id/title/status/end/estimate/seq/milestone/design_ref/goal/not_responsible/
implementation/boundaries/notes/depends_on/files/acceptance/verifications/completions/
requirements/events/run`；`run` 内含 `arms{codex,claude}`、`compare`、`merge`。

## 版块

1. **Hero**：JIRA + 标题、范围/run/arms/并发/base/集成 HEAD、生成时间；「切换主题」「打印」按钮（`no-print`）。
2. **KPI 磁贴**：任务完成 x/y（含 todo/doing/blocked 明细）、验收通过 x/y、双 Agent 臂成功/失败、已采纳 + 待选择、需求覆盖、需人工处理（blocked + conflict）。
3. **状态堆叠条**：done / doing / todo / blocked，2px 间隔；图例带色块 + 文字 + 数量。
4. **双 Agent 比对**：每个派发过的任务一组表格，列为 `臂 / 报告状态 / 代码 diff / 验收 / 验证 / 效果 / 截图 / patch`，标题显示 compare 徽章、merge 徽章与机器建议；`compare=ready` 且未采纳时给出 `po.py choose …` 命令。
5. **数据一致性**：见下节；无问题时显示一条 good 行。
6. **项目上下文**：项目 Brief（`## 结构设计` 原样保留）、基线约束、范围外、设计稿，等宽字体、限高滚动、默认折叠在 `<details>` 里。
7. **波次与批次**：run.json 的 `batches[]`，含拆批原因；`plan.json` 的全量拓扑预览。
8. **任务看板**：每条一个 `<details class="task">`，摘要行显示 id/标题/状态徽章/端/估时/验收进度/依赖/compare/merge；展开后是目标/不负责/实现/边界、依赖与文件、验收表（含结果）、**双 Agent 与比对**（每臂 dispatch/outcome/worktree/端口/报告状态/diff/验收/验证/效果/截图/耗时）、evidence/deviations、事件时间线。
9. **需求覆盖**：R1..Rn → 覆盖它的任务、优先级、验收。
10. **接口契约 / 决策 · 风险 · 埋点 / i18n / 设计稿**。
11. **库校验**：validate errors/warnings 全量。
12. **合并与下一步**：待选择任务的选择命令、冲突、失败臂、集成分支合并命令、阻塞处理建议。**不自动执行任何写操作**。

## 数据一致性检查清单

`integrity()` 逐条产出 `(level, ref, message)`：

| 检查 | level |
|---|---|
| `done` 但没有 completion | error |
| `done` 但验收未全 `pass` | error |
| `merged` 但库状态不是 `done` | error |
| 某臂启动失败 / `outcome=failed` | error |
| 某臂已完成但没有 `PO_ARM_REPORT.json` | error |
| 两臂都已结束但尚未比对（`compare` 不在 ready/chosen 且未合并） | warn |
| 采纳时 patch 应用冲突（附错误摘要） | error |
| 改动超出登记文件（按臂） | warn |
| `validate` 的 errors / warnings | error / warn |

> worker 不写 ptb，因此「done 但未合并」不再由 worker 产生；任务在 `choose` 后才变 `done`，两者应同时成立。

## 实现约束

- **所有 ptb 文本必须 HTML 转义**（`esc()`）；notes / evidence 可能包含 `<script>`。
- **状态不只靠颜色**：每个状态徽章 = 色点 + 图标 + 中文标签；堆叠条每段都有图例。
- 状态色固定为 dataviz 状态盘：good `#0ca30c`、warning `#fab219`、critical `#d03b3b`、neutral `#898781`；文字一律用 ink 变量，不用数据色。
- 亮/暗主题：`@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` 与 `:root[data-theme="dark"]` 两处都声明；切换按钮写 `localStorage`。
- 打印：`beforeprint` 展开所有任务卡、`afterprint` 恢复；隐藏按钮；去掉阴影、取消限高。
- 响应式：窄屏任务摘要换行、表格横向滚动；正文不横向溢出。
- 不引入图表库；堆叠条用纯 CSS flex。若未来加图表，先按 `dataviz` 技能校验调色板。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| 报告里没有两臂信息 | `run.json` 不存在或路径不对；报告仍会渲染 ptb 部分 |
| 「双 Agent 比对」为空 | 还没有任何任务被双路派发，或 run.json 是旧 schema 1 |
| 大量「两臂已结束但尚未比对」 | `advance` 没跑或 compare 失败；运行 `compare --run-dir …` |
| 一直 `waiting_choice` | 用户还没执行 `choose`；这是设计行为，不会自动合并 |
| validate 报缺文件 | 报告默认不带 `--root`，不应出现；若出现说明用了 `--full`，属预期信号 |
| 页面空白/样式缺失 | 检查 `report_template.html` 是否被误改；模板必须内联全部样式 |
| 打开很慢 | 任务很多时每个任务都跑 `task show` + `log`；可接受，必要时减少 full 模式调用 |
