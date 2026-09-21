#!/usr/bin/env bash
# 把 ~/.zcode/skills/ 下的所有技能同步到本仓库（跟随符号链接、排除 .git）
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$HOME/.zcode/skills"

if [ ! -d "$SRC" ]; then
  echo "错误：找不到技能目录 $SRC" >&2
  exit 1
fi

for skill_dir in "$SRC"/*/; do
  name="$(basename "$skill_dir")"
  rsync -a --delete -L --exclude '.git' "$skill_dir" "$REPO_DIR/$name/"
  echo "已同步: $name"
done
