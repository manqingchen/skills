#!/usr/bin/env bash
# evidence.sh — codegraph 查询并格式化为 symbol@file#Lx-Ly 证据行（validate 唯一可核验格式）
# 用法: evidence.sh query|callers|callees|impact <英文标识符> [-r <项目根>] [-n <条数>]
#   query    盘点同类实现
#   callers  谁在调用它（依赖方向与消费方）
#   callees  它调用了谁（模块职责分布）
#   impact   改动波及面（无 -n，用 codegraph 默认深度）
# 硬约束（与 SKILL 一致）: 只信 --json（codegraph 对未初始化仓库/不存在符号也退出码 0）；
#   中文查询是词法匹配、实测返回 0 条 → 报错不执行；无索引不静默降级 → 报错并提示先 index ensure。
set -euo pipefail

SUB=""; TERM=""; ROOT="."; N=5
while [ $# -gt 0 ]; do
  case "$1" in
    query|callers|callees|impact) SUB="$1"; shift ;;
    -r|--root) ROOT="${2:?--root 需要值}"; shift 2 ;;
    -n|--limit) N="${2:?--limit 需要值}"; shift 2 ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) if [ -z "$TERM" ]; then TERM="$1"; shift; else echo "未知参数: $1" >&2; exit 2; fi ;;
  esac
done
if [ -z "$SUB" ] || [ -z "$TERM" ]; then
  echo "用法: evidence.sh query|callers|callees|impact <英文标识符> [-r <项目根>] [-n <条数>]" >&2
  exit 2
fi

if printf '%s' "$TERM" | LC_ALL=C grep -q '[^ -~]'; then
  echo "错误: 查询词含非 ASCII 字符。codegraph 是词法/结构匹配，中文查询实测返回 0 条——先把 PRD 概念翻成英文标识符（如 降价角标 → priceDropBadge）再查。" >&2
  exit 2
fi
command -v codegraph >/dev/null 2>&1 || { echo "错误: codegraph 未安装。" >&2; exit 2; }
if [ ! -d "$ROOT/.codegraph" ]; then
  echo "错误: $ROOT 无 .codegraph 索引，不静默降级。先跑: python3 <ptb.py 路径> index ensure --root $ROOT" >&2
  exit 2
fi

cd "$ROOT"
if [ "$SUB" = "query" ]; then
  RAW="$(codegraph query "$TERM" -j -l "$N")"
else
  RAW="$(codegraph "$SUB" "$TERM" -j)"
fi

PTB_EV_JSON="$RAW" python3 - "$SUB" "$TERM" <<'PY'
import json, os, sys

sub, term = sys.argv[1], sys.argv[2]
try:
    data = json.loads(os.environ["PTB_EV_JSON"])
except Exception:
    sys.exit("错误: codegraph 输出不是合法 JSON——codegraph 对未初始化仓库/不存在符号也返回退出码 0，只认 JSON 内容。")

rows = []
if isinstance(data, list):  # query: [{node: {...}, score}]
    for it in data:
        n = it.get("node") or {}
        if n.get("name"):
            rows.append(n)
else:  # callers/callees/impact: {symbol, targets: [...], ambiguous, ...}
    if data.get("ambiguous"):
        print("注意: 符号名不唯一，以下为全部同名定义的消费方/波及面，必要时到具体仓库用文件路径区分", file=sys.stderr)
    for n in data.get("targets") or []:
        if n.get("name"):
            rows.append(n)

seen, out = set(), []
for n in rows:
    key = (n.get("name"), n.get("filePath"), n.get("startLine"))
    if key in seen:
        continue
    seen.add(key)
    out.append(f"{n.get('name')}@{n.get('filePath', '?')}#L{n.get('startLine', 0)}")

if not out:
    print(f"0 条命中（{sub} {term}）——换英文标识符重查，或确认符号在本仓库存在")
    sys.exit(0)
print(f"{sub} {term}: {len(out)} 条（symbol@file#Lx 证据格式，可直接写进 brief / 任务 implementation）")
print("\n".join(out))
PY
