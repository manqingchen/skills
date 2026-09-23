# skills

个人 Agent Skills 合集。每个子目录一个技能（含 `SKILL.md`），兼容 [skills.sh](https://skills.sh) 生态安装。

## 安装

```bash
# 方式一：skills.sh CLI（可加 --skill 指定单个技能）
npx skills add manqingchen/skills

# 方式二：直接克隆
git clone git@github.com:manqingchen/skills.git
```

## 技能列表

| Skill | 说明 |
|---|---|
| [prd-to-flow](./prd-to-flow/) | 把 PRD / 需求文档拆解为可执行任务卡与技术方案：反向调研代码库 → 向用户确认模糊点 → 拆解文档 + 技术方案 + 任务卡 |
| [ui-restore](./ui-restore/) | 设计稿 HTML → UI 像素级还原的强制工作流（解析 design-manifest → 样式对照表 → 双宽度截图比对） |
| [typesafe-ai](./typesafe-ai/) | TypeSafe：把 AI 判断封装成可编程原语（第三方技能，MIT License，见目录内 LICENSE） |
| [ship](./ship/) | 一键发布：智能分批提交并推送当前分支 → 合并到 develop（自动定位或创建 worktree）→ 推送 develop |
| [gz-global-api](./gz-global-api/) | 查询海外站 Java 接口的 OpenAPI 契约：网关 `/os/{service}/...` URI → 拉取测试环境 Swagger，裁剪出目标接口及依赖 |

## 维护

本仓库是技能的唯一工作副本（`~/Documents/workspace/skills`），`~/.zcode/skills/`、`~/.claude/skills/`、`~/.agents/skills/` 里的技能均为指向本仓库的符号链接，直接编辑即生效（`sync.sh` 已不再需要，仅留档）。修改后：

```bash
git add -A && git commit -m "..." && git push
```
