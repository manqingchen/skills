#!/usr/bin/env bash
# stage-check.sh — 检测设计稿 HTML 是否为「缩放舞台」（固定宽度画布 + transform:scale）
# 用法: stage-check.sh <html-path>
# 输出: 舞台宽、渲染比例来源、可直接粘贴的 design add --note 与 jiras.baseline 提示句
# 依据: 缩放舞台的一切绝对像素都是舞台坐标，不能直接落地；拆解阶段只检测与传递，不换算
set -euo pipefail

if [ $# -ne 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  echo "用法: stage-check.sh <html-path>" >&2
  exit 2
fi
HTML="$1"
if [ ! -f "$HTML" ]; then
  echo "文件不存在: $HTML" >&2
  exit 2
fi

# 固定宽度画布：width: 1200px 以上（≥3 位数，排除 icon 级小宽度）
WIDTHS="$(grep -oE 'width:[[:space:]]*[0-9]{3,}px' "$HTML" | grep -oE '[0-9]+' | sort -n | uniq | tr '\n' ' ' || true)"
# 缩放应用：CSS 字面量，或 JS（style.transform 赋值 / scale(${..}) 模板串 / clientWidth 比值）
SCALE_CSS="$(grep -cE 'transform:[[:space:]]*scale\(' "$HTML" || true)"
SCALE_JS="$(grep -cE 'style\.transform|scale\(\$\{|clientWidth[[:space:]]*/' "$HTML" || true)"

SCALE_TOTAL=$((SCALE_CSS + SCALE_JS))
if [ -z "$WIDTHS" ] || [ "$SCALE_TOTAL" -eq 0 ]; then
  echo "非缩放舞台（未同时检出固定宽度画布（width: ≥3位数px）与 transform:scale 应用）"
  exit 0
fi

STAGE="$(printf '%s' "$WIDTHS" | awk '{print $NF}')"   # sort -n 后最后一个即最大宽度
if [ "$SCALE_JS" -gt 0 ]; then
  RATIO_SRC="JS 动态计算（style.transform / clientWidth 比值）"
else
  RATIO_SRC="CSS 固定值 transform:scale(...)"
fi
cat <<EOF
缩放舞台: 是
舞台宽: ${STAGE}px（候选宽度: $(printf '%s' "$WIDTHS")）
渲染比例: ${RATIO_SRC} —— 一切绝对像素都是舞台坐标，不能直接落地
design add --note: "缩放舞台: 画布宽 ${STAGE}px, ${RATIO_SRC}; 绝对像素为舞台坐标"
jiras.baseline 提示: "设计稿为缩放舞台（${STAGE}px），落地尺寸需按参照物比值换算"
EOF
