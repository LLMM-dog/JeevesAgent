#!/usr/bin/env bash
# Jeeves 一键部署脚本（Linux 服务器）。
#
# 用法：
#   ./deploy/deploy.sh user@server [--domain example.com]
#
# 做什么：
#   1. rsync 项目到远端 /opt/jeeves（排除 .git/.venv/data 等）
#   2. 远端装 uv + Node.js（缺则装）
#   3. uv sync 装后端依赖，npm 构建前端
#   4. 生成 .env（含新 ENCRYPTION_KEY；首次自动开启鉴权）
#   5. 安装 systemd 服务并启动
#   6. 给了 --domain 时再装 Caddy 反代（自动 HTTPS）
#
# 安全默认：首次部署强制 JEEVES_SECURITY__AUTH_ENABLED=true，
# 管理员密码自动生成并打印 —— 不裸奔到公网。

set -euo pipefail

SERVER="${1:-}"
DOMAIN=""
REMOTE_DIR="/opt/jeeves"
REMOTE_USER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    *) SERVER="$1"; shift ;;
  esac
done

if [ -z "$SERVER" ]; then
  echo "用法: ./deploy/deploy.sh user@server [--domain example.com]" >&2
  exit 1
fi

fail() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }
info() { printf '\033[36m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$1"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 0. 本地前置检查 ──
command -v rsync >/dev/null 2>&1 || fail "本机缺 rsync（Windows 用户可用 Git Bash / WSL）"
command -v ssh  >/dev/null 2>&1 || fail "本机缺 ssh"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$SERVER" 'echo ok' >/dev/null 2>&1 \
  || fail "无法免密 SSH 登录 $SERVER（先 ssh-copy-id）"

# ── 1. 推送代码 ──
info "推送代码到 $SERVER:$REMOTE_DIR …"
ssh "$SERVER" "mkdir -p $REMOTE_DIR"
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude 'data' --exclude 'workspace' \
  --exclude 'frontend/node_modules' --exclude 'frontend/dist' \
  --exclude 'personas/*.md' --exclude 'config/*.yaml' --exclude '*.pyc' \
  -e ssh "$ROOT/" "$SERVER:$REMOTE_DIR/"
ok "代码已推送"

# ── 2. 远端依赖 ──
info "检查远端依赖（uv / node）…"
ssh "$SERVER" 'bash -s' <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! command -v uv >/dev/null 2>&1; then
  echo "安装 uv …"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v node >/dev/null 2>&1; then
  echo "安装 Node.js 20 …"
  if command -v apt-get >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
  else
    fail "不认识这个系统的包管理器，请手动装 Node.js 18+ 后重试"
  fi
fi
REMOTE
ok "远端依赖就绪"

# ── 3. 安装依赖 + 构建前端 ──
info "安装后端依赖并构建前端（首次约 3-5 分钟）…"
ssh "$SERVER" "cd $REMOTE_DIR && export PATH=\"$HOME/.local/bin:\$PATH\" && uv sync --frozen --extra docker --extra mcp --extra search --extra cron --extra web"
ssh "$SERVER" "cd $REMOTE_DIR/frontend && npm ci && npm run build"
ok "依赖与前端就绪"

# ── 4. .env（首次生成）──
info "检查 .env …"
ENV_OK=$(ssh "$SERVER" "test -f $REMOTE_DIR/.env && grep -q '^JEEVES_SECURITY__ENCRYPTION_KEY=.' $REMOTE_DIR/.env && echo yes || echo no")
if [ "$ENV_OK" != "yes" ]; then
  echo "生成 .env（含新加密密钥，鉴权强制开启）…"
  ssh "$SERVER" "cd $REMOTE_DIR && cp .env.example .env && uv run python -c \"from cryptography.fernet import Fernet; print('JEEVES_SECURITY__ENCRYPTION_KEY=' + Fernet.generate_key().decode())\" >> .env"
  # 服务器部署必须开鉴权：不裸奔
  ssh "$SERVER" "cd $REMOTE_DIR && sed -i 's/^JEEVES_SECURITY__AUTH_ENABLED=false/JEEVES_SECURITY__AUTH_ENABLED=true/' .env && sed -i 's/^JEEVES_APP__HOST=.*/JEEVES_APP__HOST=0.0.0.0/' .env && sed -i 's/^JEEVES_APP__ENV=.*/JEEVES_APP__ENV=prod/' .env"
  ok ".env 已生成（管理员密码见首次启动日志）"
else
  ok ".env 已存在，保留"
fi

# ── 5. systemd 服务 ──
info "安装 systemd 服务…"
ssh "$SERVER" 'bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/jeeves

# 专用运行用户（不存在则创建，禁止登录）
if ! id jeeves >/dev/null 2>&1; then
  useradd --system --home /opt/jeeves --shell /usr/sbin/nologin jeeves
fi
chown -R jeeves:jeeves /opt/jeeves/data /opt/jeeves/workspace /opt/jeeves/skills /opt/jeeves/personas /opt/jeeves/config 2>/dev/null || true

cp deploy/systemd/jeeves.service /etc/systemd/system/jeeves.service
systemctl daemon-reload
systemctl enable --now jeeves
sleep 3
systemctl is-active --quiet jeeves || { echo '服务未起来，看日志: journalctl -u jeeves -n 50'; exit 1; }
REMOTE
ok "Jeeves 服务已启动"

# ── 6. Caddy 反代（可选）──
if [ -n "$DOMAIN" ]; then
  info "安装 Caddy 并配置 $DOMAIN …"
  ssh "$SERVER" 'bash -s' "$DOMAIN" <<'REMOTE'
set -euo pipefail
DOMAIN="$1"

if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl 2>/dev/null || true
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

sed "s/your-domain.com/$DOMAIN/g" /opt/jeeves/deploy/Caddyfile > /etc/caddy/Caddyfile
systemctl enable --now caddy
REMOTE
ok "Caddy 已配置 https://$DOMAIN"
fi

echo
echo "部署完成："
if [ -n "$DOMAIN" ]; then echo "  https://$DOMAIN"; fi
echo "  首次登录密码看启动日志: ssh $SERVER 'journalctl -u jeeves -n 100 | grep 初始密码'"
