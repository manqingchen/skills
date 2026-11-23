#!/usr/bin/env bash
# survey-size.sh — 统计源文件规模，给出 §1 反向调研方式建议
# 用法: survey-size.sh [<项目根>]（默认当前目录）
# 判定: < 50 个源文件 → 小项目，直接 Glob/Grep/Read 自己扫
#       ≥ 50 → 中大型，用 Agent 并行派发（A 架构 / B 业务模块 / C 数据层 / D 接口层）
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

COUNT="$(find "$ROOT" -type f \
  \( -name '*.ts' -o -name '*.tsx' -o -name '*.js' -o -name '*.jsx' -o -name '*.vue' \
     -o -name '*.mjs' -o -name '*.cjs' -o -name '*.py' -o -name '*.go' \
     -o -name '*.java' -o -name '*.kt' \) \
  -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/dist/*' \
  -not -path '*/build/*' -not -path '*/.codegraph/*' -not -path '*/coverage/*' \
  -not -path '*/vendor/*' 2>/dev/null | wc -l | tr -d ' ')"

echo "源文件数: ${COUNT}（${ROOT}，已排除 node_modules/dist/build/.git/.codegraph/vendor/coverage）"
if [ "$COUNT" -lt 50 ]; then
  echo "建议: 小项目（<50）— 直接用 Glob / Grep / Read 自己扫"
else
  echo "建议: 中大型项目（≥50）— 用 Agent 并行派发，各 agent 一个主题互不重叠:"
  echo "  A 整体架构（技术栈/目录/构建/依赖）   B 相关业务模块（同类功能与代码位置）"
  echo "  C 数据层（模型/表/迁移/ORM）          D 接口层（路由/鉴权/请求响应约定/错误码）"
  echo "  派发要求: 只回结论 + 路径 + 关键代码位置，不要回大段原文"
fi
