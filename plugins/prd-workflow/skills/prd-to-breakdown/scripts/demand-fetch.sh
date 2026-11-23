#!/usr/bin/env bash
# demand-fetch.sh — wiki 需求原文一键下载入库（cwiki-download + ptb demand add）
# 用法: demand-fetch.sh <wiki-url|pageId> [--jira OS-xxxx] [--recheck]
# 行为:
#   未传 --jira 落 demands/_inbox，传了落 demands/<JIRA>；下载后自动登记（同 URL 覆盖登记）
#   --recheck: URL 已入库时先下到临时目录，只比对头部「当前版本/更新时间」元数据，
#     一致 → 输出「未变更」退出 0（不覆盖、不做内容 diff）；
#     不一致 → 输出 diff 摘要（前 40 行），覆盖文件并重登记
# 环境: PTB_HOME 覆盖 ~/.prd-to-breakdown；CWIKI_SKILL_ROOT 显式指定 cwiki-download 技能目录
set -euo pipefail

PTB_HOME="${PTB_HOME:-$HOME/.prd-to-breakdown}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PTB="$(cd "$SCRIPT_DIR/../../.." && pwd -P)/scripts/ptb.py"

URL=""; JIRA=""; RECHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --jira) JIRA="${2:?--jira 需要值}"; shift 2 ;;
    --recheck) RECHECK=1; shift ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) if [ -z "$URL" ]; then URL="$1"; shift; else echo "未知参数: $1" >&2; exit 2; fi ;;
  esac
done
if [ -z "$URL" ]; then
  echo "用法: demand-fetch.sh <wiki-url|pageId> [--jira OS-xxxx] [--recheck]" >&2
  exit 2
fi

find_cwiki_root() {
  if [ -n "${CWIKI_SKILL_ROOT:-}" ] && [ -f "$CWIKI_SKILL_ROOT/run.sh" ]; then
    echo "$CWIKI_SKILL_ROOT"; return 0
  fi
  local dir="$SCRIPT_DIR"
  while [ "$dir" != "/" ] && [ "$dir" != "$HOME" ]; do
    if [ -f "$dir/cwiki-download/run.sh" ]; then echo "$dir/cwiki-download"; return 0; fi
    dir="$(dirname "$dir")"
  done
  if [ -f "$HOME/.claude/skills/cwiki-download/run.sh" ]; then
    echo "$HOME/.claude/skills/cwiki-download"; return 0
  fi
  return 1
}
CWIKI_ROOT="$(find_cwiki_root)" || { echo "错误: 找不到 cwiki-download 技能（run.sh）。用 CWIKI_SKILL_ROOT 显式指定。" >&2; exit 2; }

# 下载并输出 md 路径（run.sh 最后一行 stdout 是 markdown 路径）
cwiki_download() { # $1=目标目录
  local out rc md
  set +e
  out="$(bash "$CWIKI_ROOT/run.sh" "$URL" --output-dir "$1" 2>&1)"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$out" >&2
    echo "下载失败：若报 E_NO_COOKIE / E_MISSING_CREDENTIALS，请引导用户初始化 guazi-sso-login（不在本流程收集账号密码）；可请用户先粘贴正文，并把「需求原文未入库」写进 risk。" >&2
    exit "$rc"
  fi
  md="$(printf '%s\n' "$out" | tail -n 1)"
  case "$md" in
    *.md) echo "$md" ;;
    *) echo "错误: 未能从 cwiki-download 输出解析出 .md 路径（最后一行: ${md}）" >&2; exit 2 ;;
  esac
}

# 提取文件头部的版本元数据（已验证落盘文件头两行固定为「当前版本：N / 更新时间：…」）
meta_of() { head -n 5 "$1" 2>/dev/null | grep -E '^(当前版本|更新时间)：' || true; }

# 该 URL 已入库则输出已登记 path，否则输出空
existing_path() {
  python3 "$PTB" demand list 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for r in rows:
    if r.get("url") == sys.argv[1]:
        print(r.get("path") or "")
        break
' "$URL"
}

# 登记到状态库：pageId/title 从 URL 或文件名推导；jira 未建档时降级为无归属登记并提示 attach
register() { # $1=md 路径
  local md="$1" page_id title stem llm errf
  stem="$(basename "$md" .md)"
  page_id="$(printf '%s' "$URL" | sed -nE 's/.*[?&]pageId=([0-9]+).*/\1/p')"
  if [ -z "$page_id" ]; then page_id="$(printf '%s' "$stem" | sed -nE 's/-([0-9]+)$/\1/p')"; fi
  title="$(printf '%s' "$stem" | sed -nE 's/-[0-9]+$//')"
  llm=""
  if [ -f "${md%.md}.llm.md" ]; then llm="${md%.md}.llm.md"; fi
  local args=(demand add --url "$URL" --path "$md" --page-id "$page_id" --title "$title")
  if [ -n "$llm" ]; then args+=(--llm-path "$llm"); fi
  errf="$(mktemp)"
  if [ -n "$JIRA" ]; then
    if python3 "$PTB" "${args[@]}" --jira "$JIRA" 2>"$errf"; then
      rm -f "$errf"
      echo "拆解输入: ${llm:-$md}（优先读 .llm.md 模型阅读版）"
      return 0
    fi
    if grep -q "不存在" "$errf"; then
      echo "提示: Jira $JIRA 尚未建档（ptb jira add）。先按无归属登记，建档后执行: python3 \$PTB demand attach --id <D号> --jira $JIRA"
    else
      cat "$errf" >&2; rm -f "$errf"; exit 1
    fi
    rm -f "$errf"
  fi
  python3 "$PTB" "${args[@]}"
  echo "拆解输入: ${llm:-$md}（优先读 .llm.md 模型阅读版）"
}

DEMAND_BASE="$PTB_HOME/demands"
DEST_DIR="$DEMAND_BASE/${JIRA:-_inbox}"

# ---- --recheck：先对版本元数据，元数据变了才做内容 diff ----
OLD_PATH="$(existing_path || true)"
if [ "$RECHECK" -eq 1 ] && [ -n "$OLD_PATH" ] && [ -f "$OLD_PATH" ]; then
  TMP="$(mktemp -d "${TMPDIR:-/tmp}/demand-recheck.XXXXXX")"
  NEW_MD="$(cwiki_download "$TMP")"
  if [ -n "$(meta_of "$OLD_PATH")" ] && [ "$(meta_of "$OLD_PATH")" = "$(meta_of "$NEW_MD")" ]; then
    echo "未变更（$(meta_of "$NEW_MD" | tr '\n' ' ')）— 版本元数据一致，不做内容 diff"
    rm -rf "$TMP"
    exit 0
  fi
  echo "版本元数据已变化:"
  echo "  旧: $(meta_of "$OLD_PATH" | tr '\n' ' ')"
  echo "  新: $(meta_of "$NEW_MD" | tr '\n' ' ')"
  echo "--- 内容 diff（前 40 行）---"
  diff -u "$OLD_PATH" "$NEW_MD" | head -n 40 || true
  echo "---"
  mkdir -p "$DEST_DIR"
  NEW_BASE="$(basename "$NEW_MD")"
  mv "$NEW_MD" "$DEST_DIR/$NEW_BASE"
  if [ -f "${NEW_MD%.md}.llm.md" ]; then
    mv "${NEW_MD%.md}.llm.md" "$DEST_DIR/$(basename "${NEW_MD%.md}.llm.md")"
  fi
  if [ -d "$TMP/assets" ]; then
    mkdir -p "$DEST_DIR/assets"
    cp -R "$TMP/assets/." "$DEST_DIR/assets/"
  fi
  # 标题变了 → 新旧文件名不同，清掉旧文件避免双份
  if [ "$OLD_PATH" != "$DEST_DIR/$NEW_BASE" ] && [ -f "$OLD_PATH" ]; then
    rm -f "$OLD_PATH" "${OLD_PATH%.md}.llm.md"
  fi
  rm -rf "$TMP"
  register "$DEST_DIR/$NEW_BASE"
  echo "已覆盖并重登记: $DEST_DIR/$NEW_BASE"
  exit 0
fi

# ---- 正常下载 + 登记 ----
mkdir -p "$DEST_DIR"
MD="$(cwiki_download "$DEST_DIR")"
register "$MD"
echo "落盘: $MD"
