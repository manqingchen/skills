#!/usr/bin/env bash
# task-save.sh — task add 的幂等版：任务已存在时自动转 task set / task text 更新
# 用法: 与 task add 相同。例:
#   task-save.sh --jira X --id T001 --title "..." --end frontend --dep T000 --req R1 --goal-file /tmp/g.md
# 程序化 §7 硬规则「task add 前先 task list 查重，已存在的用 task set / task text 更新」:
#   首次 → task add；已存在 → 标量字段走 task set，文本字段(goal/implementation/…)走 task text，
#   --dep/--req 幂等补挂（dep add / req link 均为 OR IGNORE）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PTB="$(cd "$SCRIPT_DIR/../../.." && pwd -P)/scripts/ptb.py"

set +e
OUT="$(python3 "$PTB" task add "$@" 2>&1)"
RC=$?
set -e
echo "$OUT"
if [ "$RC" -eq 0 ]; then
  exit 0
fi
if ! printf '%s' "$OUT" | grep -q "已存在"; then
  exit "$RC"
fi
echo "-- 任务已存在，转更新（task set / task text / 幂等补挂 dep、req）--"

JIRA=""; TID=""
SET_ARGS=()
TEXT_SPEC=()
DEPS=()
REQS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --jira) JIRA="${2:?}"; shift 2 ;;
    --id) TID="${2:?}"; shift 2 ;;
    --title|--end|--status|--estimate|--parallel|--milestone|--design-ref|--seq)
      SET_ARGS+=("$1" "${2:?$1 需要值}"); shift 2 ;;
    --goal|--goal-file|--not-responsible|--not-responsible-file|--implementation|--implementation-file|\
    --boundaries|--boundaries-file|--notes|--notes-file)
      TEXT_SPEC+=("$1" "${2:?$1 需要值}"); shift 2 ;;
    --dep) DEPS+=("${2:?}"); shift 2 ;;
    --req) REQS+=("${2:?}"); shift 2 ;;
    *) echo "task-save: 忽略未知参数 $1" >&2; shift ;;
  esac
done
if [ -z "$JIRA" ] || [ -z "$TID" ]; then
  echo "错误: 更新路径需要 --jira 与 --id" >&2
  exit 2
fi

if [ "${#SET_ARGS[@]}" -gt 0 ]; then
  python3 "$PTB" task set "$TID" --jira "$JIRA" ${SET_ARGS[@]+"${SET_ARGS[@]}"}
fi

i=0
while [ "$i" -lt "${#TEXT_SPEC[@]}" ]; do
  flag="${TEXT_SPEC[$i]}"; val="${TEXT_SPEC[$((i + 1))]}"; i=$((i + 2))
  field="$(printf '%s' "$flag" | sed -E 's/^--//; s/-file$//; s/-/_/g')"
  if printf '%s' "$flag" | grep -q -- '-file$'; then
    python3 "$PTB" task text "$TID" --jira "$JIRA" --field "$field" --file "$val"
  else
    python3 "$PTB" task text "$TID" --jira "$JIRA" --field "$field" --text "$val"
  fi
done

for d in ${DEPS[@]+"${DEPS[@]}"}; do
  python3 "$PTB" dep add --jira "$JIRA" --task "$TID" --on "$d"
done
for r in ${REQS[@]+"${REQS[@]}"}; do
  python3 "$PTB" req link --jira "$JIRA" --task "$TID" --id "$r"
done
echo "已更新任务 ${TID}（幂等：dep/req 为 OR IGNORE，重复挂载无副作用）"
