# 部署与远程访问

Jeeves 默认本机运行（`127.0.0.1:9000`，无鉴权）。远程访问有两条路：

| 方式 | 适合 | 安全模型 |
| --- | --- | --- |
| **Tailscale 隧道** | 个人设备访问自己的 Jeeves（推荐） | WireGuard 私有组网，仅授权设备可达；funnel 可选择性公网暴露 |
| **Linux 服务器 + Caddy** | 已有云服务器、想长期稳定服务 | 公网 HTTPS，登录鉴权 + 限流 + 安全头兜底 |
| **Docker** | 服务器上容器化 | 同服务器方案，编排器管理 |

> **无论哪条路，第一步都是开启鉴权**：`JEEVES_SECURITY__AUTH_ENABLED=true`。
> 绑定非本机地址而未开鉴权会**拒绝启动**（硬校验）。

## 快速开始（界面，无需翻文件）

打开 `设置 → 部署`：

1. 输入管理员用户名 + 密码，点「开启鉴权并创建管理员」
2. 点「安装便携版 Tailscale」→「登录 Tailscale」→ 打开弹出的授权链接
   （先检测系统已装的 Tailscale；没有则下载到项目 `.tailscale/`，随项目删除）
3. 点「开启 serve」，访问 `https://<设备名>.ts.net`

（.env 仍然可用于服务器脚本/高级配置，界面改动会即时同步到内存。）

### 2b. Linux 服务器一键部署

```bash
ssh-copy-id user@server
./deploy/deploy.sh user@server --domain your-domain.com
```

### 2c. Docker

```bash
cp .env.example .env   # 开鉴权
docker compose up -d --build
```

## 运维要点

- 改 `.env` 后重启：`systemctl restart jeeves`（systemd）或 `docker compose up -d`
- 备份：`data/`（数据库 + 记忆）+ `.env`（加密密钥，丢了 API Key 全部无法解密）
- 升级：`git pull && ./deploy/deploy.sh user@server` 或 `docker compose up -d --build`

完整细节见 [deploy/README.md](../../deploy/README.md)。
