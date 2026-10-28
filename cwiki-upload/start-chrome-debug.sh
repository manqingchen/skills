#!/usr/bin/env bash

DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}"
LOCK_DIR="/tmp/cwiki-download-chrome.lock"
LOCK_WAIT_SECONDS=30
PORT_WAIT_SECONDS=15
QUIT_WAIT_SECONDS="${CHROME_QUIT_WAIT_SECONDS:-20}"
CHROME_BIN="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-$HOME/Library/Application Support/Google/Chrome}"
FORCE_KILL_WAIT_SECONDS="${CHROME_FORCE_KILL_WAIT_SECONDS:-5}"
DEBUG_HOST="${CHROME_DEBUG_HOST:-127.0.0.1}"
DEBUG_VERSION_URL="http://${DEBUG_HOST}:${DEBUG_PORT}/json/version"
CHROME_APP_NAME="${CHROME_APP_NAME:-Google Chrome}"
CHROME_START_URL="${CHROME_START_URL:-about:blank}"

check_port() {
    lsof -i :$DEBUG_PORT >/dev/null 2>&1
}

check_cdp_ready() {
    curl -fsS "$DEBUG_VERSION_URL" 2>/dev/null | grep -q '"Browser"'
}

is_chrome_running() {
    pgrep -x "Google Chrome" >/dev/null 2>&1 || pgrep -f "$CHROME_BIN" >/dev/null 2>&1
}

wait_chrome_exit() {
    local waited=0
    while is_chrome_running; do
        if [ "$waited" -ge "$QUIT_WAIT_SECONDS" ]; then
            echo "等待 Chrome 退出超时（${QUIT_WAIT_SECONDS}s），请手动关闭 Chrome 后重试" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 0
}

force_kill_chrome() {
    pkill -x "Google Chrome" >/dev/null 2>&1 || true
    pkill -f "$CHROME_BIN" >/dev/null 2>&1 || true
}

cleanup_stale_chrome_singletons() {
    if is_chrome_running; then
        return 0
    fi

    local name
    local path
    for name in SingletonLock SingletonCookie SingletonSocket; do
        path="$CHROME_USER_DATA_DIR/$name"
        if [ -e "$path" ] || [ -L "$path" ]; then
            echo "清理 Chrome 残留锁：$path"
            rm -f "$path" 2>/dev/null || true
        fi
    done
}

ensure_chrome_stopped() {
    if ! is_chrome_running; then
        return 0
    fi

    osascript -e 'tell application "Google Chrome" to quit' 2>/dev/null || true
    if wait_chrome_exit; then
        return 0
    fi

    echo "Chrome 优雅退出超时，开始强制结束进程..."
    force_kill_chrome

    local waited=0
    while is_chrome_running; do
        if [ "$waited" -ge "$FORCE_KILL_WAIT_SECONDS" ]; then
            echo "强制结束 Chrome 仍超时（${FORCE_KILL_WAIT_SECONDS}s），请手动关闭后重试" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 0
}

acquire_lock() {
    local waited=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        if [ "$waited" -ge "$LOCK_WAIT_SECONDS" ]; then
            echo "等待 Chrome 启动锁超时（${LOCK_WAIT_SECONDS}s），请稍后重试" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "$$" > "$LOCK_DIR/pid" 2>/dev/null || true
    return 0
}

release_lock() {
    rm -rf "$LOCK_DIR" 2>/dev/null || true
}

wait_port_ready() {
    local waited=0
    while true; do
        if check_port && check_cdp_ready; then
            return 0
        fi
        if [ "$waited" -ge "$PORT_WAIT_SECONDS" ]; then
            echo "等待 Chrome 调试接口 ${DEBUG_VERSION_URL} 就绪超时（${PORT_WAIT_SECONDS}s）" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

start_chrome_debug() {
    if command -v open >/dev/null 2>&1 && [ "$(uname -s)" = "Darwin" ]; then
        open -na "$CHROME_APP_NAME" --args \
          --remote-debugging-address="$DEBUG_HOST" \
          --remote-debugging-port="$DEBUG_PORT" \
          --user-data-dir="$CHROME_USER_DATA_DIR" \
          "$CHROME_START_URL"
        return $?
    fi

    nohup "$CHROME_BIN" \
      --remote-debugging-address="$DEBUG_HOST" \
      --remote-debugging-port="$DEBUG_PORT" \
      --user-data-dir="$CHROME_USER_DATA_DIR" \
      "$CHROME_START_URL" </dev/null >/dev/null 2>&1 &
    CHROME_PID=$!
    disown "$CHROME_PID" 2>/dev/null || true
}

# 并发保护：避免多个 Cursor 窗口同时触发重启 Chrome
acquire_lock || exit 1
trap release_lock EXIT

# 进锁后再检查一次调试接口，避免竞态
if check_port && check_cdp_ready; then
    echo "Chrome 调试端口 $DEBUG_PORT 已开启，无需重新启动"
    exit 0
fi

echo "正在检查 Chrome 状态..."

# 检查并确保 Chrome 完整退出
if is_chrome_running; then
    echo "Chrome 正在运行，需要重启以开启调试模式"
    echo "这将关闭当前 Chrome 窗口并重新打开..."
fi
ensure_chrome_stopped || exit 1
cleanup_stale_chrome_singletons

# macOS 优先交给 LaunchServices 拉起 GUI 应用，非 macOS 再回退到 nohup/disown。
start_chrome_debug || exit 1

wait_port_ready || exit 1
echo "Chrome 已启动，调试地址: ${DEBUG_HOST}:${DEBUG_PORT}"
echo "注意：首次启动后需要重新登录 Confluence"
echo "之后 Cookie 会保留，下次启动无需重新登录"
