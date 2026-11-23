#!/usr/bin/env bash
# prd-find.sh — 在项目里定位 PRD / 需求文档候选（§0 读取 PRD 的来源探测）
# 用法: prd-find.sh [<项目根>]（默认当前目录）
# 扫描: docs/ doc/ prd/ 需求/ requirements/ 目录（.md/.docx/.pdf/.txt）
#       以及根下两层的 *.prd.md 与文件名含「需求」的 .md（排除 node_modules）
set -euo pipefail

ROOT="${1:-.}"
if [ "$ROOT" = "-h" ] || [ "$ROOT" = "--help" ]; then
  sed -n '2,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi
if [ ! -d "$ROOT" ]; then
  echo "目录不存在: $ROOT" >&2
  exit 2
fi

FOUND=0
note_hit() { FOUND=$((FOUND + 1)); }
for d in docs doc prd 需求 requirements PRD; do
  if [ -d "$ROOT/$d" ]; then
    while IFS= read -r f; do
      echo "$f"; note_hit
    done < <(find "$ROOT/$d" -maxdepth 2 -type f \( -name '*.md' -o -name '*.docx' -o -name '*.pdf' -o -name '*.txt' \) 2>/dev/null)
  fi
done
while IFS= read -r f; do
  echo "$f"; note_hit
done < <(find "$ROOT" -maxdepth 2 -type f \( -iname '*.prd.md' -o -iname '*需求*.md' \) -not -path '*/node_modules/*' 2>/dev/null)

if [ "$FOUND" -eq 0 ]; then
  echo "未在 $ROOT 找到 PRD 候选（已查 docs/doc/prd/需求/requirements 目录与 *.prd.md / *需求*.md）"
  exit 1
fi
