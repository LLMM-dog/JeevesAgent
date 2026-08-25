# Jeeves 生产镜像：多阶段构建，前端产物打进运行镜像。

# ── 阶段 1：构建前端 ──
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── 阶段 2：运行后端 ──
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    JEEVES_APP__ENV=prod
WORKDIR /app

# uv 官方镜像直接拷二进制，避免在容器里再跑安装脚本
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 先拷依赖清单再装 —— 清单不变时这一层走缓存，秒过
COPY pyproject.toml uv.lock alembic.ini ./
COPY backend/ ./backend/
RUN uv sync --frozen --no-dev --extra docker --extra mcp --extra search --extra cron --extra web

# 前端产物（阶段 1）
COPY --from=frontend /build/dist ./frontend/dist

EXPOSE 9000
# 生产由 systemd/编排器管理生命周期，这里前台运行
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--app-dir", "backend"]
