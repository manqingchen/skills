#!/usr/bin/env bash
# i18n-guard.sh — 多语言任务验收守卫：按固定模板补 1 条验收 + 1 条风险
# 用法: i18n-guard.sh --jira X --task T00X
# 程序化 §7 「涉及词典 key 的任务，验收至少加一条『各目标语言词典查询返回非空』，
# 并默认记一条风险『译文未下发前，关闭了 fallback 的端会直接渲染 key 原文』」。
# 注意: 验收要按翻译平台实际能力写（能否按语言登记复数形态等），不要按理想能力写。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PTB="$(cd "$SCRIPT_DIR/../../.." && pwd -P)/scripts/ptb.py"

JIRA=""; TASK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --jira) JIRA="${2:?}"; shift 2 ;;
    --task) TASK="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$JIRA" ] || [ -z "$TASK" ]; then
  echo "用法: i18n-guard.sh --jira X --task T00X" >&2
  exit 2
fi

python3 "$PTB" acc add --jira "$JIRA" --task "$TASK" \
  --desc "各目标语言词典查询返回非空（key 创建成功 successCount>0 只代表源语言可见）" \
  --kind functional
python3 "$PTB" risk add --jira "$JIRA" \
  --desc "译文未下发前，关闭了 fallback 的端会直接渲染 key 原文" \
  --note "翻译平台按 key 异步翻译；写验收前先确认平台能力边界（能否按语言登记复数形态等）"
echo "已为 $TASK 补多语言验收与风险（重复执行会追加同文案条目，勿重复跑）"
