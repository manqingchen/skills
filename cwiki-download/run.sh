#!/usr/bin/env bash
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$SCRIPT_DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

# 如果调用方没有显式指定 CWIKI_DOWNLOAD_WORKSPACE_ROOT，
# 默认使用调用 run.sh 时的工作目录作为“工程根目录”，而不是 skill 自己的目录。
if [ -z "${CWIKI_DOWNLOAD_WORKSPACE_ROOT:-}" ]; then
  export CWIKI_DOWNLOAD_WORKSPACE_ROOT="$(pwd)"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "未检测到 uv，请先安装 uv 后再运行 cwiki-download。" >&2
  echo "安装命令：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

uv run --directory "$SCRIPT_DIR" python -m scripts.cwiki_download "$@"
