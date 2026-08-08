#!/usr/bin/env bash
# jeeves 启动脚本（macOS / Linux）。
#
# 默认起开发模式：后端 --reload + 前端 vite dev server。
# 加 --prod 则先构建前端再只起后端（后端伺服静态文件）。
#
# 用法：
#   ./start.sh                开发模式
#   ./start.sh --prod         生产模式
#   ./start.sh --backend-only 只起后端
#   PORT=8080 ./start.sh      换端口

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-9000}"
PROD=0
BACKEND_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --prod) PROD=1 ;;
    --backend-only) BACKEND_ONLY=1 ;;
    *) echo "未知参数：$arg"; exit 1 ;;
  esac
done

fail() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }
info() { printf '\033[36m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$1"; }

# ── 前置检查 ──
#
# 这几项任何一项缺失，启动都会失败，而 uvicorn 的报错通常不指向真因。

command -v uv >/dev/null 2>&1 || \
  fail "找不到 uv。安装：curl -LsSf https://astral.sh/uv/install.sh | sh"

[ -f "$ROOT/.env" ] || fail "缺少 .env。先跑：python scripts/setup.py"

# ENCRYPTION_KEY 为空时后端拒绝启动。虽然报错明确，
# 但用户不知道该怎么生成一个合法的 Fernet key。
if ! grep -qE '^JEEVES_SECURITY__ENCRYPTION_KEY=.+' "$ROOT/.env"; then
  fail ".env 里 JEEVES_SECURITY__ENCRYPTION_KEY 为空。跑 python scripts/setup.py 自动生成"
fi

# ── 端口抢占检查 ──
#
# 如果上次 start.sh 被 kill -9（或终端直接关掉），trap 不会执行，
# uvicorn 会变成孤儿并继续占着端口。下次启动时 uvicorn 直接报
# "Address already in use" 然后退出，而脚本会说"后端启动失败" ——
# 完全不指向"端口被占"这个真因。

if command -v lsof >/dev/null 2>&1; then
  OCCUPANT="$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$OCCUPANT" ]; then
    printf '\033[33m⚠ 端口 %s 已被占用（PID %s）\033[0m\n' "$PORT" "$(echo "$OCCUPANT" | tr '\n' ' ')"
    echo "  可能是上次没有正常退出留下的进程。"
    printf '  清理并继续？[Y/n]: '
    read -r ans || ans=""
    case "$ans" in
      ""|y|Y|yes|YES)
        echo "$OCCUPANT" | xargs -r kill -TERM 2>/dev/null || true
        sleep 1
        echo "$OCCUPANT" | xargs -r kill -KILL 2>/dev/null || true
        sleep 1
        if lsof -ti :"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
          fail "清理后端口仍被占用，请手动关掉它再试"
        fi
        ok "已清理"
        ;;
      *) fail "端口被占用，已取消" ;;
    esac
  fi
fi

# ── 生产模式：先构建前端 ──

if [ "$PROD" = "1" ]; then
  [ -d "$ROOT/frontend/node_modules" ] || \
    fail "前端依赖未安装。先跑：cd frontend && npm install"

  # 【只在源码比产物新时才构建】。
  #
  # 生产模式是普通用户的默认入口，而无条件构建意味着每次启动都白等
  # 若干秒 —— 用户什么都没改，产物却重新生成一遍。
  #
  # 判据是"有没有源文件比 dist/index.html 更新"。比较 index.html 而不是
  # 整个 dist 目录：目录的 mtime 在某些文件系统上不随内容更新，
  # 而 index.html 每次构建必然重写。
  #
  # 用 find -newer 而不是比时间戳数值：前者是 POSIX 的，
  # 后者要用 stat，而 stat 的参数在 GNU 和 BSD（macOS）上不一样。
  DIST="$ROOT/frontend/dist/index.html"
  NEED_BUILD=1
  if [ -f "$DIST" ]; then
    # 除了 src/，还要看会影响构建结果的配置 —— 漏掉它们的话，
    # 改了 vite.config 却不重新构建，那种"改了没生效"最难排查。
    NEWER=$(
      find "$ROOT/frontend/src" -type f -newer "$DIST" -print -quit 2>/dev/null
      for f in vite.config.ts tsconfig.json tsconfig.app.json package.json \
               index.html tailwind.config.ts postcss.config.js; do
        [ -f "$ROOT/frontend/$f" ] && \
          find "$ROOT/frontend/$f" -newer "$DIST" -print -quit 2>/dev/null
      done
    )
    if [ -z "$NEWER" ]; then
      NEED_BUILD=0
      info "前端产物已是最新，跳过构建"
    fi
  fi

  if [ "$NEED_BUILD" = "1" ]; then
    info "构建前端…（首次或源码有改动）"
    (cd "$ROOT/frontend" && npm run build) || fail "前端构建失败"
    ok "构建完成"
  fi
fi

# ── 后端参数 ──

BACKEND_ARGS=(run uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --app-dir backend)
# 生产模式不要 --reload：多起一个 watch 进程，
# 而文件变化时重启会中断正在进行的对话流
[ "$PROD" = "1" ] || BACKEND_ARGS+=(--reload)

if [ "$PROD" = "1" ] || [ "$BACKEND_ONLY" = "1" ]; then
  info "启动后端 http://127.0.0.1:$PORT"
  [ "$PROD" = "1" ] && echo "（前端已构建，直接访问上面这个地址）"
  cd "$ROOT"

  # 放后台启动，等就绪后自动打开浏览器
  uv "${BACKEND_ARGS[@]}" &
  BACKEND_PID=$!

  for _ in $(seq 1 40); do
    sleep 0.5
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      fail "后端启动失败"
    fi
    if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      break
    fi
  done

  # 自动打开浏览器
  if [ "$PROD" = "1" ]; then
    if command -v open >/dev/null 2>&1; then
      open "http://127.0.0.1:$PORT"
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "http://127.0.0.1:$PORT"
    fi
  fi

  wait "$BACKEND_PID"
  exit $?
fi

# ── 开发模式：两个都起 ──

[ -d "$ROOT/frontend/node_modules" ] || \
  fail "前端依赖未安装。先跑：cd frontend && npm install"

mkdir -p "$ROOT/data"
BACKEND_LOG="$ROOT/data/backend.log"

info "启动后端（日志：data/backend.log）…"
cd "$ROOT"

# 让后端自成一个进程组。
#
# 为什么必须这样：进程树是三层（uv → uvicorn → 可能的 reload worker）。
# 只 kill 顶层的话，uvicorn 会变成孤儿并继续占端口 —— 下次启动报
# "Address already in use"，而用户以为是别的程序占了。
#
# 而 `kill -TERM -$PID`（负号 = 整个进程组）要求那个 PID 【本身是组长】。
# 直接 `cmd &` 启动的后台进程在非交互 shell 里【和脚本共用进程组】——
# 那时负号会把脚本自己也杀掉。
#
# setsid 让它成为新会话的组长，负号才有正确含义。
# 没有 setsid（比如 macOS 默认不带）时退回 set -m 的作业控制。
if command -v setsid >/dev/null 2>&1; then
  setsid uv "${BACKEND_ARGS[@]}" >"$BACKEND_LOG" 2>&1 &
  BACKEND_PID=$!
  BACKEND_GROUP=1
else
  # set -m 开作业控制，让后台作业进入自己的进程组
  set -m
  uv "${BACKEND_ARGS[@]}" >"$BACKEND_LOG" 2>&1 &
  BACKEND_PID=$!
  set +m
  BACKEND_GROUP=1
fi

# 【必须清理后端】。
#
# 不清的话 Ctrl-C 后后端还占着端口，下次启动报 "Address already in use"，
# 而用户以为是别的程序占了。
#
# 杀整个进程组：uv 会 fork 出 uvicorn，只杀 uv 会留下 uvicorn 继续占端口。
cleanup() {
  if kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo
    printf '\033[90m停止后端…\033[0m\n'
    # 负号 = 整个进程组。前面用 setsid / set -m 保证了 BACKEND_PID 是组长。
    kill -TERM -"$BACKEND_PID" 2>/dev/null || kill -TERM "$BACKEND_PID" 2>/dev/null || true
    # 给它时间优雅退出 —— lifespan 里要关数据库、停追踪写入器、
    # terminate MCP 子进程。直接 KILL 会留下 MCP 的僵尸进程。
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$BACKEND_PID" 2>/dev/null || break
      sleep 0.3
    done
    kill -KILL -"$BACKEND_PID" 2>/dev/null || kill -KILL "$BACKEND_PID" 2>/dev/null || true
  fi

  # 兜底：确认端口真的释放了。
  #
  # 前面的进程组 kill 理论上够了，但如果 uvicorn 自己又 fork 了别的东西
  # （--reload 的 watcher），可能还有残留。
  if command -v lsof >/dev/null 2>&1; then
    leftover="$(lsof -ti :"$PORT" 2>/dev/null || true)"
    if [ -n "$leftover" ]; then
      printf '\033[90m清理残留的端口占用…\033[0m\n'
      echo "$leftover" | xargs -r kill -KILL 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT INT TERM

# 等后端起来。不等的话前端第一次请求会失败，
# 浏览器控制台一片红，而实际只是启动竞态。
READY=0
for _ in $(seq 1 40); do
  sleep 0.5
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    printf '\033[31m✗ 后端启动失败，日志尾部：\033[0m\n' >&2
    tail -20 "$BACKEND_LOG" >&2 || true
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
done

if [ "$READY" = "1" ]; then
  ok "后端就绪 http://127.0.0.1:$PORT"
else
  printf '\033[33m⚠ 后端 20 秒内没响应健康检查，但进程还在。继续起前端\033[0m\n'
fi

info "启动前端…（Ctrl-C 停止，会一并停掉后端）"
echo

cd "$ROOT/frontend"
npm run dev
