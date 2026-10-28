---
name: ast-grep
description: 用 ast-grep 做结构化代码搜索与替换。当文本 grep/正则搜不准或太啰嗦时用:按 AST 模式找代码(如"所有没包 try 的 await 调用""某组件的特定 props 用法""所有 .then 链")、跨格式变体匹配、结构化批量替换。触发词:结构搜索、AST 搜索、按语法找、模式匹配搜索、批量改调用方式。
---

# ast-grep 结构化代码搜索

核心思想:**grep 按字符匹配,ast-grep 按语法树匹配**。缩进、换行、引号风格、空格差异自动忽略;`$VAR` 元变量匹配任意表达式。代码是结构化数据,搜"长得像"不如搜"语法上是"。

本机已装(v0.45+,Homebrew)。注意:旧别名 `sg` 已弃用,一律用 `ast-grep` 命令。

## 何时用哪个

| 场景 | 用什么 |
|---|---|
| 找一个确切的名字/字符串/报错信息 | ripgrep(先走这个) |
| 找"某种形状"的代码:调用方式、组件用法、模式违规 | **ast-grep** |
| 找符号的定义/所有引用(需要精确跳转) | LSP / IDE,或 ripgrep 词边界 |
| 需要理解调用链、影响面 | 先 ast-grep 找点,再读文件 |

判断口诀:**要找的东西能写成一小段"代码模板",就用 ast-grep。**

## 常用命令

```bash
# 搜索。-l 指定语言(只能一个);省略 -l 则按文件扩展名自动识别、全语言扫描
ast-grep run -p 'console.log($$$A)' -l ts
ast-grep run -p 'console.log($$$A)'          # 自动识别,ts/tsx/js 等一起搜

# 结构化替换:默认只打印 diff,确认后加 --update-all(-U)落盘
ast-grep run -p '_.map($ARR, $FN)' -r '$ARR.map($FN)' -l ts
ast-grep run -p '_.map($ARR, $FN)' -r '$ARR.map($FN)' -l ts -U

# 交互式逐个确认替换(-i,不是 -u)
ast-grep run -p 'PATTERN' -r 'REWRITE' -l ts -i

# JSON 输出(供脚本消费,必须用 --json= 形式)
ast-grep run -p 'PATTERN' -l ts --json=pretty

# 复杂规则走 YAML 规则文件(见下文进阶)
ast-grep scan .
```

- 语言别名:`ts` `tsx` `js` `jsx` `html` `css` `json` `yaml` `python` `rust` `go` `java` 等内建;**Vue SFC 未内建**(`-l vue` 报 not supported),.vue 项目先在 ts/tsx 层用,或查官方 languages 文档配自定义语言。
- 退出码:无匹配时 exit 1,不是报错——脚本里注意区分。

## 模式语法:10 分钟够用

模式本身就是一小段**可解析的真实代码**,加上元变量:

- **`$NAME`** — 匹配**单个**任意 AST 节点(一个表达式/标识符/属性)
  - `ast-grep run -p 'fetch($URL)' -l ts` → 所有 fetch 调用,无论 URL 是什么
- **`$$$NAME`** — 匹配**零个或多个**节点(剩余全部)
  - `ast-grep run -p 'logger.info($$$ARGS)' -l ts` → 任意参数个数
- **JSX/模板要点**:属性后要"吸收"多余属性再闭合,否则匹配不上:
  - ✅ `<Modal visible={$V} $$$R>` — 匹配"带 visible 属性"的 Modal 开标签
  - ✅ `<Modal $$$A>$$$C</Modal>` — 整个元素及 children
  - ❌ `<Modal visible={$V}>` — 实际标签后面还有别的属性时不匹配
- 普通代码原样写,自动容忍空格/换行/引号差异

**硬性规则:模式必须是该语言一个完整、合法的语法节点**(一条语句/一个表达式)。写裸片段会报 "Multiple AST nodes are detected":

- ✅ `try { $$$B } catch {}` — 完整 try 语句
- ❌ `catch {}` — 裸 catch 子句不是完整节点,parse error

## 实战模式库(前端/TS 向)

```bash
# 1. await 的结果直接赋值(想排查没包错误处理的,配合人工看上下文)
ast-grep run -p 'const $R = await $P' -l ts

# 2. 所有 .then 链(盘点改 async/await 的存量)
ast-grep run -p '$P.then($F)'

# 3. 某组件的特定用法
ast-grep run -p '<Modal visible={$V} $$$R>' -l tsx

# 4. 空 catch(吞错误)——注意必须带完整 try
ast-grep run -p 'try { $$$B } catch {}'
ast-grep run -p 'try { $$$B } catch ($E) {}'

# 5. lodash 迁移:结构化替换
ast-grep run -p '_.debounce($FN, $WAIT)' -r 'useDebounced($FN, $WAIT)' -l ts

# 6. 遗留 API 调用点盘点
ast-grep run -p 'request.$METHOD("/api/v1/$$$REST", $$$A)' -l ts

# 7. 发布前 console 清理(先看 diff,再 -U)
ast-grep run -p 'console.log($$$A)' -r '' -l ts
```

## 进阶:复杂规则写 YAML(ast-grep scan)

模式表达不了的条件(多 pattern 组合、限制元变量取值、inside/has 结构关系)写规则文件:

```yaml
# rule.yml
id: no-empty-catch
language: TypeScript
rule:
  pattern: try { $$$B } catch ($E) {}
```

```bash
ast-grep scan --rule rule.yml src/
```

一次性任务用 `-p` 就够;要沉淀成团队检查项的再写 yaml。

## 与本机工具链的配合

- 搜索结果带 `file:line:col`,配合 Read 工具跳读上下文
- 替换前先跑一遍看 diff;多文件批量替换后**必须跑类型检查/测试**(tsc/vitest)验证
- monorepo 大库直接在根目录跑,Rust 实现,速度和 ripgrep 同级

## 边界

- 模式匹配是**语法级**:反射调用、字符串拼接的函数名、动态 import 搜不到(这类用 grep 兜底)
- 跨文件分析(谁调用了我)不是它的活,用 LSP 级工具
- 简单文本搜索 ripgrep 仍然更快更直接,不要杀鸡用牛刀
- Vue SFC 未内建支持,见上文"常用命令"
