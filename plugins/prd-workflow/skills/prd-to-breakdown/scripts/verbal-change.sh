#!/usr/bin/env bash
# verbal-change.sh — 口头口径变更的固定入口：decision + risk 落库 + 打印追问话术
# 用法: verbal-change.sh --jira X --desc "<变更内容与生效范围>" [--ref R1] [--answer "..."] \
#         [--options "做/不做"] [--conclusion-file /tmp/c.md]
# 程序化 §2 「口头变更当场做三件事」: decision add 记录变更、risk add 记「wiki 未同步，
# 验收以本条 decision 为准」、追问「是否已让产品更新 wiki」。禁止只改对话不落记录。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PTB="$(cd "$SCRIPT_DIR/../../.." && pwd -P)/scripts/ptb.py"

JIRA=""; DESC=""; REF=""; ANSWER=""; OPTIONS=""; CONCLUSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --jira) JIRA="${2:?}"; shift 2 ;;
    --desc) DESC="${2:?}"; shift 2 ;;
    --ref) REF="${2:?}"; shift 2 ;;
    --answer) ANSWER="${2:?}"; shift 2 ;;
    --options) OPTIONS="${2:?}"; shift 2 ;;
    --conclusion-file) CONCLUSION="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$JIRA" ] || [ -z "$DESC" ]; then
  echo "用法: verbal-change.sh --jira X --desc \"<变更内容>\" [--ref R1] [--answer ...] [--options ...] [--conclusion-file f]" >&2
  exit 2
fi

DEC_ARGS=(decision add --jira "$JIRA" --question "口头口径变更：$DESC")
if [ -n "$REF" ]; then DEC_ARGS+=(--ref "$REF"); fi
if [ -n "$ANSWER" ]; then DEC_ARGS+=(--answer "$ANSWER"); fi
if [ -n "$OPTIONS" ]; then DEC_ARGS+=(--options "$OPTIONS"); fi
if [ -n "$CONCLUSION" ]; then DEC_ARGS+=(--conclusion-file "$CONCLUSION"); fi
python3 "$PTB" ${DEC_ARGS[@]+"${DEC_ARGS[@]}"}

python3 "$PTB" risk add --jira "$JIRA" \
  --desc "wiki 未同步，验收以本条 decision 为准（口头变更：${DESC}）" \
  --note "已按流程追问用户「是否已让产品更新 wiki」；文档同步后可关闭本条风险"

echo "追问话术: 「这条变更是否已让产品更新 wiki？」——未同步前，验收口径以上述 decision 为准，不要按 wiki 旧文执行。"
