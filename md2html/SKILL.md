---
name: md2html
description: 把 Markdown 笔记/长文一键转成米白纸感文档风的单文件 HTML(左侧目录+滚动高亮、暗/亮双主题、深色代码块 zenburn 高亮、零外部依赖)。当用户说「md 转 html」「笔记转网页」「转成 html」「单文件 html」「html 版」「md2html」,或要给一份 .md 配好看可分享的网页版时使用。纯渲染不删内容:表格/脚注/任务列表/代码高亮/图片全保留。
---

# md2html:Markdown → 文档风单文件 HTML

一条命令把 .md 转成自包含 HTML:左侧锚点目录 + 滚动高亮、暗/亮切换(暗→亮 1.8s 慢过渡防闪)、Solarized 纸色亮色 + Nord 深石板暗色、章节序号、深色代码块(zenburn 高亮 + 一键复制)、表格横向滚动、任务列表、脚注、系统字体栈、零外部依赖(样式全内联)。

视觉体系改编自 [haidang1810/md2html](https://github.com/haidang1810/md2html)(MIT),排版管线仍是 pandoc。配色取 [Solarized](https://ethanschoonover.com/solarized/) 亮色与 [Nord](https://www.nordtheme.com/) 暗色,按长时间阅读的可读性微调过对比度。

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
3. **h2 自动编号**:模板用 CSS counter 在每个 h2 上方给出小号章节序号(`01/02`);若 MD 的 h2 自带「1.」序号,脚本自动关闭编号。
4. 改主题色/字体 → 改 `template.html` 顶部 `:root`(亮色=Solarized)与 `[data-theme="dark"]`(暗色=Nord)的 CSS 变量,两套都在那里。亮色正文对比度约 5:1、暗色约 9:1,别往 15:1 以上调(会刺眼)。
5. 模板里带 `/* ===== PANDOC ADAPTER (md2html) ===== */` 注释的段落是本仓库新增的适配层(目录拍平、表格滚动、任务列表、章节序号、代码块配色),改样式优先动这一层,不要动上游样式主体。

## 模板结构(维护用)

`template.html` 是 pandoc 模板:`$body$` 正文、`$table-of-contents$` 目录(需外包 nav)、`title/kicker/subtitle/source/date` 元数据变量、`secnum_off` 开关、`mathjax` 开关。模板内的 JS 不用模板字符串(避免 `${}` 与 pandoc 语法冲突)。

页面级交互都在模板内联 JS 里,不依赖外部脚本:目录拍平(`#TOC` 嵌套 UL → 单列 `a.lvl-3`)、滚动高亮、移动端抽屉、复制按钮、表格横向滚动容器、章节锚点、mermaid(CDN,离线退化为源码)。
