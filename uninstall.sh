#!/usr/bin/env bash
# Jeeves 一键卸载（Linux / macOS）
#
# 用法：
#   bash uninstall.sh            # 清理项目外残留，保留源码
#   bash uninstall.sh --all      # 连 Docker 镜像、包缓存一起清
#   bash uninstall.sh --delete   # 之后直接删除整个项目文件夹

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

info() { printf '\033[36m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m%s\033[0m\n' "$1"; }

info "Jeeves 卸载"
info "项目目录：$ROOT"

# 1. 清理便携版 Tailscale（.tailscale/ + tailscaled 进程）
if [ -d "$ROOT/.tailscale" ]; then
  TS="$ROOT/.tailscale/bin/tailscale"
  SOCK="$ROOT/.tailscale/tailscaled.sock"
  if [ -x "$TS" ]; then
    "$TS" --socket "$SOCK" logout >/dev/null 2>&1 || true
  fi
  pkill -f "$ROOT/.tailscale" >/dev/null 2>&1 || true
  rm -rf "$ROOT/.tailscale"
  ok "已清理便携版 Tailscale"
else
  info "无便携版 Tailscale"
fi

# 2. 清理 Docker 容器 / 其它项目外残留
if command -v uv >/dev/null 2>&1; then
  uv run python scripts/uninstall.py "$@"
else
  python3 scripts/uninstall.py "$@"
fi

# 3. 可选：删除整个项目文件夹
for a in "$@"; do
  if [ "$a" = "--delete" ]; then
    printf '确定删除整个项目文件夹（含源码，不可恢复）？[y/N]: '
    read -r ans || ans=""
    case "$ans" in
      y|Y|yes|YES)
        info "删除 $ROOT ..."
        cd / && rm -rf "$ROOT"
        ok "已删除项目文件夹"
        exit 0
        ;;
      *) info "已取消删除文件夹" ;;
    esac
  fi
done

ok "卸载完成。现在可以手动删除项目文件夹：$ROOT"