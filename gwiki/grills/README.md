# gwiki 内嵌 grill 提示词（按需 Read）

> **Provenance**：本目录 4 个 `*.md` 提示词文件源自 **Matt Pocock 公开 skills**（mattpocock plugin）。
> 原版权与作者许可参考 `~/.claude/plugins/marketplaces/mattpocock` 仓库。
> 本目录仅作 gwiki 内部按需 Read 使用，不作为客户端独立 skill 注册。

## 4 个变体

| 文件 | 内容 | 适合场景 | 触发条件（建议） |
|------|------|----------|------------------|
| `grills/batch-grill-me.md` | frontier 批问（一次给整轮问题） | 需求冷启动、用户输入 < 50 字或无具体场景 | **首选**：写需求且 KB 上下文为冷启动时 |
| `grills/grilling.md` | 单题深挖（一问一答） | 关键决策点逐项确认 | 用户明确"一问一答" 或 batch 跑完后再深挖某个分支 |
| `grills/grill-me.md` | 转发到 `/grilling`（最简） | 兜底 / 用户要求直接调 /grilling | 一般不主动选 |
| `grills/grill-with-docs.md` | 边问边写 ADR/glossary | 需在需求评审前沉淀架构决策记录 | 仅在用户明确要求写 ADR 时 |

## gwiki 写需求时的调用规则

**默认不 Read**。命中以下任一条件时，**只 Read 一个**文件（不要 4 个都 Read）：

1. **冷启动**（用户输入 < 50 字 或 缺具体场景）→ Read `grills/batch-grill-me.md`
2. **关键决策深挖**（已有粗稿但某项决策未确定）→ Read `grills/grilling.md`
3. **需写 ADR/术语表** → Read `grills/grill-with-docs.md`
4. **用户显式说"用 grill-me"** → Read `grills/grill-me.md`

## 安装后的路径

- 仓库源：`local-skill/skills/gwiki/grills/`
- 用户机器（install.sh 装到）：`~/.codex/skills/gwiki/grills/` `~/.claude/skills/gwiki/grills/` `~/.cursor/skills/gwiki/grills/` 等
- `$GWIKI_SKILL_ROOT` 指向主 skill 根目录，grills 在其下：`$GWIKI_SKILL_ROOT/grills/<变体>.md`

## 与 client skill 菜单的关系

**grills/ 不会出现在客户端 skill 菜单**（不是 sibling 目录）。`install.sh` 的 `[[ -f "$skill_dir/SKILL.md" ]]` 检查会让它**作为 gwiki/ 子目录**被复制到 `~/.X/skills/gwiki/`，但**不会**被识别为独立 skill（grills/ 下没 `SKILL.md`，只有 grills/README.md 和子文件）。
