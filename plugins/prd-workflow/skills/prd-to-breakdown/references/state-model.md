# 状态模型与 CLI

唯一数据源：`~/.prd-to-breakdown/prd.db`（SQLite）。所有读写通过 `scripts/ptb.py`，不手写 SQL、不生成 md 产物。

```bash
SKILL_DIR="$(cd "$(dirname "<本技能目录>/SKILL.md")" && pwd -P)"
PLUGIN_ROOT="$(cd "$SKILL_DIR/../.." && pwd -P)"
PTB="$PLUGIN_ROOT/scripts/ptb.py"
python3 "$PTB" --help                 # 查看全部命令
python3 "$PTB" task --help            # 查看子命令
```

## 表与字段

| 表 | 作用 | 关键字段 |
|---|---|---|
| `demands` | 需求原文（Demand collection） | `url`, `page_id`, `title`, `path`（人读版）, `llm_path`（模型版）, `jira`, `fetched_at` |
| `jiras` | 一条需求 | `jira`, `title`, `scope`(frontend/backend/both), `status`, `project_brief`, `design_notes`, `baseline`, `out_of_scope` |
| `design_drafts` | 设计稿登记 | `path`, `covers`（覆盖的任务 ID） |
| `requirements` | R1..Rn | `id`, `title`, `description`, `acceptance`, `priority`, `excluded_frontend` |
| `tasks` | 任务 | `id`, `seq`, `title`, `end`, `status`(todo/doing/done/blocked), `estimate`(S/M/L), `parallel`, `milestone`, `design_ref`, `goal`, `not_responsible`, `implementation`, `boundaries`, `notes` |
| `task_deps` | 依赖 | `task_id`, `depends_on` |
| `task_files` | 涉及文件 | `path`, `change`(add/modify/delete/refactor/none), `note` |
| `task_requirements` | 任务↔需求 | `task_id`, `req` |
| `acceptance` | 验收项 | `seq`, `descr`, `kind`(functional/visual/regression/contract), `via`, `command`, `depends_contract`, `result`(pending/pass/fail/blocked) |
| `verifications` | 验证命令 | `cmd` |
| `completions` | 完成记录 | `date`, `evidence`, `deviations` |
| `task_events` | 进度事件（中断恢复依据） | `ts`, `kind`(start/progress/verify/block/resume/complete/note), `message` |
| `contracts` / `contract_fields` | 接口契约 | `id`, `method`, `path`, `status`(fixme/defined/aligned)；字段 `name`, `type`, `required`, `enum_values` |
| `contract_deltas` | 契约对齐差异 | `contract_id`, `field`, `expected`, `actual`, `resolution` |
| `decisions` | 待确认/决策 | `ref`, `question`, `options`, `answer`, `conclusion` |
| `tracking_points` | 埋点 | `event`(exposure/click/pv), `api`, `element`, `timing` |
| `i18n_keys` | 多语言 | `key`, `value`（JSON） |
| `risks` | 风险约束 | `descr`, `note` |
| `corrections` | 纠正记录 | `dimension`, `expected`, `actual`, `root_cause`, `blame`, `rule_gap`, `status`(open/adopted/rejected) |
| `upgrades` / `upgrade_corrections` | 技能升级 | `summary`, `targets`, `commit_hash` |

## 常用命令

```bash
# 需求原文（wiki 下载后登记）
python3 "$PTB" demand add --url <wiki URL> --page-id 123456 --title "..." \
  --path ~/.prd-to-breakdown/demands/OS-xxxx/标题-123456.md \
  --llm-path ~/.prd-to-breakdown/demands/OS-xxxx/标题-123456.llm.md [--jira OS-xxxx]
python3 "$PTB" demand list [--jira X]
python3 "$PTB" demand attach --id 3 --jira OS-xxxx   # 未知 Jira 时先入 _inbox，再归入需求

# 需求
python3 "$PTB" jira add --jira X --title "..." --scope frontend [--brief-file /tmp/b.md] [--baseline "..."]
python3 "$PTB" jira show --jira X [--md]           # 视图（不落盘）
python3 "$PTB" jira list                          # 索引
python3 "$PTB" jira set --jira X --status developing

# 设计稿 / 需求条目
python3 "$PTB" design add --jira X --path /tmp/p.html --covers "T001,T003"
python3 "$PTB" req add --jira X --id R1 --title "..." [--acceptance-file /tmp/a.md]

# 任务
python3 "$PTB" task add --jira X --id T001 --title "..." --end frontend --dep T000 --req R1 \
  [--goal-file] [--implementation-file] [--boundaries-file] [--not-responsible-file] [--notes-file]
python3 "$PTB" task text T001 --jira X --field implementation --file /tmp/impl.md
python3 "$PTB" task set T001 --jira X --status block/doing/... [--design-ref ...]
python3 "$PTB" task show T001 --jira X [--md]
python3 "$PTB" file add --jira X --task T001 --path a/b.ts --change modify
python3 "$PTB" acc add --jira X --task T001 --desc "..." --kind visual --via ui-restore
python3 "$PTB" acc set --jira X --task T001 --id 3 --result pass
python3 "$PTB" verify add --jira X --task T001 --cmd "pnpm lint"

# 执行视图
python3 "$PTB" ready --jira X [--json]             # 依赖已就绪的 todo 任务
python3 "$PTB" validate --jira X [--root <项目根>] [--json]

# 进度留痕与中断恢复
python3 "$PTB" start --jira X --task T001 --msg "Gate 通过"
python3 "$PTB" progress --jira X --task T001 --msg "路由完成，接列表数据"
python3 "$PTB" progress --jira X --task T001 --kind verify --msg "截图比对通过"
python3 "$PTB" progress --jira X --task T001 --kind block --msg "等契约 C2 冻结"
python3 "$PTB" log --jira X --task T001 [--json]
python3 "$PTB" doctor --jira X --root <项目根> [--json] [--only-open]

# 完成
python3 "$PTB" complete --jira X --task T001 --evidence "lint 0; tsc 0" [--deviations "无"]
# 验收未全通过会被拒绝；确需强行完成加 --force 并在 deviations 说明

# 其他
python3 "$PTB" contract add --jira X --id C2 --method POST --path /x --status fixme
python3 "$PTB" contract field --jira X --contract C2 --name price --type number --required
python3 "$PTB" decision add --jira X --question "..." --answer "..." --conclusion-file /tmp/c.md
python3 "$PTB" track add --jira X --task T001 --event click --api sendTrack --element ".btn" --timing "点击后"
python3 "$PTB" risk add --jira X --desc "..." ; python3 "$PTB" i18n add --jira X --key k --value '{"en":"..."}'
python3 "$PTB" export --jira X --out /tmp/x.json   # 快照/排查
python3 "$PTB" backup                               # 手动备份（写操作也会自动备份，保留 20 份）
```

## validate 检查项

错误（阻止交付）：依赖指向不存在的任务、依赖成环、任务引用不存在的需求、标记完成但无完成记录、标记完成但验收未全 pass。
警告：需求无人覆盖、任务无验收、视觉验收未标注 `via=ui-restore`、阻塞任务没写原因、设计稿路径不存在。
带 `--root` 时额外检查：标记 `add` 但文件已存在、标记 `modify/delete` 但文件不存在。

## 环境变量

- `PTB_DB`：库路径（默认 `~/.prd-to-breakdown/prd.db`）
- `PTB_BACKUP_DIR`：备份目录（默认 `~/.prd-to-breakdown/backups`）
