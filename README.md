# skills

个人 Agent Skills 合集。每个子目录一个技能（含 `SKILL.md`），兼容 [skills.sh](https://skills.sh) 生态安装。

## 安装

```bash
# 方式一：skills.sh CLI（可加 --skill 指定单个技能）
npx skills add manqingchen/skills

# 方式二：直接克隆
git clone git@github.com:manqingchen/skills.git
```

### 以插件安装（prd-workflow）

`plugins/prd-workflow/` 同时带两份 manifest，同一份源码可分别装进 ZCode 与 Claude Code：

| 平台 | 市场文件 | 操作 |
|---|---|---|
| ZCode | `plugins/marketplace.json` | Plugin Marketplace → Add → 粘贴 `~/Documents/workspace/skills/plugins` → 选 `dev-prd-workflow` → `prd-workflow` → Install |
| Claude Code | `.claude-plugin/marketplace.json` | `/plugin marketplace add ~/Documents/workspace/skills` → `/plugin install prd-workflow@dev-prd-workflow` |

> 两份 manifest 的 `name` / `version` 必须保持一致；改插件时同时更新 `.zcode-plugin/plugin.json`、`.claude-plugin/plugin.json` 与两个 marketplace 条目。
> 已通过软链使用这三个技能时，**安装插件后请删掉 `~/.zcode/skills`、`~/.claude/skills`、`~/.agents/skills` 下的对应软链**，否则同名技能会重复注册。

## 技能列表

| Skill | 说明 |
|---|---|
| [prd-workflow（插件）](./plugins/prd-workflow/) | 需求交付工作流插件 v1.1.0：打包 prd-to-breakdown / task-runner / task-retro 三个技能与共享 CLI `scripts/ptb.py`，共用 SQLite 状态库 `~/.prd-to-breakdown/prd.db`；本地测试市场见 `plugins/marketplace.json` |
| [prd-to-breakdown](./plugins/prd-workflow/skills/prd-to-breakdown/) | 把 PRD / 需求文档拆解为可执行任务：反向调研代码库 → 向用户确认模糊点 → 写入 SQLite 状态库（`~/.prd-to-breakdown/prd.db`，需求/任务/验收/契约/决策/埋点），不产出 md |
| [task-runner](./plugins/prd-workflow/skills/task-runner/) | 按任务记录驱动开发：取依赖就绪的任务 → 开工 Gate（文件现状/契约/设计锚点/基线）→ 实现并逐条验收 → 回写完成记录与状态；视觉委托 ui-restore，提交委托 smart-commit |
| [task-retro](./plugins/prd-workflow/skills/task-retro/) | 纠正记录与技能升级：捕获验收时的偏差（期望/实际/改动/归因层），定期聚合复发问题，向 prd-to-breakdown / task-runner 提出具体改法并留痕 |
| [ui-restore](./ui-restore/) | 设计稿 HTML → UI 像素级还原的强制工作流（解析 design-manifest → 样式对照表 → 双宽度截图比对） |
| [typesafe-ai](./typesafe-ai/) | TypeSafe：把 AI 判断封装成可编程原语（第三方技能，MIT License，见目录内 LICENSE） |
| [ship](./ship/) | 一键发布：智能分批提交并推送当前分支 → 合并到 develop（自动定位或创建 worktree）→ 推送 develop |
| [smart-commit](./smart-commit/) | 智能分批提交（ship 提交阶段的委托执行者）：敏感文件预检 → 按「一个提交只做一件事」逻辑分组 → conventional 中文提交 → 核对输出分组清单；只提交不推送 |
| [gz-global-api](./gz-global-api/) | 查询海外站 Java 接口的 OpenAPI 契约：网关 `/os/{service}/...` URI → 拉取测试环境 Swagger，裁剪出目标接口及依赖 |
| [contract-align](./contract-align/) | 承接 prd-to-breakdown 产出，后端技术文档到位后做接口字段增量对齐：三源比对（前端需求 × 后端文档 × OpenAPI）+ 铁律「未确认必问清、不清晰必问到清晰」，结论写入状态库 contracts/deltas/decisions 并回扫任务验收 |
| [guazi-sso-login](./guazi-sso-login/) | 获取瓜子 SSO Cookie / CHDSSO token / cwiki 登录态，供其他技能解决未登录、Cookie 失效（E_NO_COOKIE、GUAZISSO 等）|
| [cwiki-download](./cwiki-download/) | 把 Confluence（cwiki.guazi.com）页面下载为本地 Markdown |
| [cwiki-upload](./cwiki-upload/) | 把本地 Markdown 发布回 Confluence：spaceKey + parentPageId 建子页、同名默认拒绝覆盖、按引用上传附件、可追加评论 |
| [notion-api](./notion-api/) | 用 Notion REST API 读写页面/数据库/块：只需 `NOTION_API_TOKEN`（集成 token），不走 MCP、不用浏览器授权；技能来自 intellectronica/agent-skills（第三方） |

> guazi-sso-login / cwiki-download / cwiki-upload 三个来自公司 SkillsHub（`npx --registry=http://npm.guazi-corp.com grapehub-cli@1 install <slug>`），目录内 `.grapehub-*.json` 是 CLI 安装元数据，更新/卸载走 `grapehub-cli update/uninstall`。

## 维护

本仓库是技能的唯一工作副本（`~/Documents/workspace/skills`），`~/.zcode/skills/`、`~/.claude/skills/`、`~/.agents/skills/` 里的技能均为指向本仓库的符号链接，直接编辑即生效（`sync.sh` 已不再需要，仅留档）。修改后：

```bash
git add -A && git commit -m "..." && git push
```
