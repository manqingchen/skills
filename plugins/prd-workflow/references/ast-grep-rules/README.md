# ast-grep 规则包（项目索引 · 结构约定层）

`ptb index build` 会读取本目录全部 `*.yml`，对每条规则自动生成 `Ts` / `Tsx` 两个语言变体后扫描全仓（ast-grep 一条规则只能绑定一种语言）。仓库内的覆盖规则放在 `~/.prd-to-breakdown/index/<repo>/rules/`，同 id 覆盖内置规则。

## 覆盖的约定类别

| 规则 id | 回答什么问题 |
|---|---|
| `provider-definition` | 状态放在哪些 Context Provider 里（状态归属候选） |
| `hook-export` | 有哪些可复用的 `use*` hook（抽象候选） |
| `http-request` | 请求从哪儿发出（是否收口在 `service/`） |
| `storage-access` | 什么数据落本地存储（持久化口径） |
| `i18n-usage` | 词典键怎么被消费 |
| `timer-schedule` | 哪些地方有轮询 / 延时 |
| `event-bus` | 事件总线的发布与订阅点（静态图断链处，必须人工配对） |

## 规则写法

- `language` 只写 `Ts`，`ptb` 生成 `Tsx` 变体；pattern 必须同时是合法 TS 与 TSX。
- 匹配标识符前缀用顶层 `constraints`（不能写在 `all` 里面）：

```yaml
rule:
  pattern: export function $F($$$) { $$$ }
constraints:
  F:
    regex: ^use[A-Z]
```

- 只做“发现”，不判违规；是否违反约定由模块分析阶段结合目录判断（例如 `http-request` 命中在 `service/` 外才值得追问）。
