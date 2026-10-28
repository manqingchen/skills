# cwiki-upload 美化策略

本文件只在用户明确要求美化，或 CLI 传入 `--prettify` 时读取。普通 Markdown 上传不需要应用这里的规则。

## 分工

- `scripts/markdown_storage.py` 负责 Markdown、附件、代码块、表格、目录和轻量 Confluence 宏到 storage XHTML 的基础转换
- `scripts/prettify_storage.py` 负责基于已生成的 storage XHTML 做美化后处理
- Agent 负责信息设计、临时整理版 Markdown 和 `operations` 方案；脚本负责稳定渲染

## 信息设计

- 目标不是“加很多颜色”，而是让 Wiki 更易读、更有层级、更像经过整理的交付文档
- 先提炼阅读路径：读者先看什么结论、再看什么证据、最后执行什么动作
- 必要时可用临时 Markdown 补摘要、拆分长段落、把流程改成步骤；不要修改用户源文件
- 尊重原文组织，不要擅自合并用户已经分开的环境表、阶段表或配置块
- 对技术实践、复盘、方案说明，优先整理成“结论摘要 -> 背景/问题 -> 方案/过程 -> 地址/配置 -> 回顾/下一步”

## 文档类型

- 产品/需求文档：突出目标、范围、用户价值、关键规则、验收口径
- 技术/接口文档：突出架构边界、调用链路、环境、风险、返回/配置对象
- 项目/周报/复盘：突出结论、进展、阻塞、责任人、下一步
- 公告/发布说明：突出影响范围、变更内容、操作指引、风险提醒
- 会议纪要：突出决议、行动项、负责人、时间点

## 视觉规则

- 配色遵守公司色彩规范：主色 `#009B3F`，按下 `#008436`，禁用 `#59BEB2`；急迫色 `#FF5461`，警示色 `#FFB432`，链接色 `#006633`
- 文字色：一级 `#303741`，二级 `#5F6670`，三级 `#8F96A0`，四级 `#BEC5CF`
- 边框和背景：分割线 `#D8DDE6`，背景 `#F5F7FA`，浅绿 `#E5F5EC`，浅橙 `#FFEEE8`，浅灰 `#EFF2F6`
- 规范原色只用于文字、边线、标题左侧强调线等小面积强调
- 作为背景时必须使用 60% 色阶，尤其是 `panelHeader`、`th`、并列卡片标题条：绿 `#66C58C`，警示橙 `#FFCB7A`，急迫红 `#FF98A0`
- 60% 背景上的文字使用白色 `#FFFFFF`
- 普通短文档建议最多 1-2 个 callout；中长文档可用 2-4 个 callout，但必须服务摘要、结论、风险、行动项
- `callout` 默认用于局部摘要，不是整节背景色；长章节、代码块、表格和三级标题保持白底正文
- tag/badge 只用于核心对象，例如产品名、协议名、环境名、状态名；默认轻量矩形，圆角约 4px
- 表格默认浅灰表头和清晰边框；只有核心矩阵或关键对比表才考虑语义色表头
- 多个同构小表格不默认合并；只有用户要求或合并收益明确且不改变阅读意图时才合并
- `columns` 仅用于天然并列对比，例如当前/目标、方案 A/B、风险/收益、前后对比；服务地址、环境地址、配置清单等短表格不要默认做双栏

## 轻量宏语法

````markdown
:::info 结论摘要
这里放 1-3 句关键结论。
:::

:::warning 风险提示
这里放风险、阻塞或短期不可行原因。
:::

:::tip 建议
这里放推荐动作或下一步。
:::

:::panel 问题背景 {tone=info}
这里放需要有标题条的背景、结论或对比说明。
:::

:::expand 配置详情
这里放较长配置、排查过程或补充说明。
:::

:::columns
:::column 50%
这里放左侧内容。
:::
:::column 50%
这里放右侧内容。
:::
:::

:status[已完成]{color=green}
:status[需关注]{color=yellow}
:status[阻塞]{color=red}
:status[强提醒]{color=red subtle=false}

```ts title=instrumentation.ts
export async function register() {}
```
````

- 块级宏：`info`、`note`、`warning`、`tip`、`panel`、`expand`、`columns` / `column`
- 行内宏：`status`，颜色别名支持 `green/success`、`yellow/warning`、`red/danger`、`blue/info`、`grey/default`
- `panel` 支持 `{tone=info|success|warning|danger|neutral}`，由脚本映射到公司规范色
- 宏表达语义，`prettify` 只做宏外标题、普通表格和少量文本的轻量样式

## Operations

优先使用通用 `operations`，不要只列举固定场景：

```json
{
  "operations": [
    {
      "selector": { "type": "section", "title": "二级标题" },
      "action": "callout|section_accent|table_style|two_column|keyword_emphasis|tag",
      "target": "tables",
      "options": {}
    }
  ]
}
```

- 常用动作：`callout` 做重点卡片，`section_accent` 做标题层级，`table_style` 做表格风格，`two_column` / `grid` 做布局，`keyword_emphasis` 做局部强调，`tag` / `badge` 做轻量标签
- `keyword_emphasis` 只用于结论词或关键动作，默认每个关键词只高亮第一次
- `table_style` 默认使用浅灰表头；如果传入规范原色作为 `tableHeaderBg`，脚本会转换成 60% 背景色
- 长方案章节推荐只用标题强调，不用整节面板

## 自检

- 是否有明确的信息层级，而不是只加颜色？
- 是否只保留了少量真正需要突出的重点？
- 是否超过 4 个 callout 或 8 个 tag？如果超过，必须收敛；短文档阈值减半
- 是否把长章节整段包成彩色背景？如果是，改成摘要面板 + 白底正文，或仅保留标题强调
- 是否把地址表、环境表做成了拥挤的双栏卡片？如果是，退回普通表格或轻量表头
- 是否擅自改变了原文的信息组织？如果没有明确收益或用户要求，应保留原结构
- dry-run 摘要中的 `prettify_source` 应为 `agent-plan`
