---
name: md2html
description: 把 Markdown 笔记/长文一键转成 eli5 风格的自包含单文件 HTML(左侧目录+滚动高亮、暗/亮双主题暖色、zenburn 代码高亮、零外部依赖)。当用户说「md 转 html」「笔记转网页」「转成 html」「单文件 html」「html 版」「md2html」,或要给一份 .md 配好看可分享的网页版时使用。纯渲染不删内容:表格/脚注/任务列表/代码高亮/图片全保留。
---

# md2html:Markdown → eli5 风格单文件 HTML

一条命令把 .md 转成自包含 HTML:左侧锚点目录 + 滚动高亮、暗/亮切换(0.5s 渐变防晃眼)、暖色低亮度配色、代码深色块 zenburn 高亮、系统字体栈、零外部依赖(样式全内联)。与 eli5 技能产出的学习笔记 HTML 同一套视觉体系。

## 用法

```bash
bash ~/.zcode/skills/md2html/build.sh <input.md> [选项]
```

默认输出:同目录同名 .html。

| 选项 | 作用 |
|---|---|
| `-o <path>` | 指定输出路径 |
| `--title / --subtitle / --kicker` | 覆盖页头(眉标/标题/副标题) |
| `--source <url>` | 原文链接(页头 chip + 页脚) |
| `--date <d>` | 日期(默认 YAML 里的 date,再默认今天) |
| `--math` | 数学公式模式(pandoc markdown + MathJax CDN;需联网) |
| `--from <fmt>` | 覆盖输入格式(默认 `gfm+emoji`) |
| `--no-toc` | 不要左侧目录 |
| `--keep-h1` | 不把首个 H1 提升为页头标题 |

YAML front-matter 会被自动读取并从正文剥离:`title` `subtitle` `kicker` `date` `source`。

## 不丢内容保证

- 默认输入格式 `gfm+emoji`:表格、任务列表、删除线、脚注、中文锚点全保留;**`$` 永远不会当公式误吃**(技术笔记里的 shell 变量安全)。
- 本地图片内嵌 base64(`--embed-resources`),外链图片保持链接。
- 首个 H1(前 10 行内)被提升为页头大标题并从正文移除——内容没丢,是换了位置;`--keep-h1` 可关闭。
- mermaid 代码块自动接 CDN 渲染;离线时回退显示源码,内容不丢。
- 数学公式默认**不解析**(显示 `$x^2$` 字面量);要渲染必须 `--math`,但该模式下正文裸露的成对 `$` 会被当公式边界,含行内 `$` 变量的笔记慎用(围栏代码块内的不受影响)。

## Agent 使用规则

1. **纯渲染流水线**:禁止为了排版删改 MD 内容;排版不对改 `template.html`,不动内容。
2. **转换后抽查再交付**:对比 MD 与 HTML 的 h2 数、表格行数、代码块数,对得上才算完成。
3. **h2 自动编号**:模板用 CSS counter 给每个 h2 加 `01/02` 徽章;若 MD 的 h2 自带「1.」序号,脚本自动关闭徽章。
4. 改主题色/字体 → 改 `template.html` 顶部 `:root` 的 CSS 变量;亮暗两套都在那里。

## 模板结构(维护用)

`template.html` 是 pandoc 模板:`$body$` 正文、`$table-of-contents$` 目录(需外包 nav)、`title/kicker/subtitle/source/date` 元数据变量、`secnum_off` 开关、`mathjax` 开关。模板内的 JS 不用模板字符串(避免 `${}` 与 pandoc 语法冲突)。
