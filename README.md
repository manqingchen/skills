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

## 维护

技能的**工作副本**在 `~/.zcode/skills/`（ZCode 从这里加载）。修改技能后同步到本仓库：

```bash
./sync.sh        # 从 ~/.zcode/skills/ 拉取所有技能（跟随符号链接、排除 .git）
git add -A && git commit -m "..." && git push
```
