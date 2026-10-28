#!/usr/bin/env bash
# md2html — 把 Markdown 转成 eli5 风格的自包含单文件 HTML(pandoc)
# 用法: bash build.sh <input.md> [选项]
#   -o <path>        输出路径(默认: 同目录同名 .html)
#   --title <t>      页头标题(默认: YAML title > 首个 H1 > 文件名)
#   --subtitle <s>   页头副标题
#   --kicker <k>     页头眉标(默认: 学习笔记)
#   --source <url>   原文链接(页头 chip + 页脚)
#   --math           数学公式模式(切到 pandoc markdown + MathJax,需联网)
#   --from <fmt>     覆盖输入格式(默认 gfm+emoji;--math 时 markdown+task_lists+emoji)
#   --no-toc         不要左侧目录
#   --keep-h1        不把首个 H1 提升为页头(默认提升并从正文移除,避免标题重复)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/template.html"

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

INPUT="" OUT="" TITLE="" SUBTITLE="" KICKER="" SOURCE="" DATE=""
MATH=0 NO_TOC=0 KEEP_H1=0 FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    --title) TITLE="$2"; shift 2 ;;
    --subtitle) SUBTITLE="$2"; shift 2 ;;
    --kicker) KICKER="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --math) MATH=1; shift ;;
    --no-toc) NO_TOC=1; shift ;;
    --keep-h1) KEEP_H1=1; shift ;;
    --from) FROM="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) if [ -z "$INPUT" ]; then INPUT="$1"; shift
       else echo "未知参数: $1" >&2; usage 1; fi ;;
  esac
done
[ -n "$INPUT" ] || { echo "缺少输入文件" >&2; usage 1; }
[ -f "$INPUT" ] || { echo "找不到输入文件: $INPUT" >&2; exit 1; }
command -v pandoc >/dev/null || { echo "需要 pandoc(brew install pandoc)" >&2; exit 1; }
[ -n "$OUT" ] || OUT="${INPUT%.*}.html"

# ---------- 1. 剥离 YAML front-matter 并读取元数据 ----------
TMP_MD="$(mktemp /tmp/md2html-body.XXXXXX.md)"
trap 'rm -f "$TMP_MD"' EXIT
fm_get() { # $1=key  从已提取的 front-matter 里取值,去引号去尾空白
  printf '%s\n' "$FRONT_MATTER" | grep -m1 -E "^${1}:" \
    | sed -E "s/^${1}:[[:space:]]*//; s/[[:space:]]+$//; s/^['\"]//; s/['\"]$//" || true
}
if head -n1 "$INPUT" | grep -q '^---[[:space:]]*$'; then
  FM_END="$(awk 'NR>1 && /^---[[:space:]]*$/{print NR; exit}' "$INPUT")"
  if [ -n "$FM_END" ]; then
    FRONT_MATTER="$(sed -n "2,$((FM_END-1))p" "$INPUT")"
    [ -z "$TITLE" ]    && TITLE="$(fm_get title)"
    [ -z "$SUBTITLE" ] && SUBTITLE="$(fm_get subtitle)"
    [ -z "$SOURCE" ]   && SOURCE="$(fm_get source)"
    [ -z "$DATE" ]     && DATE="$(fm_get date)"
    [ -z "$KICKER" ]   && KICKER="$(fm_get kicker)"
    sed -n "$((FM_END+1)),\$p" "$INPUT" > "$TMP_MD"
  else
    cp "$INPUT" "$TMP_MD"
  fi
else
  cp "$INPUT" "$TMP_MD"
fi

# ---------- 2. 标题 fallback + 首个 H1 提升 ----------
if [ -z "$TITLE" ]; then
  TITLE="$(grep -m1 -E '^# +[^#]' "$TMP_MD" | sed -E 's/^#[[:space:]]*//; s/[[:space:]]+$//' || true)"
fi
[ -n "$TITLE" ] || TITLE="$(basename "$INPUT" .md)"
if [ "$KEEP_H1" = 0 ]; then
  H1_LINE="$(grep -nE '^# +[^#]' "$TMP_MD" | head -1 | cut -d: -f1 || true)"
  if [ -n "$H1_LINE" ] && [ "$H1_LINE" -le 10 ]; then
    awk -v n="$H1_LINE" 'NR!=n' "$TMP_MD" > "$TMP_MD.tmp" && mv "$TMP_MD.tmp" "$TMP_MD"
  fi
fi

# ---------- 3. h2 自带序号时关闭自动编号徽章 ----------
SECNUM_OFF=0
if grep -qE '^## +([0-9]+|[一二三四五六七八九十]+)[.、:： )]' "$TMP_MD"; then SECNUM_OFF=1; fi

# ---------- 4. 其余默认值 ----------
[ -z "$KICKER" ] && KICKER="学习笔记"
[ -z "$DATE" ]   && DATE="$(date +%F)"
if [ -n "$FROM" ]; then :                       # 用户指定优先
elif [ "$MATH" = 1 ]; then FROM="markdown+task_lists+emoji"
else FROM="gfm+emoji"
fi

# ---------- 5. pandoc 转换 ----------
ARGS=(-f "$FROM" -t html5 -s
  --template="$TEMPLATE"
  --toc-depth=3
  --section-divs
  --embed-resources
  --resource-path="$(dirname "$INPUT")"
  --metadata "title=$TITLE"
  --metadata "kicker=$KICKER"
  --metadata "date=$DATE")
# 注意:pandoc 模板里非空值即 truthy,所以 secnum_off 只在需要时传
[ "$SECNUM_OFF" = 1 ] && ARGS+=(--metadata "secnum_off=true")
[ "$NO_TOC" = 0 ] && ARGS+=(--toc)
[ "$MATH" = 1 ] && ARGS+=(--mathjax)
[ -n "$SUBTITLE" ] && ARGS+=(--metadata "subtitle=$SUBTITLE")
[ -n "$SOURCE" ] && ARGS+=(--metadata "source=$SOURCE")

if pandoc --help 2>&1 | grep -q -- --syntax-highlighting; then
  ARGS+=(--syntax-highlighting=zenburn)
else
  ARGS+=(--highlight-style=zenburn)
fi

pandoc "${ARGS[@]}" "$TMP_MD" -o "$OUT"
echo "✅ 已生成: $OUT"
